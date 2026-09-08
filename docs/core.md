# The live core (Plan B2)

The core is the part of `pineforge-live` that decides **what should be true at
the venue**. It talks to no venue, no clock and no socket: two functions are
called *with* a bar (or a tick) and the venue's own reported fills and
position, and they hand back the orders an execution layer should place. That
is what makes the whole thing testable against a recorded tape
(`scripts/l1_harness.py`) and what keeps every real exchange name out of it —
the tests and the harness call their venue `"TAPE"`.

Spec: `pineforge-workflow-live/docs/superpowers/specs/2026-09-07-pineforge-live-design.md`
(§4 is the algorithm, §5.4 the reconciler, §5.5 STOP/RiskGuard, §1 the G-invariants).

## The one invariant everything else serves

**G0 — single broker model.** The ledger at every script-bar close *is*
`run_backtest_full(bars[history_start..n])`. Every intrabar evaluation is the
same function over `bars[..n] + the forming bar` with tail logic suppressed.
The core links no other fill-deciding entry point and never calls
`strategy_stream_*`. There is no second implementation of the fill rules to
drift from the backtest, because there is no second implementation at all.

**G1 — prefix stability.** For all `m < n`: the trades of `ledger(n)` with
`exit_bar ≤ m` equal `ledger(m)`'s trades, and `broker_state_hash[m]` recorded
during `ledger(n)`'s run equals the hash journaled at settlement `m`. Checked
at *every* settlement against the journal, not sampled. A failure is
`STOP(HARD, HOLD)`: the recompute disagrees with a durable record, which is
never something a runtime may trade through.

## Cadence

```
seed(history)                      once, at epoch open — adopt the position the recompute says we hold
  ├─ evaluate(forming, now_ms)     per coalesced tick (stream) / once per run (check)
  ├─ evaluate(forming, now_ms)     …
  └─ settle(bar, fills, …, now_ms) on the confirmed script-TF kline
```

`settle()` opens each bar's RiskGuard budget (`begin_bar`), so a bar's whole
tick stream shares one `max_fill_actions_per_bar` allowance — spec §4's "one
TRIGGER per intent per cycle; `max_fill_actions_per_bar` ≤ P's fill count".

## The pieces

| module | what it owns |
|---|---|
| `core/ledger.py` | `Ledger` — the recompute, the G1 check, the settlement journal row. `SettleResult` is one bar's whole recomputed state (trades, keys, hashes, position, `entry_fills`, the settled `book`, `recompute_ms`). |
| `core/probe.py` | `Probe` — the intrabar recompute. Runs `P_auto`, and `P_other` (opposite path order) whenever `P_auto` fills, and keeps only what both paths agree on. `ProbeResult` carries `fills`, `deferred`, `retracted`, `dropped`, `levels`. |
| `core/book.py` | The settled book: `Intent` (one resting order, keyed by `IntentKey`), `settled_book()`, `book_diff()` → `RESTING`/`MODIFIED`/`CANCELLED`/`DEAD`, and the dual-entry guard. |
| `core/classify.py` | `classify_bar()` — one bar's ledger (emulated) fills against the venue's own, into a `FillClass`. `emulated_from_settle()` derives the emulated fills from a `SettleResult` (closed-trade legs + position-delta fills). |
| `core/reconcile.py` | `reconcile()` — a pure decision: classified fills in, `CorrectionRequest`s + a possible STOP out. Never submits anything. |
| `core/riskguard.py` | `RiskLimits`/`RiskGuard` budgets, the `StopController` (levels, durability, restore, clear), G3 `Breaker`s and their `RateWindow`s. |
| `core/ids.py` | Trade/intent identity: `TradeKey`, `keys_sha256`, the header-derived order-type names. |
| `core/live.py` | `LiveCore` — the facade that composes all of the above into `seed`/`settle`/`evaluate`, and the only place that journals. |

## `settle(bar, venue_fills, in_flight, mirrored, real_position, now_ms)`

Spec §4 settle 1–8, in this order — and the order is load-bearing:

1. **Recompute.** Append the bar, roll `bars_hash`, `run_backtest_full` over
   everything. A revised settled bar is `BarsDivergence` → `STOP(FLAT_ONLY)`;
   a non-contiguous bar is a `LedgerGap` and *propagates* (the caller handed
   the core the wrong bar — gap carry-forward is the bar layer's job, not a
   STOP).
2. **G1** against the journal → `LedgerDivergence` → `STOP(HARD, HOLD)`.
3. **Settled book and its diff** (`book_diff(prev, cur)`) — this is the
   mirrored set B3 syncs to the venue.
