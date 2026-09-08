# Strategy alerts over webhooks

PineForge Live runs a compiled PineScript strategy and sends its order actions
to a webhook URL. The receiver can be your own service, automation platform,
or broker bridge. You supply the market data and choose the receiver; the
public signal runtime does not require an exchange account.

The C++ engine calculates strategy orders. The Python runner receives data,
calls the engine through its C ABI, journals decisions, and sends webhooks.
The webhook reports the engine's orders.
An HTTP success records delivery to the receiver; it says nothing about an
actual trade, fill price, or account position.

## Local receiver

Install the package with Python 3.12 or newer:

```sh
python -m pip install -e .
```

Choose an HMAC secret and export it in the shells running both the sender and
receiver. Keep the value in the environment, outside the configuration file.
Start the included receiver:

```sh
python examples/webhook_receiver.py --database ./webhook-receipts.sqlite3
```

It listens at `http://127.0.0.1:8765/webhook`. When
`PINEFORGE_WEBHOOK_SECRET` is set, it requires valid HMAC signatures. An unset
variable disables signature verification for local experiments; an empty
configured secret is refused. Use `--secret-env NAME` to select another
variable. `--bind` and `--port` change the listen address.

The receiver prints each newly recorded event and stores it in SQLite. It
does not connect to a broker. Console output is advisory; the SQLite receipt
is the durable record and is written before the HTTP acknowledgment.

For a receiver exposed beyond localhost, use HTTPS and a configured secret.
The Python example is intended as a small integration reference, with TLS
terminated by your web server or hosting platform.

## Configuration

Copy [signal-config.json](../examples/signal-config.json) and set these fields:

| Field | What to supply |
|---|---|
| `strategy_path` | Your compiled PineForge strategy shared library (`.so` or `.dylib`) |
| `strategy_name` | The display name included in every event |
| `history_path` | Contiguous confirmed bars used to initialize the strategy |
| `journal_path` | A persistent SQLite file for decisions and webhook delivery |
| `script_tf` | The strategy timeframe, such as `"15"` or `"1D"` |
| `instrument` | Your data source name, market type and symbol |
| `syminfo` | Metadata matching the instrument used to compile/test the strategy |
| `inputs`, `overrides` | Ordered string key/value pairs applied to the engine |
| `webhook.target_url` | The receiving service's endpoint |
| `webhook.secret_env` | The environment variable containing the shared HMAC secret, or `null` |
| `source` | The source of new bars and updates |

Paths resolve relative to the configuration file. The example metadata is for
an illustrative ETHUSDT feed and must be changed to match your instrument.
The shared library and historical bars are user-supplied files. No strategy
or market data is downloaded by this configuration.

Historical CSV columns are:

```csv
timestamp,open,high,low,close,volume
```

`timestamp` is the bar's opening time in Unix milliseconds. History contains
confirmed bars at `script_tf`, in chronological order without gaps. It warms
up the engine; starting a new journal does not send past history's orders.
On restart, keep the same journal and the same historical prefix so the
runtime can verify and restore its checkpoint.

The default `trigger_mode` is `"settled"`: actions come from confirmed engine
fills after each bar closes. `"intrabar"` enables provisional actions from a
forming-bar probe and requires the receiver to process later update events.

After saving your configured file, run the sender in a second terminal. With
the example's `stdin` source, a JSONL file can provide the input:

```sh
pineforge-live run --config ./signal-config.json < ./live-bars.jsonl
```

For a persistent feed, configure `websocket` or `http` as below and run the
same command without input redirection. Install WebSocket support when using
that source:

```sh
python -m pip install -e '.[websocket]'
pineforge-live run --config ./signal-config.json
```

For a scheduled invocation, configure a `jsonl` or `http` source, then use:

```sh
pineforge-live check --config ./signal-config.json
```

`check` processes one finite snapshot, attempts queued deliveries, and exits.
`run` consumes finite JSONL/stdin input until EOF. HTTP polling and WebSocket
feeds continue until interrupted; WebSocket connections reconnect even after
a normal server close. Both commands resume the same journal. Each command prints a JSON report and returns a
nonzero status when work remains unresolved; a report is also saved beside
the journal with the suffix `.report.json`.

