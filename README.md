# pineforge-live

The live runtime whose ledger *is* the PineForge backtest: at every script-bar
close the ledger is `run_backtest_full(bars[history_start..n])`; every intrabar
evaluation is the same function over the bars plus the forming bar with the
engine's ABI v4 tail flags. Design: `pineforge-workflow-live/docs/superpowers/specs/2026-09-07-pineforge-live-design.md`.

## Layout
- `pineforge_live/engine/` — ctypes binding to the engine's C ABI (v4). Never the streaming lifecycle (G0).
- `pineforge_live/types.py`, `pineforge_live/adapters/base.py` — venue-neutral types and adapter interfaces (`ADAPTER_API_VERSION`).
- `pineforge_live/bars/` — forming-bar builder, bar policy, bar hash.
- `pineforge_live/journal/` — SQLite WAL journal, sidecar STOP marker, fencing lease.
- `pineforge_live/epoch.py` — epoch / code identity / runtime config hashes.
- `pineforge_live/adapters/tape.py` — bar/tick tape adapters and a drivable clock.
- `pineforge_live/core/` — the live core: ledger, probe, settled book, fill classification, reconciler, RiskGuard/STOP, and the `LiveCore` facade (`settle`/`evaluate`). See **[docs/core.md](docs/core.md)**.
- `pineforge_live/harness.py` — the tape-backed epoch/handle/journal wiring the L1 harness and the test fixtures share.
- `pineforge_live/cli.py` — the `pineforge-live` operator CLI: `version` | `engine-info` | `journal-inspect` | `tape-smoke` (installed as the `pineforge-live` console script, `pyproject.toml`'s `[project.scripts]`).
- `scripts/build_engine.sh` — builds the pinned engine checkout at `PINEFORGE_ENGINE_ROOT` and its corpus fixtures, so the engine-backed tests (and `tape-smoke`) have a compiled strategy `.so`/`.dylib` and a derived feed to run against.
- `scripts/l1_harness.py` — the L1 harness (below).

## The core

`pineforge_live/core/` is Plan B2: everything that decides *what should be true
at the venue*, and nothing that talks to one. Two entry points —
`LiveCore.settle(bar, venue_fills, …)` at each confirmed script-TF close and
`LiveCore.evaluate(forming, now_ms)` on each coalesced tick — return a
`CoreOutput` whose `actions` are the orders Plan B3's executor should place.
**[docs/core.md](docs/core.md)** is the page: the settle/evaluate steps, the
modules, the fill-class → reconcile table, STOP semantics, and the
`ActionRequest` kinds B3 consumes.

## L1 harness

`scripts/l1_harness.py` runs the full cadence over a recorded feed against a
perfect venue — seed, then per bar `evaluate()` at the tape's k = 4 probe
points and `settle()` at the close — and checks spec §10.2's two L1
assertions:

- **G1** at every settlement (the ledger's own check), and *independently* an
  end-of-run reference pass comparing the final recompute's hash vector and
  trade prefixes against what each settlement journaled at the time. The
  per-bar read-back of the `n−1` journal row is a cheap belt-and-braces
  assertion, not an independent check — `Ledger.settle` compares that same row
  before journaling and raises otherwise.
- **probe ≡ recompute**: every probe fill is a fill of that bar's own
  settlement or was retracted by a *later* `evaluate()` on the same bar, read
  in tick order (fill → retract → fill leaves the fill standing and it must
  settle). Path-variant fills — confirmed by `P_auto` only — are counted
  separately, since they settle as `PATH_DIVERGENT`.

The venue honours `ActionRequest`'s supersede contract, so a settle-time advance
MARKET leg and any open-priced re-quote of it at bar `n+1` are filled once, not
twice. It exits non-zero on any `g1_failures`, `probe_not_settled`, or **STOP**
— raised by `settle()` or by `evaluate()` — because a STOP mid-window refuses
actions and changes the very stream the two assertions are about. A refused
`seed()` is reported the same way (`report["seed"]`, exit 1) instead of dying
with a message.

```sh
PINEFORGE_ENGINE_ROOT=~/code/pineforge-engine-wt/main python3 scripts/l1_harness.py \
  --so   $PINEFORGE_ENGINE_ROOT/corpus/validation/ta-sma-152-close-cross-01/strategy.dylib \
  --feed $PINEFORGE_ENGINE_ROOT/corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv \
  --start 2000 --bars 200 --out build/l1.json
```

```
l1: bars 200 settle_fills 16 probe_fills 16 retracts 0 probe_not_settled 0 path_variant 0 superseded 0 stops 0 incidents 0 non_confirmed 0 g1_failures 0 recompute_ms_p99_settle 5.722 recompute_ms_p99_probe 10.245
l1: recompute_ms settle p50 5.168 p99 5.722 max 6.578 (n 200)
l1: recompute_ms probe  p50 5.192 p99 10.245 max 10.474 (n 800)
```

The bracket probe (`ta-pivot-atr-stop-target-01`: ATR stop/target via
`strategy.exit`, same feed and same `"TAPE"` syminfo — only `--so` differs) over
the same window, which additionally exercises priced-exit `TRIGGER`s:

```
l1: bars 200 settle_fills 13 probe_fills 13 retracts 0 probe_not_settled 0 path_variant 0 superseded 0 stops 0 incidents 0 non_confirmed 0 g1_failures 0 recompute_ms_p99_settle 9.016 recompute_ms_p99_probe 17.211
l1: recompute_ms settle p50 8.144 p99 9.016 max 9.392 (n 200)
l1: recompute_ms probe  p50 8.178 p99 17.211 max 17.753 (n 800)
```

And the POOC probe (`order-deferred-flip-pooc-cross-bar-01`:
`process_orders_on_close=true`, so the `strategy.close` fires at the cross bar's
own close while the stop entry waits for the next open — same feed and syminfo
again), the fixture spec §4 settle 6's "`process_orders_on_close` fills → MARKET
now" is about:

```
l1: bars 200 settle_fills 8 probe_fills 4 retracts 0 probe_not_settled 0 path_variant 0 superseded 0 stops 0 incidents 0 non_confirmed 4 g1_failures 0 recompute_ms_p99_settle 5.418 recompute_ms_p99_probe 10.046
l1: recompute_ms settle p50 4.938 p99 5.418 max 5.575 (n 200)
l1: recompute_ms probe  p50 4.938 p99 10.046 max 10.444 (n 800)
```

Its four non-`CONFIRMED` classifications are the four POOC closes in the window,
each `SETTLE_ONLY` → one reduce-only `MARKET_NOW` on its own bar. Zero of them
are `MISSED`: a POOC fill is not a fill the venue missed, and routing it through
the reconciler put a 2% steady-state orphan rate through the spec's own 1%
breaker.

`superseded` is the count of requests the venue never saw because a later
request replaced them. It is 0 on all three probes because these fixtures size
at partition 1 (fixed qty), so the engine's qty at the open equals the
settle-time close proxy and the advance needs no re-quote; a percent-of-equity
(partition 3) script re-quotes and the count rises. It is not an error either
way — a run with the supersede contract ignored would instead show doubled fills
and a STOP.

Those are the `recompute_ms` numbers on this machine (2000 bars of history,
macOS, one core; ±1 ms run to run, and float milliseconds because a probe
recompute can be sub-millisecond): they are what spec §2's
`grace ≥ recompute_p99 + submit_p99` has to be sized against, and they grow
with the history the ledger recomputes — re-measure per epoch rather than
assuming these. The probe's p99 is roughly double its own median because a bar
with any fill runs `P_auto` **and** `P_other`.

`--out` writes the whole report: per bar the probe fills, retracts,
path-variant fills, settlement fills, the actions requested and the (superseded)
set the venue actually saw, any STOP/incident/non-`CONFIRMED` class, G1 status
and both recompute times, plus the summary above and a `seed` section (the STOP
and incidents of a refused seed).

The three runs above are the release criterion for the core: each exits 0 with
`probe_not_settled 0`, `g1_failures 0` and `stops 0`.

## Tests
`PINEFORGE_ENGINE_ROOT=~/code/pineforge-engine-wt/main python3 -m pytest -q`
(`scripts/build_engine.sh` builds that checkout; engine-backed tests skip without the variable.)