4. **Classify** this bar's emulated fills against `venue_fills`, matched on
   `(intent id, leg, side, target bar)` first and qty last.
5. **Reconcile** (§5.4 below) and **raise whatever STOP it demands**.
6. **Only then build the bar's actions** and filter every one through the
   STOP gate and the RiskGuard budgets. Building actions *after* the
   escalation is what stops a settlement from shipping an exposure-increasing
   order it has already decided to STOP over.
7. **Journal** the reconcile row (the settlement row is the ledger's own,
   written atomically and idempotently per `(epoch, bar_index)`).

`real_position` is the venue account's own reported position — spec §5.4's
*secondary* check. The *primary* basis is `our_signed_fills`: the cumulative
signed qty of the `OURS` fills we were handed, which `LiveCore` derives itself
(anchored at `seed()` on the position the seed adopted, reset when a
settlement leaves the ledger flat) unless the caller passes an
exchange-derived value for that one call.

## `evaluate(forming, now_ms)`

Spec §4 evaluate 1–4:

1. `P_auto` over `bars[..n-1] + forming`; if it fills anything, `P_other` with
   the opposite path order.
2. A fill confirmed by **both** paths becomes a `TRIGGER` MARKET at the
   engine's own qty. A path-variant fill that *closes* a cycle is still
   emitted (`PATH_DIVERGENT` carries the price delta); one that would *change
   net position* is deferred and becomes `MISSED` at settlement. A fill a
   previous tick reported and this one no longer confirms is `retracted` —
   journaled, never a STOP.
3. Intrabar level refresh for settled intents under the epoch's
   `trail_refresh_policy` (`intrabar_best` or `bar_open_level`).
4. The first `evaluate()` of a new bar yields the settled book's MARKET fills
   at that open — which `settle()` has already requested as
   `MARKET_AT_OPEN`, so the two are de-duplicated: a `(intent, leg)` already
   asked for at this bar's open is not TRIGGERed again, and a `(intent, leg)`
   is TRIGGERed at most once per bar however many ticks arrive.

## Fill classes and the reconcile table

`classify_bar` labels each fill; `reconcile` decides what, if anything, to do
about it (spec §5.4). Corrections are bounded by `ReconcileConfig`
(`max_missed_age_bars`, `max_missed_entry_distance_bps`, `budget_notional`,
`mirror_early_daily_cap`) and by the dead-band
`max(lot_step, min_qty, min_notional/price)`.

| class | meaning | reconciler |
|---|---|---|
| `CONFIRMED` | emulated fill has its venue counterpart | nothing |
| `IN_FLIGHT` | the intent has a non-terminal action | deferred; never corrected, never `MISSED` |
| `MISSED` | emulated, no venue fill, nothing in flight | one correcting MARKET within age/distance/budget bounds; otherwise the cycle is journaled `SKIPPED` |
| `SYNTHETIC` | engine-forced close (`MARGIN_CALL`, `INTRADAY_*`) | reduce-only MARKET now; never looks for a venue counterpart |
| `QTY_DIVERGENT` | same intent, qty differs beyond the dead-band | reduce-only trim (excess) / budgeted top-up (shortfall) / carry as residual |
| `PATH_DIVERGENT` | venue and ledger closed the same cycle via different legs, net equal | continue; counterfactual journaled; G3 input |
| `MIRROR_EARLY` | venue filled a mirrored level the ledger did not fill this bar | hold flat, retain the intent; per-day cap → `STOP(FLAT_ONLY)` |
| `TRIGGER_REVERSED` / `ENTRY_SLIP` | our executed TRIGGER against a flat/opposite ledger; entry beyond the slip budget | reduce-only flatten, then `STOP(FLAT_ONLY)` |
| `RETRACTED` / `UNATTRIBUTED_VENUE` | a venue fill with no emulated counterpart | `STOP(FLAT_ONLY)` (auto-correction is opt-in) |

Above the table: if the account position disagrees with our own fill basis by
more than the dead-band, the reconciler cannot trust its own fill tracking on
that call — it escalates `STOP(FLAT_ONLY)` (`account_mismatch`) and skips
every `MISSED`/`QTY_DIVERGENT` correction. A venue-initiated fill
(liquidation, ADL) is `STOP(HARD, FLATTEN)`.

`CorrectionRequest.kind` is one of `MARKET_CORRECT`, `REDUCE_ONLY_TRIM`,
`TOP_UP`, `FLATTEN`.

## STOP semantics