## Feed format

The feed is a small, broker-neutral JSON contract. An adapter for your data
provider should normalize its bars or ticks into these events. The sample
configuration reads one JSON frame per line from standard input:

```json
{"type":"forming","bar":{"ts_open":1788912000000,"o":2500,"h":2505,"l":2498,"c":2502,"v":12}}
{"type":"tick","ts":1788912001000,"seq":1,"price":2503,"qty":0.5}
{"type":"bar","bar":{"ts_open":1788912000000,"o":2500,"h":2520,"l":2490,"c":2510,"v":100}}
```

`bar` means a confirmed bar; `forming` means the current incomplete bar.
Opening timestamps must align to the configured timeframe, and each new
confirmed bar must follow the preceding one. A forming bar must immediately
follow the latest confirmed bar. Tick sequence numbers must be contiguous (`last + 1`); exact duplicates may
be replayed on reconnect. A sequence hole is an unhealed gap and stops the
runner. `trade_count` is optional on
bars and defaults to zero.

Other source configurations are:

```json
{"kind":"jsonl","path":"./live-bars.jsonl"}
```

```json
{"kind":"websocket","url":"wss://your-feed.example/events"}
```

```json
{"kind":"http","url":"https://your-feed.example/events","poll_interval_ms":1000}
```

WebSocket text frames and HTTP JSON responses use the same event format;
a frame may also contain an array of events. These URLs refer to your
normalized feed service, whose response must follow this contract. Raw
exchange message formats need a small adapter on the feed side.

## Event contract, version 1

A confirmed order action looks like this:

```json
{
  "schema_version": 1,
  "event": "order_action",
  "event_id": "example-event-001",
  "status": "confirmed",
  "strategy": {"name": "my-strategy", "epoch": "example-epoch"},
  "instrument": {
    "venue": "provided-feed",
    "market_type": "perp",
    "symbol": "ETHUSDT",
    "ticker": "provided-feed:ETHUSDT"
  },
  "timeframe": "15",
  "timestamp": 1788912900000,
  "bar": {
    "index": 100,
    "time": 1788912000000,
    "confirmed": true,
    "open": 2500,
    "high": 2520,
    "low": 2490,
    "close": 2510,
    "volume": 100
  },
  "order": {
    "id": "Long",
    "action": "buy",
    "contracts": 1,
    "price": 2500,
    "leg": "entry",
    "reduce_only": false,
    "close_cause": "UNKNOWN",
    "path_variant": false,
    "identity_resolved": true
  },
  "message": null
}
```

`event_id` identifies one immutable event and stays unchanged across retries
and process restarts. `strategy.epoch` identifies the strategy configuration.
`bar.time` is the opening timestamp; `timestamp` is the event's observation
time. Both use Unix milliseconds.

`order.action` is `buy` or `sell`; `order.contracts` is a positive quantity in
the engine's instrument units. `leg` distinguishes entry from exit and
`reduce_only` marks an exit. `order.price` is the engine's reference price,
not a broker price or a guarantee that the receiver can obtain it. Closed
trade legs carry the engine's recorded fill price. A still-open position
delta uses the engine's resulting average entry price; with pyramiding that
can be a blended basis rather than the incremental order's individual price.
When the Pine order ID cannot be uniquely resolved, `id` is `null` and
`identity_resolved` is `false`.

With intrabar alerts enabled, the first event has `status: "provisional"`.
Later events use `event: "order_update"`, include `original_event_id`, and
carry one of these statuses:

| Status | Meaning |
|---|---|
| `confirmed` | The confirmed bar contains the previously reported order |
| `changed` | The order's calculated details changed, or it reappeared |
| `retracted` | The order disappeared from the current calculation |

An update is a notification about the original event. It is never a second
instruction to execute the same buy or sell. An intrabar retraction cannot
undo a real trade; a receiver opting into provisional execution must define
how it handles those changes. The sample receiver stores both event kinds.

