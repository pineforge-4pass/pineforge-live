# pineforge-live

A live-trading runtime for PineScript v6 strategies compiled by PineForge. Its
live ledger *is* the backtest: at every script-bar close the runtime recomputes
`run_backtest_full` over the whole bar history and treats the result as the
authoritative position and trade list. Between closes it probes the forming bar
with the engine's own intrabar flags, classifies every venue fill against what
the ledger emulated, reconciles within explicit bounds, and escalates to durable
STOP levels when it cannot. The property it is built around: **there is exactly
one implementation of the strategy's fill rules — the backtest engine — and the
live runtime never links or calls another fill-deciding entry point.**

## Status

> **Pre-alpha. Nothing in this repository places real orders. Do not trade with it.**

Implemented and tested:

- **the foundation** — engine binding (C ABI v4), bar policy and forming-bar
  builder, durable SQLite journal with a stop marker and a fenced lease, tape
  adapters;
- **the core** — recompute ledger, intrabar probe, settled book, fill
  classification, bounded reconciler, RiskGuard/STOP, and the `LiveCore` facade.

**Local execution is implemented against a deterministic mock venue.** A
SQLite outbox assigns durable physical-order identities; the coordinator
submits, adopts timed-out orders, deduplicates fills and records execution
receipts. `DurableCore` commits core decisions, their outbox, and restart
state together. The `execution-replay` command exercises this path in stream
and check schedules, including runtime recreation.

**Exchange adapters and live operating loops are unfinished.** The replay is
follow-only and never reaches an exchange. Risk validation, protective-order
planning and emergency rate reserves have tested components, but live account
inputs, mirror replacement, dead-man arm deadlines, DISASTER handling and
production admission are not wired. There is no live daily-loss guarantee.

Current execution boundaries:

- A timeout after acceptance is adopted through the durable client/order
  mapping. An absent ambiguous order remains `UNKNOWN`; the current adapter
  protocol cannot prove exhaustive absence, so it is not blindly resubmitted.
- Settle-emitted corrections and orders-on-close produce **action receipts**.
  Their quantities update the real-position basis once; they are not invented
  engine fills for the following bar. Late ledger receipts and terminal
  residuals stop the offline driver for explicit reconciliation.
- `MARKET_AT_OPEN` stays a notice until a successful evaluation confirms the
  target bar's open. A requote or withdrawal is committed before submission.
- Atomic recovery applies to the `DurableCore` path. Calling `LiveCore`
  directly still requires a caller-owned durability design. Schema-v1 journals
  are preserved and refused before mutation; new runs use schema v2.

What you can do today: run the engine-backed tests, the existing L1 harness,
and the [offline execution replay](docs/execution.md). The [B3 plan](docs/plan-b3.md)
and [active ledger](ledger.md) distinguish implemented components from the
remaining live integration and evidence gates.

## Why

The usual failure of live PineScript deployment is drift: the live executor
re-implements the strategy's fill logic, or approximates it, and the live result
stops resembling the backtest. pineforge-live removes that second implementation
rather than trying to keep it in sync.

That removes **one** source of drift. It does not make live P&L equal a
backtest: real fills, slippage, partial fills, fees, funding, latency and every
case the reconciler handles by correction or STOP all still move the account.
And "the backtest" here means *pineforge-engine's* result. How closely the
engine reproduces TradingView is a separate project's measurement, reported in
that repository, not claimed here.

- **Recompute ledger.** At each script-bar close the runtime calls
  `run_backtest_full(bars[history_start..n])` on a fresh strategy handle. The
  trades and position that come back are the ledger; nothing else is.
