# pineforge-live

> [!WARNING]
> **Deprecated.** This Python runner is no longer developed. Live trading support now ships in
> [pineforge-engine](https://github.com/pineforge-4pass/pineforge-engine) itself: the optional native
> `pineforge-live` executable (C++17, built with `-DPINEFORGE_BUILD_LIVE_RUNNER=ON`) runs a strategy from
> historical warmup into a realtime tick or bar stream on the same instance, with durable order-action
> webhooks, custom feed parsers and SQLite recovery. Start with its
> [runner guide](https://github.com/pineforge-4pass/pineforge-engine/blob/main/runner/README.md) and the
> [streaming guide](https://github.com/pineforge-4pass/pineforge-engine/blob/main/docs/pages/streaming.md).
> This repository stays readable as a record; it supports engine 0.x only and was never published to PyPI.

Run PineForge-compiled PineScript strategies on your own machine and send
the engine's simulated fill actions to a webhook. Connect the webhook to your
own broker bridge, automation platform or application.

**PineForge Live is broker-neutral.** It needs a compiled PineForge strategy,
market data and a webhook URL. It does not require an exchange account,
choose a broker, or hold broker API keys.

```text
Your market data → PineForge backtest engine → durable order events → your webhook → your broker/automation
```

The strategy engine is C++; the feed/webhook runner is Python. The strategy
ledger is recomputed by the same `run_backtest_full` engine used
for backtests. There is no separate implementation of strategy fill rules.
One production runner accepts ticks or confirmed 1m OHLCV for strategies on
higher timeframes. `mock-feed` generates replay input for the same runner.
The supported PineScript features depend on the engine and compiler versions.

## What works

- `run`: consume JSONL, stdin, HTTP polling or a generic WebSocket feed.
- `check`: process one JSONL/HTTP snapshot and exit, suitable for cron.
- `mock-feed`: convert original 1m OHLCV into direct minute bars or deterministic
  tick paths, using the same input contract as `run`.
- One `order_action` webhook per engine fill, with symbol, side, quantity,
  reference price, Pine order id, bar and strategy identity.
- Durable SQLite decisions and ordered webhook delivery, including restart
  recovery, stable event IDs, bounded retries and optional HMAC signatures.
- `order_update` events to confirm, change or retract provisional intrabar
  signals, without issuing a second action on confirmation.
- Queue inspection, explicit retry/skip, and fenced STOP recovery.
- A working receiver example with transactional event deduplication.

**Status: pre-alpha.** In the default `settled` mode, webhooks report engine
fills after the script bar closes. A modeled next-open or intrabar stop fill
can therefore be reported later than its backtest timestamp; the reference
price is not an executable quote. Pending-order creation, modification and
cancellation are not separate webhook events.

Optional `intrabar` mode emits provisional actions that may later change or
be retracted. HTTP success confirms receipt, not a broker fill. Validate your
actual feed and bridge in paper trading before using real funds. The runner
does not read or reconcile a broker account: even though historical warmup
sends no past actions, it may leave a modeled position that your bridge must
reconcile with the account before submitting new orders.

## Install

Requires Python 3.12+, macOS or Linux, and a compiled strategy exposing
PineForge C ABI v4. Clone this repository and install the Python runtime:

```sh
git clone https://github.com/pineforge-4pass/pineforge-live.git
cd pineforge-live
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For WebSocket feeds, also run `python -m pip install -e '.[websocket]'`.
JSONL, stdin, HTTP and webhook delivery use the Python standard library.
`pineforge-engine` supplies the compiled strategy; this package's runtime
loads it with `ctypes`.

To build the public engine and its example strategy corpus:

```sh
git lfs install
git clone https://github.com/pineforge-4pass/pineforge-engine.git ../pineforge-engine
git -C ../pineforge-engine checkout --detach 399eeadaa34cdbae0e30829f0a6c1dbe900cdfa0
git -C ../pineforge-engine submodule update --init corpus
git -C ../pineforge-engine/corpus lfs pull
export PINEFORGE_ENGINE_ROOT="$(cd ../pineforge-engine && pwd)"
scripts/build_engine.sh
```

Use an ABI-v4 engine build. Two-input probe verification used engine revision
`399eeadaa34cdbae0e30829f0a6c1dbe900cdfa0`. CMake 3.16+, a C++17 compiler and
Git LFS are required; the corpus feed is an LFS object. Initial setup needs network
access for the corpus data and engine build dependencies. The build script
requires `PINEFORGE_ENGINE_ROOT` explicitly and compiles the public corpus,
which can take several minutes. Keep the corpus revision pinned by the engine.

For your own PineScript source, use
[pineforge-codegen-oss](https://github.com/pineforge-4pass/pineforge-codegen-oss)
to generate C++, then compile it against the ABI-v4 engine. The compiler is
a separate source-available project with commercial-use restrictions; read
the [license summary](#license) before using the compiler or its output in a
product or service. The corpus demo uses already-generated C++ and does not
run the PineScript compiler.

## Run a complete local example

In one terminal, set a shared secret and start the example receiver:

```sh
export PINEFORGE_WEBHOOK_SECRET='replace-with-your-own-random-secret'
python examples/webhook_receiver.py --database build/webhook-receipts.sqlite3
```

In another terminal, change to this repository's root, activate the same
virtual environment, and use the same secret:

```sh
. .venv/bin/activate
export PINEFORGE_WEBHOOK_SECRET='replace-with-your-own-random-secret'
python scripts/make_webhook_demo.py \
  --engine-root ../pineforge-engine \
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

Use `run` for a continuous feed and `check` for a finite JSONL file or one HTTP
snapshot. Both resume the same journal:

```sh
pineforge-live check --config signals.json
```

Data providers normalize their ticks or bars into the shared
[feed format](docs/webhooks.md#feed-format). JSONL, stdin, HTTP polling and
WebSocket sources are supported; provider subscriptions and authentication
belong in your feed adapter. Tick-only script-timeframe feeds settle when a
tick starts the next bucket; confirmed boundaries also allow quiet periods
to close. Minute input uses the explicit contract below.

## Trade from ticks or 1m OHLCV

For a strategy running above 1m, add these fields to your configuration:

```json
{
  "script_tf": "15",
  "input_tf": "1",
  "input_mode": "bars",
  "trigger_mode": "settled"
}
```

Keep `history_path` in the strategy timeframe: a 15m strategy warms up from
confirmed 15m history. Send new confirmed 1m bars starting immediately after
that history. The runner updates the forming 15m candle after each minute and
settles it immediately when its final minute closes. The C++ engine then
produces the order actions delivered to your webhook.

Choose `input_mode: "ticks"` for a trade stream. Send positive-quantity ticks
and a confirmed 1m `bar` boundary for each minute. The runner verifies the
boundary against the received ticks and aggregates their OHLCV. Every minute
with positive volume must contain ticks; a missing whole minute cannot be
replaced silently by its boundary. A zero-volume minute sends only its `bar`
boundary. This lets quiet periods close without inventing trades.

The parent open comes from the first minute with positive volume, or the
first minute's open when the parent has none. High, low and close include
every supplied minute, including zero-volume quotes. Volume is summed and
rounded to six decimals. Missing minutes are refused by default.
Each consumed minute, forming state, decision and webhook is committed
atomically. Identical replayed minutes are idempotent; changed rows stop the
runtime. Tick sequences must remain contiguous across restart.

Omitting `input_tf` preserves the original script-timeframe bar/tick contract.
`input_mode: "mixed"` is the default for compatibility; use the explicit
`bars` or `ticks` mode when verifying either input independently.

Sparse 1m sources can explicitly choose `input_gap_policy: "observed"` and
`mock-feed --gap-policy observed`. Only supplied rows are aggregated; no
prices or volume are invented. Parent closing boundaries remain required.
Keep the default `"reject"` for feeds promising every active minute.

### Scripts that request another timeframe

For a strategy such as a 1D script that calls `request.security()` on 15m,
add `"auxiliary_history_path": "history-1m.csv"`. This immutable 1m warmup must
cover the script history and end before live input. Both feed modes extend it
with observed minutes through the same runner, including after restart. C++
continues to execute and fill orders on native script bars while its security
queries read the auxiliary feed. See the [feed contract](docs/webhooks.md) for
history boundaries and the separate `request.security_lower_tf()` limitation.

## Replay your own 1m data

Generate bars or deterministic ticks from the same CSV, then feed the result
to `run` with the corresponding `input_mode`:

```sh
pineforge-live mock-feed minutes.csv --input-mode bars --output minutes.jsonl
pineforge-live mock-feed minutes.csv --input-mode ticks --policy high-first --output ticks.jsonl
```

The CSV columns are `timestamp,open,high,low,close,volume`, with opening times
in Unix milliseconds. Configure `source` as a JSONL file or stdin. Mock ticks
are synthetic paths through each minute's OHLC, not recovered exchange ticks.
See the [replay guide](docs/replay.md) for low-first/seeded paths, time ranges,
stdin, restart rules and examples.

## Exchange sessions and daily candles

For non-UTC boundaries, holidays or daylight-saving changes, supply an
explicit `parent_windows_path` calendar covering warmup and live input. Use
the same calendar with `mock-feed`. It declares parent opens/closes and can
separate a daily label from its first active minute. Unknown or incomplete
closing coverage must not be silently treated as a finished candle.
See [session calendars](docs/replay.md#exchange-sessions-and-daily-candles).

## Webhook events

Each event carries a stable `event_id`, strategy/instrument identity, bar
metadata and an `order` with `id`, `action`, `contracts`, `price`, `leg` and
`reduce_only`. The same ID appears in the HTTP `Idempotency-Key` and
`X-PineForge-Event-Id` headers. See the
[versioned event schema and full example](docs/webhooks.md#event-contract-version-1).

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

The [two-input verification report](docs/two-input-verification.md) records
**35/35 selected campaign probes passing** direct 1m OHLCV and both synthetic
tick paths through the same runner: 201 actual HTTP order-action deliveries,
zero duplicate IDs and zero restart deliveries. Each tick path processed
122,092 trade ticks. All probes matched C++ batch on identical reconstructed
input; 27 also matched native chart OHLCV, while 8 had source-data differences.
The report pins the tested code, inputs, reviews and artifact hashes.

```sh
python -m pip install -e '.[dev]'
env -u PINEFORGE_ENGINE_ROOT python -m pytest
PINEFORGE_ENGINE_ROOT=../pineforge-engine python -m pytest
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

## Contributing

Bug reports, documentation fixes, feed adapters and replay cases are welcome.
Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, PR scope and
contribution licensing. The [changelog](CHANGELOG.md) tracks release changes.
Maintainer campaign tooling is separate; cloud access is not required to use
the runtime or submit a contribution.

## Security reports

Use GitHub's [private vulnerability reporting](https://github.com/pineforge-4pass/pineforge-live/security/advisories/new)
for security-sensitive findings. Include the affected version, impact and a
minimal reproduction with sensitive values removed. Keep exploit details,
credentials and account information out of public issues.

## License

This repository is licensed under [Apache License 2.0](LICENSE). It permits
personal and commercial use, modification and redistribution, including use
inside proprietary products, subject to its license and [notice](NOTICE) requirements.
It does not require a PineForge-hosted account or service.

The components in a PineForge setup have separate licenses:

| Component | License and scope |
|---|---|
| **pineforge-live** | [Apache-2.0](LICENSE): this Python runtime, its examples and documentation. |
| **pineforge-engine** | [Apache-2.0](https://github.com/pineforge-4pass/pineforge-engine/blob/main/LICENSE): the C++ engine, distributed separately. |
| **pineforge-codegen-oss** | [PolyForm Noncommercial 1.0.0 with supplemental terms](https://github.com/pineforge-4pass/pineforge-codegen-oss/blob/main/LICENSE): source-available, **not OSI open source**. Personal own-account/own-capital trading is permitted. Organizational use, managing third-party capital, embedding the compiler or its output in a product/service, and hosted/SaaS/public-facing use require a commercial license under those terms. |

The runtime's Apache license does not grant rights to the compiler, generated
strategies, third-party strategy sources or market data. Review codegen's
[legal information](https://github.com/pineforge-4pass/pineforge-codegen-oss/blob/main/LEGAL.md)
and the applicable source/data licenses for your use. The optional `websockets`
package is distributed separately under its own BSD license.

PineScript and TradingView names identify compatibility with their products.
PineForge Live is not affiliated with, endorsed by or certified by TradingView.
