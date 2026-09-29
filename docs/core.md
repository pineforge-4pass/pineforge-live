# The live core (Plan B2)

The core is the part of `pineforge-live` that decides **what should be true at
the venue**. It talks to no venue, no clock and no socket: two functions are
called *with* a bar (or a tick) and the venue's own reported fills and
position, and they hand back the orders an execution layer should place. That
is what makes the whole thing testable against a recorded tape
(`scripts/l1_harness.py`) and what keeps every real exchange name out of it —
the tests and the harness call their venue `"TAPE"`.

Spec: `pineforge-workflow-live/docs/superpowers/specs/2026-09-07-pineforge-live-design.md`
(§4 is the algorithm, §5.4 the reconciler, §5.5 STOP/RiskGuard, §1 the G-invariants),
a maintainer design document that is not published.

## The one invariant everything else serves

**G0 — single broker model.** The ledger at every script-bar close *is*
`run_backtest_full(bars[history_start..n])`. Every intrabar evaluation is the
same function over `bars[..n] + the forming bar` with tail logic suppressed.
The core links no other fill-deciding entry point and never calls
`strategy_stream_*`. There is no second implementation of the fill rules to
drift from the backtest, because there is no second implementation at all.

**G1 — prefix stability.** For all `m < n`: the trades of `ledger(n)` with
`exit_bar ≤ m` equal `ledger(m)`'s trades, and `broker_state_hash[m]` recorded
during `ledger(n)`'s run equals the hash journaled at settlement `m`. A failure
is `STOP(HARD, HOLD)`: the recompute disagrees with a durable record, which is
never something a runtime may trade through.

What is checked when: every settlement re-checks the *previous bar's* hash and
digests against the journal, and the full trade prefix ending there — the whole
vector then follows by induction for a chain this process settled itself.
`seed()` is where that induction has to be re-established, because a restart
inherits a journal it did not write in this incarnation: it verifies **every**
journaled settlement row inside the seeded history's own length against the
recomputed hash vector.

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
| `core/live.py` | `LiveCore` — the facade that composes all of the above into `seed`/`settle`/`evaluate`, and the only place that journals: `epochs`/`runtime_configs` at construction, `incidents`, `reconciles`, and the settled book's own transitions to `intents` (spec §6 — B3 reads them to re-run a journaled STOP's cancel step on restart without recomputing the ledger first). `bars`/`settlements` are the ledger's, `evaluations` the probe's, `stops` the STOP controller's. |

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
7. **Journal** the reconcile row and the settled book's intent transitions
   (the settlement row is the ledger's own, idempotent per
   `(epoch, bar_index)`). "Atomic" it is not: the bar row and the settlement
   row are two inserts, which is exactly why `seed()` pre-checks for a
   conflicting settlement row before writing either.