## Delivery and verification

Requests use HTTP POST with these headers:

```text
Content-Type: application/json
X-PineForge-Event-Id: <event_id>
Idempotency-Key: <event_id>
X-PineForge-Signature: sha256=<hex digest>
```

The signature header is present when a secret is configured. Compute
`HMAC-SHA256(secret_utf8_bytes, raw_http_body_bytes)` and compare the digest
in constant time. Verify the received bytes before parsing JSON; reformatting
JSON changes the signature. The example receiver also verifies that either
identity header, if supplied, agrees with the body.

Delivery is **at least once**. An endpoint can commit an event and lose its
HTTP response, so the sender must retry the same ID and payload. Store the
ID and the accepted payload in one transaction before acknowledging it.
The example returns:

| Response | Meaning |
|---|---|
| `200` | Receipt durably recorded, or an identical duplicate already exists |
| `400` | Invalid event or mismatched identity header |
| `401` | Signature missing or invalid |
| `409` | An existing ID was reused for different payload bytes |
| `503` | Receipt could not be committed; retry later |

The sender keeps event order across retries and persists delivery attempts.
A failed event prevents newer actions overtaking it. A delivery success is
an HTTP 2xx acknowledgment only; response bodies do not establish broker
execution. Redirects are not followed.

Inspect the durable queue with:

```sh
pineforge-live webhook-inspect --config ./signal-config.json
```

After stopping an active sender, flush due deliveries without consuming new
market data:

```sh
pineforge-live webhook-flush --config ./signal-config.json
```

A permanent HTTP error or an exhausted retry budget leaves a failed event at
the head of the queue. Correct the receiver, then retry the event ID shown
by inspection:

```sh
pineforge-live webhook-retry --config ./signal-config.json --event-id EVENT_ID
```

`webhook-skip --config ./signal-config.json --event-id EVENT_ID --cause REASON`
records an explicit decision to abandon that delivery and allows later events
to proceed. Use the actual ID and your reason; checking the receiver first
establishes whether the original request was already accepted. Retry and skip
preserve the delivery audit instead of rewriting old payloads.

To turn receipts into real orders, add a durable worker that reads unprocessed
receipts and uses `event_id` as its downstream idempotency key. Persisting a
receipt and calling an external broker are separate operations: marking a
receipt handled before a broker call can lose an order, and marking it after
an unidentifiable call can duplicate it on restart. Use the downstream
service's idempotency/adoption mechanism and record its result. Keep
`order_update` handling separate from new-order submission.

## Verify the example

```sh
python -m pytest tests/test_webhook_receiver.py
```

These tests use real loopback HTTP and cover signatures, malformed requests,
concurrent duplicates, conflicting payloads, SQLite failure, and restart
after a subprocess exits with a committed receipt before acknowledgment.
They exercise durable receipt handling, without placing any trades.

## Runtime ownership and computation

One fenced runner owns each journal. C++ recomputations run on a dedicated
worker thread; feed input and lease renewal remain on the event loop.
Ledger/diagnostic writes are staged during computation, then committed with
the webhook events and checkpoint in one short transaction. Writer authority
is checked again at commit and immediately before each HTTP request.

Tick-built forming state, last tick identity and evaluation cadence survive
restart. Increasing sequence numbers cannot carry an older timestamp. A final
confirmed bar can grow beyond an earlier forming snapshot; a complete
bar built from ticks is compared against a matching confirmed bar.

`webhook-inspect`, `webhook-flush`, `webhook-retry` and `webhook-skip` use the
stored queue epoch even if the strategy library/history files were moved or
rebuilt. If a journal holds multiple deployments, select `--epoch HASH`.
These commands read only the webhook and journal settings from the config;
they never need to load or execute the strategy to recover delivery.

Runtime output and sidecar paths must not overlap configuration or input
files. Relative source paths stay relative in deployment identity, so moving
a configuration tree together with its journal preserves IDs and resumes
without replaying actions. Keep the stored webhook URL unchanged for queue
recovery. Volume-only forming updates participate in the evaluation cadence.
