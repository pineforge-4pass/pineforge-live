# Two-input verification — 2026-09-09

**35 of 35 selected probes passed all three feed variants on Cloud Run.**
Each case produced order actions through the public `run_signals` runtime and
matched a C++ batch control using identical history and reconstructed script
bars. This verifies the two input modes through one production runner.
`mock-feed` generates data; it does not implement another strategy engine.

| Input variant | Passed probes | Generated trade ticks | Actual HTTP order actions | Restart deliveries |
|---|---:|---:|---:|---:|
| 1m OHLCV bars | 35 | 0 | 67 | 0 |
| Ticks, high-first | 35 | 122,092 | 67 | 0 |
| Ticks, low-first | 35 | 122,092 | 67 | 0 |

There were **201 actual HTTP order-action deliveries**, zero duplicate event
IDs within each run, zero failures and zero unmeasured cases. Every tick
variant contained positive-quantity trade ticks. The receiver ran on loopback
inside Cloud Run; it did not place broker orders.

The [machine-readable results](verification/two-input-2026-09-09.json) contain
all 35 probe IDs, per-mode action/tick counts, original source hashes,
calendar hashes and generation-pinned case artifact receipts.

## Exact candidate and inputs

- Live code: `8356c15aebf19dcb88427baf709b237e31b5744e`.
- C++ engine: `399eeadaa34cdbae0e30829f0a6c1dbe900cdfa0`.
- Codegen: `0fe2189f2cb845cc7371ce56dd55ad9cff72dda0`.
- Image: `sha256:a8ed3eb03cf87ae645e98e1f199d363b90a446de1a5e4bc43102d7bdb20ff480`.
- Run: `two-input-20260909-a7`.
- Execution: `pineforge-live-review-two-input-20260909-a7-4tgrv`.
- Run manifest: `8764059fee535c8dfd509f3a28b81e4eb3d6e63c5a7f6ecefa2dd17f9e72daa0`.

The selection was frozen from a read-only Postgres campaign export: two probes
per lane/group with seed `20260909`, plus SMA, bracket and orders-on-close
regressions. It covers 35 of the 4,190 probes recorded at the time, across
nine symbols.
Probe IDs, original source/CSV bytes, strategy inputs, engine and compiler
versions remained fixed through the verification attempts.

The experiment used `input_gap_policy: "observed"` explicitly because some
archives are sparse. Missing rows were not padded. The default production
policy remains `reject`. Replays covered 16 intraday or two daily script bars,
retaining every native warmup bar from the effective shared chart/minute
origin. Selection required closing-minute coverage and at least one original
positive-volume minute, so a quote-only file could not count as a tick test.

## What was checked

All four task results, all 35 case archives, all 105 received-event lists and
all 35 calendar provenance reports were read back and checked. Original
strategy source hashes also matched the registry identities.

Each variant checked every settlement's broker-state hash, the final
closed-trade digest, ordered action IDs/directions/prices/quantities, raw C++
exit quantities and position delta. It restarted after two input events,
finished the replay, then restarted again and delivered zero further actions.
The independent parent-bar projection and actual HTTP payloads agreed.

Session closes came from independent timeframe/session boundaries and IANA
timezones. Unknown special-session closes were excluded from replay and
retained in native warmup. No selected closing boundary was shortened to the
last surviving minute. Auxiliary security data used original minute warmup
plus observed live minutes; calls proven to use only the chart timeframe
used native chart data directly.

## Scope of the result

This is a bounded test of settled live/batch equivalence. It is not a new
4,190-probe campaign grade, gate, latency benchmark or guarantee of broker
execution prices. The campaign's optimizer/warmup ladder, native higher
timeframe feed selection and TradingView report-window projection were not
rerun. Campaign jobs, database rows and baseline were not changed.

Reconstructed bars equaled the original native chart for **27 probes**; the
other **8 had source OHLCV differences**. All 35 matched C++ batch on their
identical reconstructed input. Matching native bars also required matching
the state prefix of the native control from the effective shared origin.
That origin may be later than the original chart; the NQ one-time-entry case
therefore proves a fresh available-data epoch, not a pre-archive position.

Synthetic high-first/low-first ticks do not recover historical exchange tick
order. Minute tick mode includes explicit confirmed 1m boundaries. Regular
`request.security()` auxiliary support does not provide native lower-timeframe
intrabar arrays to `request.security_lower_tf()`.

Local validation at the same code commit: **950 tests passed; 151 engine
fixtures explicitly skipped**. Real probe execution occurred on Cloud Run
(Python 3.12, SQLite 3.40.1). An installed wheel was also tested outside the
source checkout for both input modes. Independent Grok review was **GREEN,
P0/P1/P2 = 0** on the exact code commit above.

Earlier attempts remain evidence, including failed and unmeasured cases.
Attempt 6 reported 33 passes, but two NIFTY intraday inputs contained only
minute boundaries and established no trade-tick coverage. This final run
requires real ticks and replaces those insufficient claims. Full private
readbacks are retained under `build/two-input-review/attempt-7/`; the public
JSON records their hashes without requiring cloud access to run PineForge.

## Use the runner

Follow the [README](../README.md#trade-from-ticks-or-1m-ohlcv) for `input_tf`,
feed modes and calendars, and the [webhook contract](webhooks.md) for receiver
integration. The C++ engine owns strategy behavior; the Python runner owns
input, durable state and delivery. Connect the webhook to your chosen bridge
or automation service. No broker account or cloud service is required by this
runtime.
