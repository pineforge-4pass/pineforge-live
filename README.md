# pineforge-live

Run PineScript strategies anywhere. Send every strategy order action to a
webhook. Connect that webhook to your own broker bridge, automation platform,
or application.

**PineForge Live is broker-neutral.** It needs a compiled PineForge strategy,
market data and a webhook URL. It does not require an exchange account,
choose a broker, or hold broker API keys.

```text
Your market data → PineForge backtest engine → durable order events → your webhook → your broker/automation
```

The strategy engine is C++; the feed/webhook runner is Python. The strategy
ledger is recomputed by the same `run_backtest_full` engine used
for backtests. There is no separate implementation of strategy fill rules.
By default, alerts come from confirmed script bars. Optional intrabar alerts
use the engine's forming-bar probe and are explicitly provisional.

## What works

- `run`: consume JSONL, stdin, HTTP polling or a generic WebSocket feed.
- `check`: process one JSONL/HTTP snapshot and exit, suitable for cron.
- One `order_action` webhook per engine fill, with symbol, side, quantity,
  reference price, Pine order id, bar and strategy identity.
- Durable SQLite decisions and ordered webhook delivery, including restart
  recovery, stable event IDs, bounded retries and optional HMAC signatures.
- `order_update` events to confirm, change or retract provisional intrabar
  signals, without issuing a second action on confirmation.
- Queue inspection, explicit retry/skip, and fenced STOP recovery.
- A working receiver example with transactional event deduplication.

This is pre-alpha software. HTTP success means the receiver accepted the
message; it does not mean a broker filled an order. Real execution prices,
fees, liquidity and receiver behavior remain outside the strategy ledger.
Intrabar signals can change before the bar closes. Choose settled mode for
confirmed backtest-ledger alerts.

## Install

Requires Python 3.12+, macOS or Linux, and a compiled strategy exposing
PineForge C ABI v4. From this source checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev,websocket]'
```

The WebSocket extra is optional. JSONL, stdin, HTTP and webhook delivery use
the Python standard library. `pineforge-engine` supplies the compiled
strategy; this package's runtime loads it with `ctypes`.

To build the public engine and its example strategy corpus:

```sh
git lfs install
git clone https://github.com/pineforge-4pass/pineforge-engine.git ../pineforge-engine
export PINEFORGE_ENGINE_ROOT="$(cd ../pineforge-engine && pwd)"
scripts/build_engine.sh
```

Use an ABI-v4 engine build. The last verified engine revision is
`41c9c741d2b4f20eb793655bef4e9c56357a97fe`. CMake, a C++17 compiler and Git LFS
are required; the corpus feed is an LFS object. The build script requires
`PINEFORGE_ENGINE_ROOT` explicitly and compiles the public corpus, which can
take several minutes. Keep the corpus revision pinned by the engine.

For your own PineScript source, use
[pineforge-codegen-oss](https://github.com/pineforge-4pass/pineforge-codegen-oss)
to generate C++, then compile it against the ABI-v4 engine. The compiler is
a separate project with its own license (PolyForm Noncommercial plus its
published supplemental terms, including personal trading); this repository's
Apache-2.0 license does not change those compiler terms.

## Run a complete local example

In one terminal, set a shared secret and start the example receiver:

```sh
export PINEFORGE_WEBHOOK_SECRET='replace-with-your-own-random-secret'
python examples/webhook_receiver.py --database build/webhook-receipts.sqlite3
```

In another terminal, activate the same virtual environment, set the same
secret, and generate a demo configuration from the built public corpus:

```sh
export PINEFORGE_WEBHOOK_SECRET='replace-with-your-own-random-secret'
python scripts/make_webhook_demo.py \
  --engine-root "$PINEFORGE_ENGINE_ROOT" \
  --output build/webhook-demo

pineforge-live run --config build/webhook-demo/config.json
```

This warms up on 2,000 historical bars, then processes 200 new bars from a
JSONL feed and sends real HTTP requests to `http://127.0.0.1:8765/webhook`.
The receiver records and prints the events. The demo makes no broker calls.

Run the same configuration again: the journal restores the strategy and
already-delivered events are not delivered again. The report is written beside
the journal as `signals.report.json`.

For your own strategy, copy [examples/signal-config.json](examples/signal-config.json).
Set `strategy_path`, `history_path`, your symbol and `syminfo`, your data source,
and `webhook.target_url`. Paths resolve relative to the configuration file.
Keep the strategy and warmup history fixed for that journal; a changed
strategy/configuration belongs to a new deployment identity.

## Scheduled checks and live feeds

A one-shot check fetches one HTTP response or consumes a finite JSONL file:

```sh
pineforge-live check --config signals.json
```

Schedule that command with cron or your scheduler. Use `run` for a continuous
source. Data providers normalize their messages into a small shared format:

```json
{"type":"tick","ts":1788912001000,"seq":1,"price":2503,"qty":0.5}
{"type":"forming","bar":{"ts_open":1788912000000,"o":2500,"h":2505,"l":2498,"c":2502,"v":12}}
{"type":"bar","bar":{"ts_open":1788912000000,"o":2500,"h":2520,"l":2490,"c":2510,"v":100}}
```