**An idempotent re-delivery settles nothing twice.** `Ledger.settle` returns the
last settlement unchanged when handed a byte-identical re-delivery of the bar it
already settled (spec §4.8 — check mode's REST catch-up produces one routinely),
and `LiveCore` returns immediately on that: no classification, no reconcile, no
actions, no journal row, and `pending_market` untouched. Re-running the
settlement would emulate the bar's fills a second time against a venue that has
already reported them, classify them `MISSED`, and issue a duplicate correction.

`real_position` is the venue account's own reported position — spec §5.4's
*secondary* check. The *primary* basis is `our_signed_fills`: the cumulative
signed qty of the `OURS` fills we were handed, which `LiveCore` derives itself
unless the caller passes an exchange-derived value for that one call. It is
anchored at `seed()` — on the venue's own position when `seed(real_position=…)`
is given, else on the position the recompute adopted — and reset only when a
settlement leaves the ledger flat **and the venue's own report agrees it is
flat**. Both halves matter: resetting on a flat ledger alone reads the next
bar's repair fill as a position out of nowhere, and judging "the venue is flat"
from our own accumulator instead of `real_position` leaves it stuck after a
liquidation flattens the venue behind us.

A cold start is refused when they disagree: `seed(real_position=…)` beyond the
dead band from the recompute is `STOP(HARD, HOLD, "cold_start_position")` and no
seed, unless the operator passes `adopt_position` (spec §6).

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
   at that open. `settle(n)` has already requested those as `MARKET_AT_OPEN`
   for bar `n+1` — priced off the only price it had, bar `n`'s close — so this
   evaluate does not TRIGGER them a second time: it **supersedes** them,
   re-emitting the same `(intent, leg, target_bar_index)` as a
   `MARKET_AT_OPEN` carrying the engine's own qty *at the open*
   (`reason="open_requote"`) — but only when the two qtys differ by more than
   the dead band, since otherwise there is nothing to fix. The settle-time
   request is advance *notice* that lets B3 have an order in place at the open;
   this evaluate settles its fate three ways: re-quote (a real size
   difference), nothing (the sizes agree), or **withdraw** — a `qty=0`
   supersede for a key the open did not confirm at all, because the engine's
   admission gate refused it and the ledger will never book a fill for it. A
   re-quote and a withdraw do not spend `max_fill_actions_per_bar`: they amend
   an order the settlement already counted and placed. A `(intent, leg)` is
   otherwise TRIGGERed at most once per bar however many ticks arrive.

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
| `MISSED` | emulated, no venue fill, nothing in flight, and the order *had* been resting | one correcting MARKET within age/distance/budget bounds; otherwise the cycle is journaled `SKIPPED` |
| `SETTLE_ONLY` | emulated fill of an order that never rested in the previous book — a `process_orders_on_close` fill | not the reconciler's: `LiveCore` places a `MARKET_NOW` (spec §4 settle 6) and the venue reports it at `n+1` |
| `SYNTHETIC` | engine-forced close (`MARGIN_CALL`, `INTRADAY_*`) | reduce-only MARKET now; never looks for a venue counterpart |
| `QTY_DIVERGENT` | same intent, qty differs beyond the dead-band | reduce-only trim (excess) / budgeted top-up (shortfall) / carry as residual |
| `PATH_DIVERGENT` | venue and ledger closed the same cycle via different legs, net equal — decided by *pairability*: an unmatched emulated exit with the same close direction and qty must exist | continue; counterfactual journaled; G3 input |
| `MIRROR_EARLY` | venue filled a mirrored level the ledger did not fill this bar | hold flat, retain the intent; per-day cap → `STOP(FLAT_ONLY)` |
| `TRIGGER_REVERSED` / `ENTRY_SLIP` | our executed TRIGGER against a flat/opposite ledger; a matched ENTRY pair whose `\|venue − emulated\| / emulated × 1e4` exceeds `ReconcileConfig.max_entry_slip_bps` (per pair, not per bar) | reduce-only flatten, then `STOP(FLAT_ONLY)` |
| `RETRACTED` | one of OUR fills the ledger never emulated (a TRIGGER the settled recompute no longer produces) | `STOP(FLAT_ONLY)` (auto-correction is opt-in) |
| `UNATTRIBUTED_VENUE` | a **venue-initiated** fill — liquidation, ADL, a manual trade: not ours at all | `STOP(HARD, FLATTEN)` |

Above the table: if the account position disagrees with our own fill basis by
more than the dead-band, the reconciler cannot trust its own fill tracking on
that call — it escalates `STOP(FLAT_ONLY)` (`account_mismatch`) and skips
every `MISSED`/`QTY_DIVERGENT` correction.

Four bounds sit around the table, and all four bind:

- **`[r4]` age/distance.** A `MISSED` correction is bounded by
  `max_missed_age_bars` and `max_missed_entry_distance_bps`. For the age to
  mean anything the same `(intent, leg)` has to be able to read `MISSED` on two
  consecutive settlements, so a `MISSED` the decision declined to act on *for a
  reason that can change* — the settle was not quiescent, or its own STOP level
  or the budget refused the correction — is re-presented at the next
  settlement rather than dropped.
- **`disagree_twice`.** Consecutive settlements skipped as not quiescent
  (spec §5.4's "skip (bounded) and count toward `disagree_twice`") escalate
  `STOP(FLAT_ONLY)` at the limit: a driver whose actions never go terminal
  otherwise reconciles nothing, bar after bar, with only a counter to show.
- **`max_daily_reconciles`.** Admitted corrections, excluding both flatten classes,
  are tallied per UTC day and refused past the cap (incident +
  `cycle_skipped`) — the low-n guard on corrections, because the G3 rate
  breakers are alert-only below their own `n_min` (381 samples at θ = 1%).
- **`horizon_bars`.** Checked before each recompute, against `ledger.n` — the
  index of the bar about to be settled. `horizon_bars` *is* the frozen
  `last_bar_index`, so bar index `horizon_bars - 1` is still a legal bar and bar
  index `horizon_bars` is the first one refused. An alert incident once at 80%
  consumption, and at 100% `STOP(FLAT_ONLY, "horizon")` with the settle
  **refused** — spec §2's "forces an epoch rotation before the next
  settlement". The rotation itself is an operator ceremony (spec §6).

Schema-v2 reconcile rows record `bar_ts_open` and `admitted_reconciles`.
Restart restores the latest settled bar's UTC-day totals; wall-clock write
time is provenance only. The next settled bar performs the genuine day roll.
Proposed but refused corrections do not consume the restored cap; both
`HARD_FLAT` and reconciler `FLATTEN` are exempt from correction/notional caps,
while retaining the STOP permission check.

**The account contract is strict.** `real_position` must be the venue's own
snapshot *event-time after the last fill in `venue_fills`* — spec §5.4's
wording, taken literally. The core does not tolerate a snapshot one bar behind
the fills it is handed alongside: a driver that reports the pre-fill position
*is* an `account_mismatch`, and saying so is the whole point of the secondary
check. B3 therefore either reads the account after the fill stream has drained
or advances the last snapshot by `live.signed_qty(venue_fills)` itself (the
same OURS-filtered sum `settle()` derives its primary basis from — one
definition, not two). `scripts/l1_harness.py`'s perfect venue does exactly the
latter.

`CorrectionRequest.kind` is one of `MARKET_CORRECT`, `REDUCE_ONLY_TRIM`,
`TOP_UP`, `FLATTEN`, and every one carries its own `reduce_only`: only
`reconcile` knows whether a `MARKET_CORRECT` is repairing a missed ENTRY
(exposure-increasing) or a missed EXIT (reduce-only), so the flag travels on
the request rather than being re-derived from `kind` downstream — re-deriving
it made `permits()` refuse, under `FLAT_ONLY`, the one order `FLAT_ONLY`
exists to allow.

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

The `HARD_FLAT` is the one order the reconciler does not produce: it is not a
correction toward the ledger but the STOP's own disposition acting on venue
truth, so `settle()` emits it from the STOP state — once, journaled as an
incident, and only while `real_position` is non-zero, so a restart re-issues it
only if the venue still holds a position. Being a STOP action and not a
correction, it is counted against neither `max_daily_reconciles` nor
`max_order_notional`: the exposure is the position the venue *already* holds and
the order only reduces it. It is marked issued only once it has passed the
STOP/RiskGuard gate, so a refused one is retried while the position stands. The dead-man, the re-established
static exits and `hold_expired()`'s escalation are B3's.

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
never fire is a startup error, not a silent no-op — and sampled once per
settlement: `breached()` escalates `STOP(FLAT_ONLY)`, `alert()` journals. "Could
never fire" is three things: `UB_95(0, n_min) ≥ θ`, a window smaller than its own
`n_min`, and a `name` outside `reconcile.COUNTER_NAMES` — a breaker's name *is*
the reconciler counter it watches, so one naming something nothing bumps
observes `False` for ever.

## The execution boundary

`settle()`/`evaluate()` return a `CoreOutput`, and `CoreOutput.actions` is the
whole contract with the execution layer. An `ActionRequest` is frozen and
carries `(kind, intent, side, qty, price_hint, reduce_only, cls, reason,
target_bar_index)`.

| kind | when | notes |
|---|---|---|
| `TRIGGER` | `evaluate` | an intrabar fill both paths confirmed; `price_hint` is the probe's own fill price |
| `MARKET_AT_OPEN` | `settle` **and** `evaluate` | a MARKET resting in the settled book, filling at bar `n+1`'s open. `price_hint` is `None` — the open *is* the price (no fallback; B3 waits `open_wait_ms` for it). An order that reverses a live position is TWO legs: a reduce-only close, then the engine's own opened qty. `settle(n)` emits it as advance notice; the first `evaluate()` of bar `n+1` emits it again with `reason="open_requote"` and the engine's open-priced qty — a **supersede**, not a second order (see below) |
| `MARKET_NOW` | `settle` | a `process_orders_on_close` fill (`SETTLE_ONLY`): the order never rested, so there is no leg to ask for at the next open — the venue is taken to the ledger's position *now*, at this bar's close. `target_bar_index` is `n` |
| `SYNTHETIC_CLOSE` | `settle` | an engine-forced close (margin call / intraday cap), reduce-only. `intent` is the closed trade's exit id, or `__synthetic__` when the engine booked none (a margin call does) |
| `CORRECTION` | `settle` | a reconciler `MARKET_CORRECT` / `REDUCE_ONLY_TRIM` / `TOP_UP`; `cls` names which |
| `FLATTEN` | `settle` | reduce real exposure to zero. `cls` says which of two orders it is: `FLATTEN` — the reconciler's own, from a `TRIGGER_REVERSED`/`ENTRY_SLIP` fill, counted against `max_daily_reconciles` and budget-gated like any correction; or `HARD_FLAT` — spec §5.5(c)'s STOP action (see the STOP section), gated by neither |
| `CANCEL_STALE_CYCLE` | `settle` **and** `evaluate` | an intent that left the settled book **unfilled**; `book_diff` reads `CANCELLED` for a filled order too, so the bar's own *venue* fills disambiguate — a mirrored exit the ledger filled and the venue did not is still resting there and must be chased. From `evaluate` it withdraws a settled advance whose re-quote the STOP/RiskGuard refused. `qty` is 0 — a book op, not a fill |

`intent` is always the **Pine order id**, never an `IntentKey.s`: that is the
identity a venue fill is matched back against, so the request → fill →
classification round trip only closes in that id space. The intent *key* for a
book op is in `CoreOutput.book` / `book_diff`, keyed by `IntentKey.s`.

**The supersede contract.** A later `ActionRequest` for the same `(intent,
leg, target_bar_index)` — `leg` being `"EXIT"` when `reduce_only` else
`"ENTRY"`, the same pairing the classifier matches on — **replaces** the
earlier one. B3 submits (or amends to) the last one, and the venue fills it
once, not once per request. The one producer today is the `open_requote` pair
above; an executor that treated the two as separate orders would double every
entry and reversal, and the resulting position, being neither side's,
reconciles as `unreconcilable_sides` → `STOP(FLAT_ONLY)`.

**The local execution path uses explicit action receipts.**
`ExecutionCoordinator` retains the originating bar for settle-emitted actions
and records their venue execution separately from engine-fill classification.
Their signed quantity enters the cumulative account basis exactly once.
`DurableCore` commits settlement, reconcile, requests and checkpoint together,
so a crash before commit leaves the preceding decision and a crash afterward
recovers the existing outbox. Direct `LiveCore` callers do not acquire this
atomicity automatically. See [execution.md](execution.md).

### B3 inputs

Things the core has established that B3 has to honour or supply. They are not
defects — the core cannot decide them alone — but each one is a way B3 can be
wrong that nothing here will catch:

1. **`in_flight` is PENDING/PARTIAL only.** Resting ACKED conditionals belong in
   `mirrored`. Quiescence is `not in_flight`, so a driver that puts ACKED mirror
   orders in `in_flight` never reconciles a mirrored bar at all — and now
   escalates `disagree_twice` for it.
2. **The settle-time `MARKET_AT_OPEN` is notice, not an order.** Submit the
   re-quote, or the advance once the open is known and no re-quote/withdraw
   arrived. A `qty=0` supersede means *do not place it*.
3. **`VenueFill.leg` is `"EXIT"` iff `reduce_only`.** Build `VenueFill`s from
   fills with exactly that rule or the classifier's match key never matches.
4. **Action receipts for settle-emitted actions.** `CORRECTION`, `FLATTEN`,
   `SYNTHETIC_CLOSE` and `MARKET_NOW` all carry `target_bar_index = n` for a bar
   that is already closed — the execution layer retains that origin and records a separate action
   receipt when the venue reports the fill. It does not reclassify that
   receipt as a new engine fill at `n+1`.
5. **`hold_expired()` escalation.** The core answers "has the HARD/HOLD bound
   elapsed"; acting on it (flatten) is B3's sequencing, and belongs with the
   `HARD_FLAT` the core does emit.
6. **Breaker names are config.** A G3 breaker's `name` is the reconciler counter
   it watches; `BreakerTable.self_test` refuses one outside
   `reconcile.COUNTER_NAMES` at construction.
7. **Atomic restart path.** `drivers/checkpoint.py` restores the core's
   counters, pending notices, trigger deduplication, carried fills, probe
   history and breaker samples after a fresh G1-checked seed. It refuses
   settlements without a matching runtime checkpoint. A failed decision
   poisons that instance; it cannot continue with mutated memory.
8. **Intrabar level resolution.** `ProbeResult.level_resolved` captures the
   successful AUTO run's resolved bits before the alternate-path run can
   replace the handle's accessors. Newly resolved levels can be delivered
   during the first evaluation; disappeared orders are not mirrorable.
9. **B3's desired-set mirror sync** is the real guarantee that a `MISSED`
   mirrored exit's resting venue order is cancelled; the core's own
   `CANCEL_STALE_CYCLE` covers the book departure it can see.

What B3 owns and the core deliberately does not: client ids and `action_seq`,
the order state machine and adoption, mirror sync (`level_version`, dead-band,
create-before-cancel) from `CoreOutput.book` + `ProbeResult.levels`, dead-man
arming, rate lanes, the `DISASTER` pair, the `bar_mismatch` streak, the stream
and check drivers, and the venue-fed breakers (feed/eval staleness,
liquidation distance, unexplained divergence) that `RiskLimits` already
declares but nothing here can sample.

## Proving it: the L1 harness

`scripts/l1_harness.py` runs this cadence over a recorded feed against a
perfect venue and reports the two L1 assertions (spec §10.2):

- **G1** — checked by the ledger at every settlement, and then *independently*
  by an end-of-run reference pass that compares the final recompute's hash
  vector and trade prefixes against what each settlement journaled at the
  time. (The harness's per-bar read-back of the `n−1` row is not independent:
  `Ledger.settle` compares that same row before journaling and raises
  otherwise, so it can only ever agree.)
- **probe ≡ recompute** — every probe fill is a fill of that bar's own
  settlement or was retracted by a *later* evaluate on the same bar, read in
  tick order (fill → retract → fill leaves the fill standing, and it must
  settle). Path-variant fills are excluded: only `P_auto` confirmed them, so
  they settle as `PATH_DIVERGENT` — a classification, not an L1 failure.

Its perfect venue honours the supersede contract and the strict account
contract above, so those are exercised rather than assumed. A STOP anywhere in
the window — raised by `settle()` **or** by `evaluate()` — any incident, or any
non-`CONFIRMED` classification is reported per bar and in the summary, and a
STOP exits non-zero: a STOP mid-window refuses actions and silently changes the
very stream the two assertions are about. A refused `seed()` is reported the
same way (`report["seed"]`, exit 1) rather than dying with a message, and a
recompute that aborts twice ends the run with a written summary instead of a
traceback on the next bar's `LedgerGap`. Recompute times are float milliseconds:
a probe run can be sub-millisecond, and spec §2 sizes `grace` off their p99.
Run `python scripts/l1_harness.py --help` for its options; it needs the
ABI-v4 engine build from the README's Install section.