- **Probe on the forming bar.** Between closes the same function runs over
  `bars[..n] + forming_bar` with the engine's *tail logic suppressed* (an ABI v4
  flag that turns off end-of-history behavior) and an explicit *path order* (the
  order in which the engine walks a bar's high and low). An entry is acted on
  only when both path orders agree; a closing fill the two paths disagree on is
  still emitted, tagged `PATH_DIVERGENT`.
- **Fill classes.** Every venue fill is matched to an emulated fill and given
  one of twelve classes (`CONFIRMED`, `MISSED`, `IN_FLIGHT`, `SETTLE_ONLY`,
  `SYNTHETIC`, `QTY_DIVERGENT`, `PATH_DIVERGENT`, `MIRROR_EARLY`,
  `TRIGGER_REVERSED`, `ENTRY_SLIP`, `RETRACTED`, `UNATTRIBUTED_VENUE`), each
  mapping to a fixed reconciler response.
- **Bounded reconciliation.** A missed fill is corrected once, within an age and
  price-distance bound and a notional budget; otherwise the cycle is journaled
  as skipped. Those corrective orders — `MARKET_CORRECT`, `REDUCE_ONLY_TRIM`,
  `TOP_UP`, `FLATTEN` — are a second rule set the backtest never emulated, which
  is why every one of them is bounded.
- **Escalation, not a third attempt.** `disagree_twice` consecutive settlements
  skipped as not quiescent raise `STOP(FLAT_ONLY)`, and so does exhausting the
  epoch's bar horizon. The daily reconcile cap (`max_daily_reconciles`) is
  different: it *refuses* the correction and journals an incident plus a skipped
  cycle, with no STOP.
- **G1 / G3 gates.** G1 checks that every recompute reproduces the previously
  journaled trade prefix and broker-state hashes (a violation is a HARD STOP).
  G3 runs rate breakers over reconciler counters.
- **STOP levels, durable.** `FLAT_ONLY` forbids exposure-increasing orders;
  `HARD` additionally forbids new reduce-only orders except a dead-man, a
  re-established static exit, or one flatten. A STOP is written to an
  out-of-band marker file, then the journal, then memory — so a journal fault
  still stops the process.
- **Venue-neutral core.** The core imports no venue names. Adapters implement
  frozen `Protocol`s over neutral dataclasses; tests and the harness use the
  literal venue `"TAPE"`.

## How it works

Terms this README and the code use in a specific sense:

| term | meaning |
|---|---|
| epoch | one deployment identity: venue, instrument, script timeframe, `history_start_ms`, `horizon_bars`, code identity (engine/codegen/source digests and a build receipt carrying the loaded library's sha256), syminfo and policy knobs, hashed into `epoch_hash`. Every journal row is keyed by it. `RuntimeConfig` is hashed separately, so tuning does not start a new epoch; `horizon_bars` is the last bar index the epoch may settle |
| script bar | a bar of the strategy's own timeframe. Buckets are 24×7 UTC, weeks Monday-anchored; the timeframe grammar is ASCII digits meaning minutes, with an optional `D`/`W` suffix (`"15"`, `"1D"`, `"W"`) |
| bar policy | how a bucket becomes a bar: the forming bar opens at the first print, a missing bucket carries the previous close forward with zero volume. The version string `v1:first-print-open/carry-forward-zero-volume` is part of the epoch hash, as is `adapter-api`, the adapter protocol version |
| tape | a recorded feed, a `timestamp,open,high,low,close,volume` CSV with millisecond timestamps, replayed by `adapters/tape.py`. The clock only advances to timestamps the tape contains, so a run is reproducible. `TAPE` is also the placeholder venue name |
| corpus strategy | one validation directory in pineforge-corpus (`strategy.pine`, the compiled `strategy.dylib`/`.so`, `inputs.json`, TradingView and engine trade lists). The three the harness runs are corpus strategies |
| intrabar probe | the forming-bar recompute in `core/probe.py` — unrelated to a corpus strategy despite the shared word in campaign usage |
| settled book | the set of orders resting after a settlement, each an `intent` (a Pine order id) with a `leg` that is `"EXIT"` exactly when the order is reduce-only |
| partition 1 / partition 3 | the engine's sizing vocabulary: a fixed quantity, versus a quantity resolved at the fill price (percent-of-equity and similar) |

`LiveCore` has three entry points: `seed` once at epoch open, then `settle` and
`evaluate`, driven by whoever owns the bar and tick streams (today the L1
harness and the offline execution replay).

### Settle — once per confirmed bar

1. Refuse if the epoch's `horizon_bars` is exhausted (`STOP(FLAT_ONLY, "horizon")`).
2. Recompute the ledger on the bar. A revised bar raises `bars_divergence`; a
   non-contiguous bar raises `LedgerGap` to the caller; an aborted recompute is
   an incident, not a STOP — the bar is not journaled and must be re-delivered,
   and delivering the next bar instead raises `LedgerGap`.
3. Run G1 against the previous settlement's journaled prefix, broker-state hash,
   trades digest and bars hash. A mismatch is `STOP(HARD, HOLD, "g1:<cause>")`.
4. Journal the bar and the settlement row (checksummed), diff the settled book
   against the previous one.
5. Classify this bar's emulated fills against the venue fills you pass in.
6. Reconcile: apply the class table, the account-position check and the four
   bounds; raise whatever STOP the table demands.
7. Build `ActionRequest`s — after the escalation, never before — gate each
   through STOP and the RiskGuard budgets, journal the intent transitions and a
   reconcile row, return `CoreOutput`.

### Evaluate — on every tick of the forming bar

1. Probe with `path_order=AUTO`; if it fills, probe again with the other order.
2. A fill on both paths becomes a `TRIGGER`. A closing fill that differs between
   paths is emitted as path-variant; an entry that differs is deferred to
   settle. A fill present on a previous evaluate and gone now is journaled as a
   retract, never a STOP.
3. Refresh exit levels under `trail_refresh_policy` (`bar_open_level` or
   `intrabar_best`).
4. On the first evaluate of a bar, supersede settle's `MARKET_AT_OPEN` notice:
   re-quote it, leave it, or withdraw it with `qty=0`. One `TRIGGER` per
   `(intent, leg)` per bar.

### ActionRequest kinds

`ActionRequest` is the unit of output B3 will consume. `intent` is the Pine
order id; `qty` is never negative (`side` carries direction) and is `0` only for
`CANCEL_STALE_CYCLE` and the `qty=0` withdraw supersede; the supersede key is
`(intent, leg, target_bar_index)`.

| kind | emitted by | meaning |
|---|---|---|
| `TRIGGER` | evaluate | intrabar fill confirmed on both path orders; `price_hint` is the probe's fill price |
| `MARKET_AT_OPEN` | settle (as a notice) and evaluate (as a supersede) | a MARKET order resting in the settled book that fills at the next open; a reversal is two legs |
| `MARKET_NOW` | settle | a `process_orders_on_close` fill; take the venue to the ledger position now |
| `SYNTHETIC_CLOSE` | settle | engine-forced close (margin call, intraday cap); reduce-only |
| `CORRECTION` | settle | the reconciler's `MARKET_CORRECT` / `REDUCE_ONLY_TRIM` / `TOP_UP` |
| `FLATTEN` | settle | reduce real exposure to zero; budgeted from the reconciler, exempt when it is a STOP's `HARD_FLAT` |
| `CANCEL_STALE_CYCLE` | settle and evaluate | an intent left the settled book unfilled, or a refused requote is withdrawn; `qty=0` |

[`docs/core.md`](docs/core.md) is the long-form description: the invariants,
every settle and evaluate step, the full class and reconcile table, the STOP
semantics, a module-responsibility table, and the inputs B3 has to supply. It
cites a design spec by section number (§1 invariants, §2 epoch and bar policy,
§4 the settle/evaluate algorithm, §5.4 the reconciler, §5.5 STOP/RiskGuard, §6
journal and restart, §10.2 the L1 assertions). That spec is not public yet — the
map above is what those numbers mean until it is published.

## Requirements

- **Python >= 3.12** for this package. No runtime dependencies; the engine
  binding is `ctypes`. `pytest >= 8` for development.
- **macOS or Linux.** Library loading is plain `ctypes.CDLL`; only the file
  suffix differs (`strategy.dylib` / `strategy.so`).
- **A built `pineforge-engine` checkout exposing C ABI v4**, with its `corpus`
  submodule fetched and built. No tagged engine release exposes ABI v4 yet — up
  to and including v0.13.1 the header declares ABI 3, and `engine-info` and the
  engine-backed tests will refuse it. Use the engine's `main`.
- **To build the engine:** `git`, `git-lfs` (the corpus's 1-minute OHLCV feed is
  a ~176 MB LFS object; without LFS the checkout is a pointer file and the
  feed-derivation step aborts), CMake >= 3.16, a C++17 compiler (GCC >= 9,
  Clang >= 10, Apple Clang >= 12 — `xcode-select --install` on macOS,
  `apt install build-essential cmake git-lfs` on Debian/Ubuntu), and network
  access, since the engine fetches Eigen 3.3+ through CMake when it is absent.
- **`PINEFORGE_ENGINE_ROOT`** pointing at that checkout. The tests skip every
  engine-backed case when it is unset, and the harness cannot run at all.

## Quickstart

Everything below runs from this repository's root. `python3` must be >= 3.12;
check with `python3 --version`, and use a virtual environment — a system Python
on macOS is often 3.9, and Debian/Ubuntu and Homebrew Pythons refuse a global
`pip install` outright.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

### Build the engine

```sh
git lfs install
git clone https://github.com/pineforge-4pass/pineforge-engine.git ../pineforge-engine
export PINEFORGE_ENGINE_ROOT="$(cd ../pineforge-engine && pwd)"   # main; ABI v4
scripts/build_engine.sh
```

`scripts/build_engine.sh` requires a `pineforge-engine` checkout with a
`CMakeLists.txt` (it exits 2 otherwise) and a working CMake toolchain. It reads
`PINEFORGE_ENGINE_ROOT`; it exits 2 with a setup message when that variable
is unset. There is no machine-specific default. The script initializes the `corpus`
submodule if needed, runs the engine's `scripts/run_corpus.sh` with
`SKIP_RUN=1 SKIP_VERIFY=1` and `JOBS=8` (override `JOBS` on a small machine —
these are Eigen-heavy C++ targets), reverts the `validation_report.md` the build
rewrites inside the corpus submodule, asserts that
`corpus/validation/ta-sma-152-close-cross-01/strategy.*` exists, and prints
`engine ready: <root> (<short sha>)`.

Two things to expect: this is not only a build — the run also derives the 15-minute
feed from the 176 MB 1-minute CSV — and it compiles **every** strategy in the
corpus (300+), not the three this repository uses, so budget tens of minutes.
Take the corpus at the commit the engine pins; never `git submodule update --remote`.

The `export` does not survive a new shell. If you reopen a terminal and the
tests suddenly skip engine-backed cases, that is the missing variable, not a broken build.

### Tests

Without an engine — unit tests only, engine-backed cases skipped:

```sh
python3 -m pytest -o addopts=""
# 522 passed, 122 skipped (2026-09-09 local candidate)
```

With an engine:

```sh
python3 -m pytest -o addopts=""   # with PINEFORGE_ENGINE_ROOT exported
# 644 passed (2026-09-09 local candidate)
```

`pyproject.toml` sets `addopts = "-q"`; `-o addopts=""` turns quiet mode off so
you get the session header, per-file progress and the decorated summary. The
engine-backed suite takes about 25 seconds on one core of a 2024 macOS laptop.
The counts above are what this tree produces today and will move with the
next test file.

### CLI

Set `LIB=strategy.dylib` on macOS, `LIB=strategy.so` on Linux, and
`V=$PINEFORGE_ENGINE_ROOT/corpus/validation`.

```sh
pineforge-live version
# pineforge-live 0.1.0 adapter-api 1 bar-policy v1:first-print-open/carry-forward-zero-volume

pineforge-live engine-info "$V/ta-sma-152-close-cross-01/$LIB"
# abi 4
# version 0.13.1-100-g41c9c74
# exports 24/24
# pending_order_fields 108 (884 bytes)

pineforge-live tape-smoke "$V/ta-sma-152-close-cross-01/$LIB" \
  "$PINEFORGE_ENGINE_ROOT/corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv" --bars 300
# settled 300 bars, last hash 779454d0f6ef444a, trades 5
```

Those outputs are examples from one engine commit (`41c9c74`) and the corpus
commit it pins. `abi 4` and `exports 24/24` are the stable parts; the version
string is a `git describe` of whatever you built, and the hash and trade count
hold only at that engine and corpus pin.

`engine-info` fails at load if any of the 42 symbols the binding declares is
missing or `pf_abi_version()` is not 4; the printed `exports 24/24` counts only
the 24 ABI-v4 live-surface exports. `tape-smoke` replays the first `--bars` bars
(default 500) of the feed through the tape bar source and recomputes the ledger
at every confirmed bar with broker-state-hash recording on; it exits 1 and
prints `aborted` if a run does not complete. Exit codes across the CLI: 0 ok,
2 usage, 1 anything else, printed as `error: <message>`.

The feed is pineforge-corpus's Binance ETH-USDT perpetual export — see that
repository for its provenance, known gaps and terms. It is test data, not a
reference dataset.

### L1 harness

`scripts/l1_harness.py` drives `LiveCore` through a tape: it seeds the ledger on
the first `--start` bars, then for each of `--bars` bars synthesizes ticks under
`--policy`, evaluates on every tick, echoes each action back as a perfect venue
fill, and settles. At the end it checks every journaled hash and trade prefix
against the final recompute's own hash vector and trade list — the "reference
pass", which costs no extra engine run and is independent of the per-bar
read-back the harness also does.

```sh
V=$PINEFORGE_ENGINE_ROOT/corpus/validation
FEED=$PINEFORGE_ENGINE_ROOT/corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv

python3 scripts/l1_harness.py --so $V/ta-sma-152-close-cross-01/$LIB          --feed $FEED
python3 scripts/l1_harness.py --so $V/ta-pivot-atr-stop-target-01/$LIB        --feed $FEED
python3 scripts/l1_harness.py --so $V/order-deferred-flip-pooc-cross-bar-01/$LIB --feed $FEED
```

Defaults: `--start 2000 --bars 200 --tf 15 --policy path4 --seed 1`. The first
summary line of each run, at the engine and corpus pin above:

```text
l1: bars 200 settle_fills 16 probe_fills 16 retracts 0 probe_not_settled 0 path_variant 0 superseded 0 stops 0 incidents 0 non_confirmed 0 g1_failures 0 recompute_ms_p99_settle 5.716 recompute_ms_p99_probe 10.282
l1: bars 200 settle_fills 13 probe_fills 13 retracts 0 probe_not_settled 0 path_variant 0 superseded 0 stops 0 incidents 0 non_confirmed 0 g1_failures 0 recompute_ms_p99_settle 9.575 recompute_ms_p99_probe 17.939
l1: bars 200 settle_fills  8 probe_fills  4 retracts 0 probe_not_settled 0 path_variant 0 superseded 0 stops 0 incidents 0 non_confirmed 4 g1_failures 0 recompute_ms_p99_settle 5.677 recompute_ms_p99_probe 9.984
```

| key | meaning |
|---|---|
| `settle_fills` | fills the recomputed ledger emulated across the window |
| `probe_fills` | fills the intrabar probe confirmed on both paths and turned into `TRIGGER`s |
| `retracts` | probe fills that disappeared on a later tick of the same bar |
| `probe_not_settled` | probe fills the settle did not reproduce — an L1 failure |
| `path_variant` | closing fills the two path orders disagreed on |
| `superseded` | settle-time `MARKET_AT_OPEN` notices replaced on the first evaluate |
| `non_confirmed` | venue fills classified as anything but `CONFIRMED` |
| `g1_failures` | settlements whose G1 check failed |
| `stops`, `incidents` | STOPs raised and incidents journaled |
| `recompute_ms_p99_*` | nearest-rank p99 of one engine recompute, settle and probe |

The millisecond figures come from one core of one macOS machine and will differ
on yours. The counts should not, at the same engine and corpus commits.

The three strategies were chosen for coverage: the SMA-cross one has plain
reversal MARKET entries; the pivot/ATR one has priced stop and limit exits, so
the book holds real ENTRY/EXIT rows and the probe emits priced `TRIGGER`s; the
`process_orders_on_close` one produces `SETTLE_ONLY` fills, which is why its
`non_confirmed 4` is expected — the harness prints
`fills not CONFIRMED: SETTLE_ONLY` for each — and why its `probe_fills` is lower
than its `settle_fills`. `superseded 0` across all three is a consequence of
partition-1 sizing: no entry's quantity depends on the fill price here.

Tick policies (`--policy`):

| policy | prints |
|---|---|
| `path4` | the four probe points — 1/5/10/14 minutes of a 15-minute bar, scaled proportionally for other timeframes |
| `path4-reversed` | the same four, walking the far extreme first |
| `dense` | 16 prints interpolated along open → near → far → close |
| `random-ohlc` | a seeded walk inside `[low, high]` |
| `real` | recorded ticks — needs a ticks CSV the harness does not expose today, so it is not reachable from this CLI |

`--seed` is read only by the random policy. Risk limits, the dead band and the
breakers are fixed constants at the top of `scripts/l1_harness.py` and are
deliberately wide: an L1 run measures G1 and probe-equivalence, not risk policy.
Edit those constants to experiment.

Add `--journal DIR` to keep the journal and `--out FILE` for the full JSON
report (top-level `config` including `epoch_hash`, `summary`, `recompute_ms`,
`reference_run`, `aborted_at`, `seed`, and a per-bar `bars` array). `build/`,
`*.sqlite3` and `*.stop` are git-ignored, so these stay out of commits:

```sh
python3 scripts/l1_harness.py --so $V/ta-pivot-atr-stop-target-01/$LIB --feed $FEED \
  --journal build/l1 --out build/l1.json
pineforge-live journal-inspect build/l1/j.sqlite3
# epochs: 1
# runtime_configs: 1
# bars: 201
# settlements: 201
# evaluations: 800
# intents: 4
# ... one row count per table in journal/schema.py ...
# last_settlement {'epoch_hash': '...', 'bar_index': 2199, 'bars_hash': ..., 'broker_state_hash': ..., 'trades_len': 105, 'position': 0.0, ...}
# non_terminal_actions []
# stop_marker armed
```

`journal-inspect` prints a row count for each of the journal's 16 tables, the
newest epoch's latest settlement row, and the client ids whose latest order
state is non-terminal — always `[]` today, because nothing writes actions until
B3. It writes no rows, but it is not byte-level read-only: opening a journal
runs `PRAGMA journal_mode=WAL` and the idempotent schema DDL, creating `-wal`
and `-shm` siblings, so do not point it at a journal a running process owns. It
refuses a torn tail or a journal without a schema row. The marker states are
`absent` (no `<journal>.stop` sidecar), `armed` (the sidecar is pre-allocated
and zeroed, no STOP — this is the healthy state a clean run leaves behind), and
`present` (a STOP payload, or a torn marker; either makes the journal refuse to
open until an operator clears it).

Exit code 1 from the harness means at least one of `g1_failures`,
`probe_not_settled` or `stops` was non-zero, or a recompute aborted; a feed too
short for `--start + --bars` also exits 1, with a message.

## Layout

| path | responsibility |
|---|---|
| `pineforge_live/engine/abi.py` | `ctypes` mirror of the engine's `pineforge.h` (ABI v4); asserts the ABI version and every declared symbol at load |
| `pineforge_live/engine/handle.py` | `EngineHandle`: setter log, fresh strategy per `run_full`, per-run probe flags, cross-thread abort |
| `pineforge_live/engine/report.py` | `RunResult` / `TradeRow` and pending-order decoding |
| `pineforge_live/types.py` | venue-neutral frozen dataclasses and enums: instruments, syminfo, bars, ticks, orders, fills, events, STOP levels |
| `pineforge_live/epoch.py` | `EpochSpec`, `CodeIdentity`, `RuntimeConfig`, `apply_epoch` |
| `pineforge_live/bars/` | bar policy version, `tf_ms`/`bucket_start`, forming-bar builder, bar hashing |
| `pineforge_live/journal/journal.py` | SQLite WAL journal with a checksummed tail |
| `pineforge_live/journal/schema.py` | the DDL and journal schema version (v2; v1 journals are preserved and refused) |
| `pineforge_live/journal/sidecar.py` | `StopMarker`, the out-of-band STOP file |
| `pineforge_live/journal/fence.py` | `FencedLease` — one writer per journal |
| `pineforge_live/adapters/base.py` | `Clock`, `InstrumentSource`, `BarSource`, `TickSource`, `Executor`, `Adapter` protocols |
| `pineforge_live/adapters/mock.py` | deterministic venue simulator and injectable execution faults |
| `pineforge_live/execution/` | durable requests/receipts, order coordinator, risk validation and protection proposals |
| `pineforge_live/drivers/` | atomic core checkpoints and offline execution replay |
| `pineforge_live/adapters/tape.py` | deterministic tape clock, bar source and synthetic tick source over a feed CSV |
| `pineforge_live/core/ledger.py` | recompute ledger, seed, G1 |
| `pineforge_live/core/probe.py` | two-path intrabar probe |
| `pineforge_live/core/book.py` | settled book and intent-state diff |
| `pineforge_live/core/classify.py` | `FillClass` and `classify_bar` |
| `pineforge_live/core/reconcile.py` | reconcile table, bounds, counters |
| `pineforge_live/core/riskguard.py` | STOP levels, budgets, G3 breakers, durability order |
| `pineforge_live/core/ids.py` | trade and intent identity: `TradeKey`, `IntentKey`, `keys_sha256`, header-derived order-type names |
| `pineforge_live/core/live.py` | `LiveCore`: `seed`, `settle`, `evaluate`, `ActionRequest`, `CoreOutput` |
| `pineforge_live/harness.py` | shared tape wiring (`tape_spec`, `make_handle`, `open_journal`) |
| `pineforge_live/cli.py` | the `pineforge-live` CLI |
| `scripts/build_engine.sh` | builds the engine's corpus in build-only mode |
| `scripts/l1_harness.py` | the L1 harness described above |
| `docs/core.md` | the core's design record |
| `tests/` | engine-backed cases skip without `PINEFORGE_ENGINE_ROOT` or a built fixture |

## Guarantees and gates

| gate | statement | on failure |
|---|---|---|
| G0 | The ledger at every script-bar close is `run_backtest_full(bars[history_start..n])`; every intrabar evaluation is the same function over `bars[..n] + forming` with tail logic suppressed. No other fill-deciding entry point is linked. | structural — there is nothing to fail at runtime |
| G1 | For all `m < n`, the trades of `ledger(n)` with `exit_bar <= m` equal `ledger(m)`'s trades, and the broker-state hash recorded for bar `m` during `ledger(n)`'s run equals the hash journaled at settlement `m`. | `STOP(HARD, HOLD, "g1:<cause>")` |
| G3 | Once a breaker's window holds `n_min` samples, it trips when the observed rate exceeds `theta` or the raw hit count exceeds `x_max`. `n_min = ceil(z²(1-θ)/θ)` — 381 at θ = 1% — is derived from the Wilson bound, and a breaker whose `UB95(0, n_min) >= θ` is refused at construction. | `STOP(FLAT_ONLY, "g3:<name>")`; below `n_min` an alert incident only |

G2 is a spec-level invariant with no separate runtime check in this repository;
the numbering follows the spec, so it is absent rather than missing.

STOP semantics: levels `NONE < FLAT_ONLY < HARD`; dispositions `NONE`, `HOLD`,
`FLATTEN`. STOPs only ever strengthen, and are written marker → journal →
memory. Under `FLATTEN` one reduce-only `HARD_FLAT` is permitted and exempt from
budgets. `(HARD, NONE)` normalizes to `(HARD, HOLD)`.

Clearing a STOP is an explicit operator action. Use `journal-inspect` first,
then `pineforge-live stop-clear journal.sqlite3 --cause "verified venue state"`.
The command refuses an active writer, commits the audit record and journal
clear before removing the marker, and releases its lease. Specify `--marker`
when the sidecar lives elsewhere. Startup refuses a set marker.

What the L1 harness checks, on the runs above and only there — one 15-minute
ETH-USDT tape, `--policy path4`, `--seed 1`, 200 bars from index 2000, three
strategies, a perfect echo venue: settle reproduces every probe fill, G1 holds
at every bar and in the end-of-run reference pass, and `process_orders_on_close`
fills classify as `SETTLE_ONLY` rather than `MISSED`. It also reports recompute
latency (p50/p99/max, settle and probe) — measured, never asserted; no latency
bound is enforced anywhere yet.

What it does not check: partition-3 entries whose quantity depends on the fill
price, bracket exits under injected faults (rejects, partial fills, late fills),
restart paths, and the cadence over a full multi-year history. Those belong to
the B4 fault-injection harness.

## Roadmap

- **B3 — execution and exchange adapters (in progress).** Durable local
  execution, restart checkpoints, mock venue, receipt matching, risk validation,
  protection proposals, rate reserves, and the offline CLI are implemented.
  Remaining: real instruments and exchange adapter conformance, live stream /
  cron scheduling, atomic protective-order replacement, dead-man deadlines,
  DISASTER handling, complete late/partial recovery, and measured admission.
  See [execution boundaries](docs/execution.md) and [ledger](ledger.md).
- **B4 — fault-injection harness.** Rejects, partials, stale streams, venue
  restarts, and the coverage gaps listed above.
- **Registry and lint work** after that.

## Related projects

All under [github.com/pineforge-4pass](https://github.com/pineforge-4pass).

| project | license | how this repository depends on it |
|---|---|---|
| [pineforge-engine](https://github.com/pineforge-4pass/pineforge-engine) | Apache-2.0 | the backtest engine this runtime binds to over its C ABI; `main` is what exposes ABI v4. Its TradingView-parity results are measured and reported there |
| [pineforge-corpus](https://github.com/pineforge-4pass/pineforge-corpus) | Apache-2.0 | consumed as the engine's `corpus` submodule; the three harness strategies and the ETH-USDT 15-minute feed come from it |
| [pineforge-codegen-oss](https://github.com/pineforge-4pass/pineforge-codegen-oss) | PolyForm Noncommercial (source-available, not open source) | the PineScript v6 → C++ transpiler that produces every compiled strategy this runtime loads. Read its terms before compiling a strategy for commercial use |
| [pineforge-release](https://github.com/pineforge-4pass/pineforge-release) | Apache-2.0 | the bundled engine + codegen distribution — the practical way to get a matching pair |
| [pineforge-backtest-mcp](https://github.com/pineforge-4pass/pineforge-backtest-mcp) | MIT | an MCP server exposing PineForge backtests to an agent |

Running your own strategy means going through that chain: `strategy.pine` →
pineforge-codegen-oss → `generated.cpp` → the engine's CMake build →
`strategy.dylib`/`.so`. Any library built against ABI v4 loads. One current
limit: the tape wiring's syminfo is fixed to the corpus ETH-USDT perpetual
(tick 0.01, point value 1.0, venue `TAPE`), so another symbol means editing
`tape_syminfo`/`tape_spec` in `pineforge_live/harness.py` until B3 adds a real
instrument source. The design spec and the parity campaign's own registry live
in private repositories, which is why `docs/core.md`'s section references
resolve to nothing public — that is not a broken link on your side.

## Contributing

Run both test modes before opening a pull request:

```sh
python3 -m pytest -o addopts=""                                        # engine absent
PINEFORGE_ENGINE_ROOT=/path/to/pineforge-engine python3 -m pytest -o addopts=""   # engine present
```

Rules the reviews hold to:

- No venue-specific code in `pineforge_live/core/` or `types.py`. Venue behavior
  lives behind the adapter protocols.
- Tests use the neutral venue literal `"TAPE"` and the tape adapters.
- No `TODO`, `xfail` or `skip` markers as a way of landing incomplete work. The
  only permitted skips are the engine- and fixture-absent skips in
  `tests/conftest.py`.
- A change to the core needs a test that fails without it.

Implementation notes for anyone touching `engine/handle.py` or `core/probe.py`
(the engine handle is not pure across runs; per-run probe flags go through
`run_full(per_run=...)`, never the public setters) are in `docs/core.md`.

Versioning is pre-1.0: no tags, no PyPI release, editable install only. Five
version axes appear in the first CLI line and are worth knowing apart — the
package version (kept in both `pineforge_live/__init__.py` and
`pyproject.toml`), `adapter-api` (bumped when an adapter protocol changes), the
bar-policy string (bumped when bucketing or forming rules change), the journal
schema version (bumped on DDL changes, with no migration path), and the engine
ABI (tracked from pineforge-engine). The first three plus the ABI feed
`EpochSpec.epoch_hash()`, so bumping any of them starts a new epoch.

Report problems as GitHub issues on the right repository: engine fill behavior
and TradingView parity to pineforge-engine, PineScript → C++ transpilation to
pineforge-codegen-oss, fixtures and trade lists to pineforge-corpus, and
anything about the runtime, ledger, journal or reconciler here. Attach the
harness's `--out` report, `pineforge-live version`, `engine-info` output and the
engine commit. There is no private security contact yet; there will be one
before anything in this repository can place an order.

## License

Apache-2.0, matching pineforge-engine. See the `LICENSE` file at the repository
root.

## Disclaimer

This is research software provided without warranty of any kind, and nothing in
it is warranted fit for live trading, now or in any future version. Trading
financial instruments carries the risk of loss, including total loss. Nothing
here is financial advice.

Specifically:

- **A STOP does not close your position.** `HARD` with a `HOLD` disposition
  deliberately holds an open position and waits for an operator, and
  `FLAT_ONLY` leaves existing exposure in place. An unattended process can sit
  in a losing position exactly as designed.
- **Losses can occur while the software behaves correctly.** The ledger is what
  the engine computed; the account experiences the venue's fills.
- **You are responsible** for your exchange's terms of service, market rules and
  any regulatory or licensing obligations that apply to you.
- This project is not affiliated with or endorsed by TradingView. Pine Script is
  TradingView's trademark, used here only to name the language.