`bar` is confirmed; `forming` is a cumulative, incomplete snapshot. Timestamps
are Unix milliseconds. Script bars must be contiguous and aligned to the
configured timeframe. Tick sequences must be contiguous; the runtime refuses
an unhealed gap. A tick-only feed forms and settles script bars when the next
bucket begins. Use confirmed bar messages when you need closes during quiet
periods. HTTP endpoints return one event or an array; WebSocket frames use the
same shape. No provider-specific subscriptions or credentials are built in.

See [the full feed and webhook contract](docs/webhooks.md) for source settings,
retry behavior, signing, provisional signals and receiver integration.

## Webhook events

Each event carries a stable `event_id` in the JSON body and the HTTP
`Idempotency-Key` / `X-PineForge-Event-Id` headers. A confirmed action looks like:

```json
{
  "schema_version": 1,
  "event": "order_action",
  "event_id": "stable-sha256-event-id",
  "status": "confirmed",
  "strategy": {"name": "my-strategy", "epoch": "strategy-epoch-hash"},
  "instrument": {"venue": "my-feed", "market_type": "perp", "symbol": "ETHUSDT", "ticker": "my-feed:ETHUSDT"},
  "timeframe": "15",
  "timestamp": 1788912900000,
  "bar": {"index": 2001, "time": 1788912000000, "confirmed": true, "open": 2500, "high": 2520, "low": 2490, "close": 2510, "volume": 100},
  "order": {"id": "Long", "action": "buy", "contracts": 1, "price": 2500, "leg": "entry", "reduce_only": false, "close_cause": "UNKNOWN", "path_variant": false, "identity_resolved": true},
  "message": null
}
```

The quantity and reference price come from the engine. The receiver decides
how to map the action to a broker order. A reversal can emit a reduce-only
close followed by a new entry; delivery preserves that sequence.

A timed-out HTTP request may already have been processed, so delivery is
**at least once**. Your receiver must deduplicate `event_id` before trading.
The included receiver demonstrates that pattern. HMAC signs the exact request
bytes using the optional secret selected by `webhook.secret_env`.

For intrabar mode, act only according to your chosen receiver policy:
`order_action/status=provisional` is not yet settled. Later `order_update`
messages reference `original_event_id`; a confirmation is not another buy/sell
instruction. Retractions cannot undo a real trade automatically.

## Inspect and recover

```sh
pineforge-live webhook-inspect --config signals.json
pineforge-live webhook-flush --config signals.json
pineforge-live webhook-retry --config signals.json --event-id EVENT_ID
pineforge-live webhook-skip --config signals.json --event-id EVENT_ID --cause 'handled by receiver operator'
```

A failed head event blocks later delivery until explicitly retried or skipped.
Bodies and IDs remain unchanged on retry. Queue mutations and delivery acquire
the same journal lease as `run`/`check`; an active writer prevents takeover.

Feed or engine inconsistencies retain a STOP marker. After examining the
cause, explicitly clear it with an audit reason:

```sh
pineforge-live journal-inspect path/to/signals.sqlite3
pineforge-live stop-clear path/to/signals.sqlite3 --cause 'verified corrected feed and receiver state'
```

Webhook delivery errors preserve the queued messages. Startup never treats an
HTTP acknowledgment as a fill or invents an account position.

## Verify

```sh
python3 -m pytest
PINEFORGE_ENGINE_ROOT="$PINEFORGE_ENGINE_ROOT" python3 -m pytest
```

Without `PINEFORGE_ENGINE_ROOT`, engine-backed cases skip explicitly. Tests
cover strategy-ledger identity, provisional updates, atomic restart, real
loopback HTTP delivery, HMAC, duplicate delivery, receiver persistence, HTTP
polling and WebSocket frames. The [work ledger](ledger.md) records exact
candidate review and verification results.

The older L1 and mock-execution harnesses remain available for engine/core
research. Their account, reconciler and protection components are not required
by the public webhook runtime; [docs/execution.md](docs/execution.md) records
that earlier development track.

## Project layout

| Path | Purpose |
|---|---|
| `pineforge_live/signals/` | Broker-neutral strategy signal engine and stream/check runtime |
| `pineforge_live/webhooks/` | Immutable outbox, ordered delivery, HMAC and retries |
| `pineforge_live/sources/` | Normalized stdin/JSONL/HTTP/WebSocket feeds |
| `pineforge_live/config.py` | Strategy, metadata, feed and webhook configuration |
| `pineforge_live/engine/` | PineForge ABI binding and full backtest calls |
| `pineforge_live/core/` | Ledger/probe and existing reconciliation research |
| `pineforge_live/journal/` | Durable state, STOP marker and writer fencing |
| `examples/webhook_receiver.py` | Minimal durable receiver for integration |

PineForge engine/codegen feature support and TradingView parity are separate
from webhook delivery. A shared library is executable native code: load only
strategies you trust. Broker risk limits, position sizing adjustments and
execution acknowledgments belong to your receiver or broker bridge.

Licensed under [Apache-2.0](LICENSE), matching the PineForge engine. Contributions
that improve neutral adapters, replay coverage and webhook integrations are
welcome. No external service or PineForge-hosted account is required.