Two axes (`types.StopLevel` × `types.StopDisposition`):

- `FLAT_ONLY` — no order that opens or increases exposure. Everything
  reduce-only, and every cancel, still passes.
- `HARD` — additionally no *new* reduce-only orders, except the dead-man, a
  re-established static protective exit, and (under `FLATTEN` only) one
  reduce-only `HARD_FLAT` MARKET to the venue-reported position. `HARD` always
  carries a bounded disposition: `(HARD, NONE)` normalises to `(HARD, HOLD)`,
  which is what starts the `hard_stop_max_hold_ms` clock `hold_expired()`
  measures.

Rules that hold everywhere:

- **Monotonic.** `raise_stop` only ever escalates, ranked by
  `riskguard.stronger()` (level first, disposition as the tiebreak). The
  reconciler escalates through the same function, so there is one rank table
  in the codebase, not two.
- **Operator-cleared.** `clear()` is the only path back to `NONE`, and it
  drains *every* still-open `stops` row so a restart cannot resurrect an older
  escalation.
- **Durable, journal or no journal.** The out-of-band marker file is written
  first, the journal second, memory last — and memory is set in a `finally`,
  so a disk-full fault still leaves the process STOPped (and still re-raises).
  Startup refuses while the marker exists; `restore()` replays the open rows,
  keeps the strongest, and re-derives the hold clock from the row's own
  `created_ms` so a crash does not silently restart the countdown.
- **Never silent.** An action refused by the STOP gate or a RiskGuard budget
  is journaled (`action_refused_by_stop` / `risk_refused`) and returned on
  `CoreOutput.incidents`. An order the core wanted and did not send is exactly
  what an operator must be able to find afterwards.

G3 breakers (spec §1) are self-tested at construction — a breaker that could
never fire (`UB_95(0, n_min) ≥ θ`, or a window smaller than its own `n_min`)
is a startup error, not a silent no-op — and sampled once per settlement:
`breached()` escalates `STOP(FLAT_ONLY)`, `alert()` journals.

## The B3 handoff

`settle()`/`evaluate()` return a `CoreOutput`, and `CoreOutput.actions` is the
whole contract with the execution layer. An `ActionRequest` is frozen and
carries `(kind, intent, side, qty, price_hint, reduce_only, cls, reason,
target_bar_index)`.

| kind | when | notes |
|---|---|---|
| `TRIGGER` | `evaluate` | an intrabar fill both paths confirmed; `price_hint` is the probe's own fill price |
| `MARKET_AT_OPEN` | `settle` | a MARKET resting in the settled book, filling at bar `n+1`'s open. `price_hint` is `None` — the open *is* the price (no fallback; B3 waits `open_wait_ms` for it). An order that reverses a live position is TWO legs: a reduce-only close, then the engine's own opened qty |
| `SYNTHETIC_CLOSE` | `settle` | an engine-forced close (margin call / intraday cap), reduce-only |
| `CORRECTION` | `settle` | a reconciler `MARKET_CORRECT` / `REDUCE_ONLY_TRIM` / `TOP_UP`; `cls` names which |
| `FLATTEN` | `settle` | reduce real exposure to zero (`TRIGGER_REVERSED`, `ENTRY_SLIP`) |
| `CANCEL_STALE_CYCLE` | `settle` | an intent that left the settled book **unfilled**; `book_diff` reads `CANCELLED` for a filled order too, so the bar's own emulated fills disambiguate and a filled order is never chased. `qty` is 0 — a book op, not a fill |

`intent` is always the **Pine order id**, never an `IntentKey.s`: that is the
identity a venue fill is matched back against, so the request → fill →
classification round trip only closes in that id space. The intent *key* for a
book op is in `CoreOutput.book` / `book_diff`, keyed by `IntentKey.s`.

What B3 owns and the core deliberately does not: client ids and `action_seq`,
the order state machine and adoption, mirror sync (`level_version`, dead-band,
create-before-cancel) from `CoreOutput.book` + `ProbeResult.levels`, dead-man
arming, rate lanes, the `DISASTER` pair, the `bar_mismatch` streak, the stream
and check drivers, and the venue-fed breakers (feed/eval staleness,
liquidation distance, unexplained divergence) that `RiskLimits` already
declares but nothing here can sample.

## Proving it: the L1 harness

`scripts/l1_harness.py` runs this cadence over a recorded feed against a
perfect venue and reports the two L1 assertions (spec §10.2) — G1 at every
settlement plus a reference-run cross-check, and *probe ⊆ settlement ∪
retracted*. See the README for the command and the current numbers.
