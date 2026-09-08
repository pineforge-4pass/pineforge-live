"""`LiveCore`: the venue-neutral facade Plan B3's drivers (and the L1
harness) call -- spec §4 settle 1-8 and evaluate 1-4, §5.4's reconciler and
§5.5's STOP/RiskGuard, composed over Tasks 2-7.

Nothing here talks to a venue, a clock or a socket: `settle()` and
`evaluate()` are called WITH the bar/tick and the venue's own reported
fills/positions, and hand back `ActionRequest`s the execution layer turns
into orders. That keeps the whole core testable against a tape (the L1
harness in `tests/test_live_core.py`) and keeps every venue name out of it.

Engine-handle contract: `Ledger.settle()` captures its own settled book
inside the settle window (`SettleResult.book`) because
`handle.effective_levels`/`level_resolved`/`probe_fill_qty` describe the
handle's LAST `run_full()` only. `LiveCore.settle()` therefore reads
`s.book` rather than calling `settled_book()` again, and makes its
`probe_fill_qty` reads while the settle's own run is still the handle's
last one -- before any `evaluate()` probe run can invalidate them.
"""
from __future__ import annotations
import json
import math
import time
from dataclasses import dataclass, field
from pineforge_live import types as T
from pineforge_live.epoch import RuntimeConfig
from .book import Intent, IntentState, book_diff
from .classify import ClassifiedFill, FillClass, VenueFill, _side_for_leg, classify_bar, emulated_from_settle
from .ledger import BarsDivergence, Ledger, LedgerDivergence, RecomputeAborted, SettleResult
from .probe import Probe, ProbeResult
from .reconcile import COUNTER_NAMES, DeadBand, ReconcileConfig, ReconcileDecision, ReconcileInput, reconcile
from .riskguard import Breaker, BreakerTable, RateWindow, RiskGuard, RiskLimits, RiskViolation, StopController

DAY_MS = 86_400_000

#: The reconciler counters that mean "this decision declined to act for a
#: reason that can be different next bar" (M4a): a non-quiescent settle, a
#: correction the budget refused, one this run's own STOP level refused.
#: The MISSED fills of such a decision are re-presented at the next
#: settlement so they keep ageing against `[r4]`'s bound instead of being
#: dropped -- unlike a decision skipped by that bound itself, or by a
#: position gate, neither of which waiting can change.
CARRY_CAUSES = frozenset({"skipped_not_quiescent", "refused_budget", "refused_by_own_stop"})

#: `CorrectionRequest` kinds counted against `max_daily_reconciles` (m8),
#: and the reconciler counters that name them in a journaled `reconciles`
#: row -- the two must agree, since the day's tally is re-derived from
#: those rows after a restart.
RECONCILING_KINDS = frozenset({"CORRECTION", "FLATTEN"})
RECONCILING_COUNTERS = ("missed_corrected", "topped_up", "trimmed", "flattened")

#: `ActionRequest.intent` for an engine-forced close whose trade carries no
#: exit id at all (n8): a margin call books `exit_id == ""`, and B3 hashes
#: the intent into the order's client id, so an empty one would collide
#: across every such close.
SYNTHETIC_INTENT = "__synthetic__"


@dataclass(frozen=True)
class ActionRequest:
    """One order the execution layer (Plan B3) should place.

    `intent` is the PINE ORDER ID (`Intent.key.order_id` /
    `EmulatedFill.intent` / `ProbeFill.intent`), never an `IntentKey.s`:
    it is the identity `classify.classify_bar` matches a venue fill back
    against (`VenueFill.intent`), so the round trip only closes if the
    same id space is used on the way out. The intent KEY for a book op
    (which cycle's order to cancel/replace) is in `CoreOutput.book` /
    `CoreOutput.book_diff`, keyed by `IntentKey.s`.

    `qty` is always positive; `side` carries the direction.
    `price_hint` is the probe's own fill price for a TRIGGER and `None`
    for a MARKET_AT_OPEN (spec §4 settle 6: "no price fallback" -- the
    open is the price, and B3 waits `open_wait_ms` for it).
    `target_bar_index` is the bar the action is journaled against, which
    is what a venue fill's `target_bar_index` must equal to be
    identity-matched at that bar's settlement.

    SUPERSEDE CONTRACT: a later request for the same `(intent, leg,
    target_bar_index)` REPLACES the earlier one -- the executor submits
    (or amends to) the last one and the venue must fill it once, not
    once per request. The one producer of such a pair is spec §4 settle
    6's MARKET leg: `settle(n)` asks for it in advance, priced off the
    only price it has (bar n's close), and the first `evaluate()` of bar
    n+1 settles its fate with the open known. `leg` here is `"EXIT"` when
    `reduce_only` else `"ENTRY"`, the same pairing
    `classify.classify_bar` matches venue fills on.

    The settle-time advance is NOTICE, not an order: it exists so B3 can
    have the order in place at the open, and B3 submits it only once the
    open is known and the first `evaluate()` of the target bar has said
    which of three things it is (m5):

      * `reason="open_requote"` with the engine's own qty at the open --
        the advance's close-priced proxy was wrong by more than the dead
        band; this is the qty spec §4 settle 6 specifies, and B3 cannot
        re-derive it afterwards (the handle's accessors describe its LAST
        run);
      * nothing at all -- the two qtys agree within the dead band, so the
        advance already carries the right size;
      * `qty=0` with `reason="withdraw"` -- the engine's admission gate
        refused the order at the open, so there is nothing to place and
        the advance must NOT stand.
    """
    kind: str; intent: str | None; side: T.Side; qty: float; price_hint: float | None
    reduce_only: bool; cls: str; reason: str; target_bar_index: int


@dataclass
class CoreOutput:
    """One `seed()`/`settle()`/`evaluate()` call's whole outcome.

    `classified` is this settlement's `ClassifiedFill`s (empty for
    `seed`/`evaluate`) -- the reconciler's own input, surfaced so B3 can
    journal the fill classification and so a test can assert on it
    without re-deriving it. `incidents` are the dicts LiveCore ALSO
    journaled via `Journal.append_incident` (they are a return value for
    the caller's convenience, not the durable record). `stop` is the
    `(level, disposition, cause)` this call raised (or attempted to raise
    -- `StopController.raise_stop` is monotonic, so a weaker escalation
    while already STOPped is recorded here but does not lower the
    controller)."""
    settle: SettleResult | None = None
    probe: ProbeResult | None = None
    actions: list[ActionRequest] = field(default_factory=list)
    stop: tuple | None = None
    incidents: list[dict] = field(default_factory=list)
    reconcile: ReconcileDecision | None = None
    book: dict[str, Intent] = field(default_factory=dict)
    book_diff: dict[str, IntentState] = field(default_factory=dict)
    classified: list[ClassifiedFill] = field(default_factory=list)


def signed_qty(venue_fills: list[VenueFill]) -> float:
    """The signed qty of the fills WE are responsible for in one batch:
    `+` for BUY, `-` for SELL, `FillCause.OURS` only (a liquidation/ADL/
    manual fill is the venue's own action, never part of our basis).

    Public because a driver (and `scripts/l1_harness.py`'s perfect venue)
    has to move its account position by exactly the quantity `settle()`
    derives its basis from -- two spellings of "the signed qty of our
    fills" is precisely the drift `real_position` exists to detect."""
    return sum((v.qty if v.side is T.Side.BUY else -v.qty) for v in venue_fills if v.cause is T.FillCause.OURS)


class LiveCore:
    """Composes the ledger, probe, classifier, reconciler, RiskGuard and
    STOP controller into the two entry points a driver calls.

    Cadence: `seed(history)` once, then per script-TF bar
    `evaluate(forming, now)` on every coalesced tick and `settle(bar,
    ...)` on the confirmed kline. The per-bar RiskGuard budget is reset by
    `settle()` only -- `evaluate()` spends from the budget the preceding
    settlement opened, so a bar's whole tick stream shares one
    `max_fill_actions_per_bar` allowance (spec §4's "one TRIGGER per
    intent per cycle; `max_fill_actions_per_bar` ≤ P's fill count").
    """

    def __init__(self, handle, spec, journal, marker, runtime_config: RuntimeConfig, limits: RiskLimits,
                 dead_band: DeadBand, breakers: list[Breaker], trail_refresh_policy: str = "bar_open_level",
                 reconcile_cfg: ReconcileConfig | None = None):
        self.h, self.spec, self.j, self.marker, self.rc = handle, spec, journal, marker, runtime_config
        self.ledger = Ledger(handle, spec, journal, runtime_config.hash())
        self.probe = Probe(handle, spec, self.ledger, trail_refresh_policy)
        self.limits, self.dead_band = limits, dead_band
        self.guard = RiskGuard(limits)
        self.stop = StopController(journal, marker, limits.hard_stop_max_hold_ms)
        self.stop.restore()
        # G3 (spec §1): self-tested at construction so a breaker that could
        # never fire (UB_95(0, n_min) >= theta, or window_n < n_min) is a
        # startup error, not a silent no-op. Each breaker owns a RateWindow
        # keyed by its own name; `settle()` observes one sample per bar --
        # "did the reconciler's counter of that name fire this bar". m2:
        # the self-test is handed the reconciler's own counter vocabulary,
        # so a breaker watching a name nothing bumps is a startup error
        # too rather than a lane that looks configured and never fires.
        self.g3 = BreakerTable(breakers); self.g3.self_test(COUNTER_NAMES)
        self.g3_windows: dict[str, RateWindow] = {b.name: RateWindow(b.window_n) for b in breakers}
        self.rcfg = reconcile_cfg or ReconcileConfig(1, 30.0, limits.max_order_notional, 3, False)
        self.book: dict[str, Intent] = {}
        # n12/m8: the two per-UTC-day tallies. Both are re-derived from
        # today's journaled `reconciles` rows at construction, so neither
        # cap is launderable by restarting the process; `_roll_day` zeroes
        # them at the settled bar's own day boundary from then on.
        self.mirror_early_today, self.reconciles_today = self._restore_day_counters()
        self._day: int | None = None
        # spec §4 settle 6: the MARKET legs this settlement asked for at the
        # NEXT bar's open, keyed by (intent id, leg). `evaluate()` on that
        # bar suppresses its own TRIGGER for the same (intent, leg) -- they
        # are the same fill, and `[r4]` allows one open action per intent.
        self.pending_market: dict[tuple[str, str], ActionRequest] = {}
        # (intent id, leg) already TRIGGERed on the bar being evaluated --
        # the probe re-reports a confirmed fill on every tick of the bar.
        self._triggered_bar: int | None = None
        self._triggered: set[tuple[str, str]] = set()
        self._our_fills = 0.0
        self.missed_since: dict[tuple[str, str], int] = {}
        # M4(a): the MISSED fills of a decision that declined to act for a
        # reason that can change (CARRY_CAUSES), re-presented at the next
        # settlement.
        self._carried_missed: list[ClassifiedFill] = []
        # M4(b): consecutive settlements the reconciler skipped as not
        # quiescent -- spec §5.4's "skip (bounded) and count toward
        # disagree_twice".
        self._not_quiescent_streak = 0
        # M3: the HARD_FLAT this run has already asked for, so a restart
        # (which re-reads it off the journal) re-issues one only if the
        # venue still holds a position.
        self._hard_flat_issued = False
        # m7: the horizon alert is one incident per crossing, not one per bar.
        self._horizon_alerted = False
        # m11: exposure this bar's already-permitted requests committed.
        self._bar_committed_qty = 0.0
        journal.append_epoch(spec.epoch_hash(), "{}")
        journal.append_runtime_config(runtime_config.hash(), "{}")

    # --- state a caller may read -------------------------------------------------
    @property
    def our_signed_fills(self) -> float:
        """The v1 fallback basis (spec §5.4's PRIMARY comparison): the
        cumulative signed qty of the fills WE are responsible for, `+` for
        BUY and `-` for SELL, over every `FillCause.OURS` venue fill
        `settle()` has been handed since the ledger was last flat.

        Anchored at `seed()` on the ledger position the seed adopted: a
        campaign that starts mid-position (every corpus fixture does --
        the always-in-market reversal scripts are never flat) has no
        "since flat" boundary to count from, and starting at 0 would make
        the basis disagree with both the ledger and the account by the
        adopted size for the life of the process. Reset to 0.0 whenever a
        settlement leaves the ledger flat AND this accumulator agrees the
        venue is flat too (within the dead-band), which re-establishes the
        exact per-cycle meaning the moment both sides do go flat. A ledger
        that settles flat over an uncorrected venue residual (a MISSED EXIT
        the same settlement is correcting) keeps carrying it: resetting
        there would read the correction's own fill, one bar later, as a
        position out of nowhere.

        Plan B3 passes the exchange-derived value to
        `settle(..., our_signed_fills=...)` instead; an explicit value is
        used for that call only and never mutates this accumulator (the
        two are different estimates of the same quantity, and mixing them
        would corrupt the fallback for whichever call comes next)."""
        return self._our_fills

    # --- helpers -----------------------------------------------------------------
    def _incident(self, out: CoreOutput, kind: str, **detail) -> None:
        """Journals an incident AND reports it on `out.incidents` -- the
        journal row is the durable record, the returned dict is what a
        driver/test reads without querying sqlite."""
        row = {"kind": kind, **detail}
        out.incidents.append(row)
        self.j.append_incident(kind, detail)

    def _raise(self, out: CoreOutput, level: T.StopLevel, disp: T.StopDisposition, cause: str) -> None:
        self.stop.raise_stop(level, disp, cause)
        out.stop = (level, disp, cause)

    def _permitted(self, out: CoreOutput, a: ActionRequest, position: float, price: float) -> bool:
        """The two gates every request passes before it reaches
        `out.actions` (ruling 4 / spec §5.5): the STOP controller's own
        `permits()`, then the RiskGuard's `check_*` budgets. A refusal is
        an incident (`action_refused_by_stop` / `risk_refused`), never a
        silent drop -- an order the core wanted and did not send is
        exactly the thing an operator must be able to find afterwards.

        `action_kind` mapping: a cancel is always permitted; a FLATTEN is
        the `hard_flat` reduce-only pass (allowed under `HARD` only with
        disposition `FLATTEN`); everything else is `entry` when it
        increases exposure and `reduce` when it does not. A SYNTHETIC
        close is deliberately a plain `reduce`, NOT `static_exit`: under
        `HARD` the spec allows only the dead-man and re-established
        protective exits, and an engine-side margin-call close is neither.
        """
        kind = "cancel" if a.kind == "CANCEL_STALE_CYCLE" else ("hard_flat" if a.kind == "FLATTEN" else
                                                                ("entry" if not a.reduce_only else "reduce"))
        if not self.stop.permits(kind, increases_exposure=not a.reduce_only, reduce_only=a.reduce_only):
            self._incident(out, "action_refused_by_stop", action=a.kind, intent=a.intent, qty=a.qty,
                           level=self.stop.level.value, disposition=self.stop.disposition.value)
            return False
        if a.qty > 0.0:
            cause = self.guard.check_order_notional(a.qty, price)
            if cause is None and not a.reduce_only:
                # Conservative bound: an exposure-increasing action can at
                # most add its whole qty to the side already held -- plus
                # whatever this bar's already-permitted requests committed
                # (m11). `position` is the SETTLED position, so two
                # same-bar pyramiding TRIGGERs on distinct intents each
                # passed on their own while their SUM breached
                # `max_abs_position`.
                cause = self.guard.check_position(abs(position) + self._bar_committed_qty + a.qty, price)
            if cause is not None:
                self._incident(out, "risk_refused", action=a.kind, intent=a.intent, qty=a.qty, cause=cause)
                return False
            if not a.reduce_only:
                self._bar_committed_qty += a.qty
        return True

    def _market_legs(self, out: CoreOutput, s: SettleResult, price: float) -> list[ActionRequest]:
        """spec §4 settle 6: every MARKET order resting in the settled book
        fills at the NEXT bar's open, with the ENGINE's own qty
        (`strategy_pending_order_fill_qty`) -- the runtime never
        re-implements the sizing rules.

        That accessor answers "the contracts the entry kernel would OPEN",
        which is NOT the whole venue transaction when the order reverses a
        live position: its doxygen names the engine's own branch
        `close_opposite_then_enter` and tells a consumer to compare the
        returned qty with the live position. So an order whose side
        opposes the settled position is submitted as TWO legs -- a
        reduce-only close of the live position, then the opened qty --
        which is also exactly how the ledger reports it back at the next
        settlement (a closed-trade EXIT leg plus a position-delta ENTRY
        leg), so both halves have a venue counterpart to match against.
        `close_only=1` means the kernel opens nothing: only the close leg
        is submitted, bounded by the live position.

        Read here, inside the settle window, because `probe_fill_qty`
        describes the handle's LAST run -- which is this settlement's own
        recompute until the next `evaluate()` probe runs.
        """
        legs: list[ActionRequest] = []
        pos = s.position_size
        band = self.dead_band.qty(price)
        for it in self.book.values():
            if not it.is_market:
                continue
            rc, qty, close_only, partition = self.h.probe_fill_qty(it.index, price)
            if rc != 0 or math.isnan(qty):
                self._incident(out, "market_qty_unavailable", intent=it.key.order_id, index=it.index, rc=rc)
                continue
            opposite = pos != 0.0 and it.is_long != (pos > 0)
            if close_only:
                # `close_only=1` is the kernel reporting that it opens
                # NOTHING: for the two MARKET reversal kernels `qty` is then
                # the UNOPENED REMAINDER (<= kQtyEpsilon, engine_fills.cpp
                # `close_opposite_then_enter`), never the size it closes --
                # the ABI exposes no closed qty at all, and the kernel closes
                # `min(tx, live)`, i.e. the whole live position for a full
                # transaction. Sizing this leg `min(|qty|, |pos|)` therefore
                # produced a sub-dead-band leg that was dropped, the venue
                # never closed, and the ledger's own close read MISSED a bar
                # later. A partial transaction surfaces as QTY_DIVERGENT at
                # the next settlement, which the reconciler trims.
                close_qty, open_qty = (abs(pos) if opposite else 0.0), 0.0
            else:
                close_qty, open_qty = (abs(pos) if opposite else 0.0), qty
            side = T.Side.BUY if it.is_long else T.Side.SELL
            reason = f"settled MARKET order (partition {partition}, close_only {int(bool(close_only))})"
            if close_qty > band:
                legs.append(ActionRequest("MARKET_AT_OPEN", it.key.order_id, side, close_qty, None, True,
                                          "MARKET", reason, s.bar_index + 1))
            if open_qty > band:
                legs.append(ActionRequest("MARKET_AT_OPEN", it.key.order_id, side, open_qty, None, False,
                                          "MARKET", reason, s.bar_index + 1))
        return legs

    def _missed_bounds(self, classified: list[ClassifiedFill], bar_index: int, price: float) -> tuple[int, float]:
        """The `(missed_age_bars, missed_distance_bps)` pair the reconciler
        bounds a MISSED correction with (spec `[r4]`).

        Age is tracked ACROSS settlements per `(intent, leg)`: the first
        bar that pair read MISSED is remembered, its age is
        `bar_index - that bar`, and the entry is dropped the moment the
        pair stops being MISSED (it filled, went IN_FLIGHT, or was
        corrected) so a later recurrence starts its own clock. The value
        passed is the MAXIMUM age over the currently-MISSED pairs (0 when
        none), i.e. the oldest thing still missing -- the reconciler
        itself takes `max(this, bar_index - fill.bar_index)`, so passing
        the oldest can only make the bound stricter, never laxer.

        Distance is measured against the SAME fill the reconciler will
        pick as its representative -- the first MISSED ENTRY if there is
        one, else the first MISSED EXIT -- rather than an arbitrary fill,
        so the bps figure describes the correction actually under
        consideration.

        Note that a non-zero age requires the SAME `(intent, leg)` to read
        MISSED on CONSECUTIVE settlements: `emulated_from_settle` only ever
        emits bar-n fills, so a single uncorrected MISSED does not keep
        ageing by itself -- readers of ruling 2 otherwise expect a growth
        the cadence cannot produce."""
        missed = [c for c in classified if c.cls == FillClass.MISSED and c.emulated is not None]
        keys = {(c.emulated.intent, c.emulated.leg) for c in missed}
        for k in [k for k in self.missed_since if k not in keys]:
            del self.missed_since[k]
        for k in keys:
            self.missed_since.setdefault(k, bar_index)
        age = max((bar_index - first for first in self.missed_since.values()), default=0)
        rep = next((c for c in missed if c.emulated.leg == "ENTRY"), None) or (missed[0] if missed else None)
        dist = abs(price - rep.emulated.price) / price * 1e4 if rep is not None and price > 0 else 0.0
        return age, dist

    def _horizon_ok(self, out: CoreOutput) -> bool:
        """spec §2's `horizon_bars` consumption check, run BEFORE the
        recompute this settlement would otherwise do (m7).

        `horizon_bars` is the frozen `last_bar_index` the epoch's
        `realtime_tail` pins `pine_last_bar_index()` to (spec §3.1), so
        settling past it runs the engine with a `last_bar_index` BELOW the
        actual last bar -- every `barstate`/`last_bar_index`-sensitive
        script silently changes behaviour. Spec §2: "at 80% consumption
        the runtime alerts and at 100% forces an epoch rotation before the
        next settlement." The alert is one incident per crossing (not one
        per bar); the rotation is `STOP(FLAT_ONLY)` plus a REFUSED settle
        -- refusing is what "before the next settlement" means, and the
        rotation itself is an operator ceremony (spec §6), not something
        the core performs. Returns False when the settle must not run."""
        state = self.guard.horizon(self.ledger.n + 1, self.spec.horizon_bars)
        if state == "rotate":
            self._incident(out, "horizon_exhausted", consumed=self.ledger.n + 1, horizon_bars=self.spec.horizon_bars)
            self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "horizon")
            return False
        if state == "alert":
            if not self._horizon_alerted:
                self._horizon_alerted = True
                self._incident(out, "horizon_alert", consumed=self.ledger.n + 1, horizon_bars=self.spec.horizon_bars)
        else:
            self._horizon_alerted = False
        return True

    def _observe_breakers(self, counters: dict[str, int]) -> list[tuple[str, bool, bool]]:
        """One G3 sample per settlement per breaker: a breaker's `name` IS
        the reconciler counter it watches, so the sample is "did that
        counter fire this bar". Returns `(name, breached, alerting)`
        triples -- `breached` is the decidable rate breach (the window has
        reached `n_min`), `alerting` the pre-decidable warning (`alert()`:
        something has fired but the window is still too short for a rate
        to mean anything). The caller escalates on the first and journals
        an incident on the second."""
        out = []
        for b in self.g3.breakers:
            w = self.g3_windows[b.name]
            w.observe(counters.get(b.name, 0) > 0)
            out.append((b.name, w.breached(b), w.alert(b)))
        return out

    # --- lifecycle ---------------------------------------------------------------
    def seed(self, history, expected_trades_sha256: str | None = None, *,
             real_position: float | None = None, adopt_position: bool = False) -> CoreOutput:
        """Seed the ledger from `history` (spec §4 settle 1, the restart
        path). A `LedgerDivergence` -- a trades digest that does not
        reproduce, or a journal conflict against a previous incarnation --
        is STOP(HARD, HOLD) per spec §4.1: the recompute disagrees with a
        durable record, which is never something the runtime may trade
        through.

        `real_position` is the venue account's own position at seed time
        (m6). Given, it anchors the fallback basis
        (`our_signed_fills`) on the VENUE rather than on the recompute --
        which is what a restart must do: a legitimate hold-flat state
        (MIRROR_EARLY: venue 0, ledger +1, no STOP) re-anchored to the
        ledger comes back as basis +1 against a real 0, i.e. an
        `account_mismatch` STOP on the first settlement of a state the
        uninterrupted run was carrying happily. Any carried residual has
        the same shape.

        A seed-time disagreement beyond the dead-band is spec §6's "cold
        start with an existing position -> refuse unless
        `--adopt-position`": `STOP(HARD, HOLD, "cold_start_position")` and
        the seed is refused, unless `adopt_position` says the operator has
        looked at it and wants the ledger's own view adopted."""
        out = CoreOutput()
        try:
            s = self.ledger.seed(history, expected_trades_sha256)
        except LedgerDivergence as e:
            self._raise(out, T.StopLevel.HARD, T.StopDisposition.HOLD, f"seed:{e.cause}")
            self._incident(out, "ledger_divergence", cause=e.cause, detail=str(e.detail))
            return out
        self.book = s.book
        self._our_fills = s.position_size if real_position is None else real_position
        if real_position is not None and not adopt_position and abs(real_position - s.position_size) > self.dead_band.qty(s.bar.c):
            # The ledger itself committed before this check (it has to --
            # the disagreement is only knowable once the recompute has a
            # position to compare). What is refused is the SEED: `out.settle`
            # stays None, which is the caller's "do not start trading"
            # signal, and the process is HARD/HOLD until an operator either
            # clears it or restarts with `adopt_position`.
            self._raise(out, T.StopLevel.HARD, T.StopDisposition.HOLD, "cold_start_position")
            self._incident(out, "cold_start_position", real_position=real_position,
                           ledger_position=s.position_size, dead_band=self.dead_band.qty(s.bar.c))
            return out
        out.settle, out.book = s, self.book
        return out

    def settle(self, bar: T.NormalizedBar, venue_fills: list[VenueFill], in_flight: set[str], mirrored: set[str],
               real_position: float, now_ms: int, *, our_signed_fills: float | None = None) -> CoreOutput:
        """spec §4 settle 1-8 for one confirmed script-TF bar.

        Order of operations: recompute the ledger (1-3) -> settled book and
        its diff (5) -> classify this bar's ledger fills against
        `venue_fills` (4) -> reconcile (7) -> raise whatever STOP the
        reconciliation demands -> only THEN build the bar's action requests
        (6) and filter every one of them through the STOP/RiskGuard gate ->
        journal the reconcile row (8, the settlement row itself is the
        ledger's). Building actions only after the escalation is what stops
        a settlement from shipping an exposure-increasing order it has
        already decided to STOP over.

        `real_position` is the venue account's own reported position (spec
        §5.4's SECONDARY check); `our_signed_fills` is the PRIMARY basis
        and is derived when not supplied -- see `our_signed_fills`.
        `in_flight`/`mirrored` are sets of Pine order ids: intents with a
        non-terminal action (never classified MISSED, `[r4]`) and intents
        currently mirrored as resting venue orders (§5.2).

        A `LedgerGap` (a non-contiguous or still-forming bar) is NOT caught
        here: it means the caller handed the core the wrong bar, and gap
        carry-forward is the bar layer's job (spec §2), not a STOP. It
        propagates so the driver fixes its own feed rather than the core
        silently settling a hole.
        """
        out = CoreOutput()
        if not self._horizon_ok(out):
            return out
        prev = self.ledger.last
        try:
            s = self.ledger.settle(bar, now_ms)
        except BarsDivergence as e:
            # spec §4.1: a revised settled bar is a feed problem, not a
            # divergent recompute -- FLAT_ONLY, not HARD. (n1: the LEDGER
            # journals the `bars_divergence` incident row; this copy is
            # returned to the caller only, so the durable record holds one
            # row per revised bar, not two.)
            out.incidents.append({"kind": "bars_divergence", "detail": str(e)})
            self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "bars_divergence")
            return out
        except LedgerDivergence as e:
            self._raise(out, T.StopLevel.HARD, T.StopDisposition.HOLD, f"g1:{e.cause}")
            self._incident(out, "ledger_divergence", cause=e.cause, detail=str(e.detail))
            return out
        except RecomputeAborted:
            # Nothing was journaled and the ledger is untouched: the same
            # bar can be re-settled, so this is an incident, not a STOP.
            self._incident(out, "recompute_aborted", ts_open=bar.ts_open)
            return out
        if s is prev:
            # M1, spec §4.8: `Ledger.settle` returns the LAST settlement
            # unchanged for a byte-identical re-delivery of the bar it
            # already settled -- check mode's REST catch-up delivers one
            # routinely. Re-running the whole settlement over it would
            # emulate the bar's fills a second time against whatever
            # `venue_fills` the driver passes (normally none, since they
            # were consumed), classify them MISSED, reconcile again -- a
            # DUPLICATE correction is real money -- append a second
            # `reconciles` row for the bar, and reset `pending_market`
            # from re-derived legs against a possibly stale
            # `probe_fill_qty`. Nothing is classified, reconciled, asked
            # for or journaled: the settlement already happened. The note
            # is returned, not journaled -- a re-delivery is an expected
            # event of the protocol, not an anomaly to alert on.
            out.settle, out.book = s, self.book
            out.incidents.append({"kind": "idempotent_redelivery", "bar_index": s.bar_index})
            return out
        self.guard.begin_bar()
        self._bar_committed_qty = 0.0
        out.settle = s

        prev_book = self.book
        self.book = s.book
        out.book, out.book_diff = self.book, book_diff(prev_book, self.book)
        emulated = emulated_from_settle(s, out.book_diff, prev_book)
        price = bar.c
        band = self.dead_band.qty(price)

        basis = self._advance_our_fills(signed_qty(venue_fills)) if our_signed_fills is None else our_signed_fills
        classified = classify_bar(emulated, venue_fills,
                                  in_flight_intents=in_flight, mirrored_intents=mirrored, dead_band_qty=band,
                                  ledger_position=s.position_size, real_position=real_position,
                                  max_entry_slip_bps=self.rcfg.max_entry_slip_bps)
        # M4(a): a MISSED the PREVIOUS decision declined to act on for a
        # reason that can change is re-presented here, after this bar's own
        # fills (fresher information first). `emulated_from_settle` emits
        # bar-n fills only and `classify_bar` has no memory, so without
        # this a MISSED skipped as non-quiescent simply vanished: nothing
        # aged it, nothing re-counted it, and `[r4]`'s "≤ 1 script bar old"
        # bound could never bind because no pair ever read MISSED twice.
        classified = classified + self._carried_missed
        self._carried_missed = []
        out.classified = classified

        age, dist = self._missed_bounds(classified, s.bar_index, price)
        self._roll_day(bar)
        dec = reconcile(ReconcileInput(s.bar_index, classified, s.position_size, real_position, basis, price,
                                       quiescent=not in_flight, in_flight=in_flight, stop_level=self.stop.level,
                                       missed_age_bars=age, missed_distance_bps=dist, cfg=self.rcfg,
                                       dead_band=self.dead_band, mirror_early_today=self.mirror_early_today))
        self.mirror_early_today += dec.counters.get("mirror_early", 0)
        out.reconcile = dec
        if dec.stop is not None:
            self._raise(out, *dec.stop)
        if dec.skipped_cycle:
            self._incident(out, "cycle_skipped", bar_index=s.bar_index)
        if CARRY_CAUSES & set(dec.counters):
            self._carried_missed = [c for c in classified if c.cls is FillClass.MISSED]
        # M4(b), spec §5.4: "else skip (bounded) and count toward
        # disagree_twice". A driver whose actions never go terminal makes
        # every settlement non-quiescent, and the reconciler then does
        # NOTHING, bar after bar, with only a counter to show for it --
        # `disagree_twice` is the declared bound on exactly that.
        if "skipped_not_quiescent" in dec.counters:
            self._not_quiescent_streak += 1
            if self._not_quiescent_streak >= self.limits.disagree_twice:
                self._incident(out, "disagree_twice", settles=self._not_quiescent_streak)
                self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "disagree_twice")
        else:
            self._not_quiescent_streak = 0
        for name, breached, alerting in self._observe_breakers(dec.counters):
            if breached:
                self._incident(out, "g3_breached", breaker=name)
                self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, f"g3:{name}")
            elif alerting:
                self._incident(out, "g3_alert", breaker=name)

        capped = False
        for a in self._requests(out, s, dec, classified, price, prev_book, in_flight, mirrored, real_position):
            if a.kind in RECONCILING_KINDS and self.reconciles_today >= self.limits.max_daily_reconciles:
                # m8, spec §5.5: `max_daily_reconciles` is the low-n guard
                # on corrections -- the G3 rate breakers are alert-only
                # below their own `n_min`, so a runtime correcting every
                # bar would reach no decidable rate for a year. Past the
                # cap the cycle is SKIPPED, like any other refusal to act.
                self._incident(out, "max_daily_reconciles", action=a.kind, intent=a.intent,
                               today=self.reconciles_today, cap=self.limits.max_daily_reconciles)
                capped = True
                continue
            if self._permitted(out, a, s.position_size, price):
                out.actions.append(a)
                if a.kind in RECONCILING_KINDS:
                    self.reconciles_today += 1
        if capped and not dec.skipped_cycle:
            self._incident(out, "cycle_skipped", bar_index=s.bar_index)
        # The MARKET legs asked for at the next bar's open, as ADVANCE
        # NOTICE: that bar's first `evaluate()` decides each one's fate
        # with the open known -- confirmed at the same size (nothing to
        # do), confirmed at another (a superseding `open_requote`), or not
        # confirmed at all (a `qty=0` withdraw). See `evaluate` and
        # `ActionRequest`'s supersede contract.
        self.pending_market = {(a.intent, "EXIT" if a.reduce_only else "ENTRY"): a
                               for a in out.actions if a.kind == "MARKET_AT_OPEN" and a.intent is not None}
        # The per-cycle reset (see `our_signed_fills`) needs BOTH sides
        # flat. A settlement that leaves the LEDGER flat while an
        # uncorrected venue residual stands -- a MISSED EXIT this same
        # settlement just issued a `MARKET_CORRECT` for -- must carry it:
        # resetting to 0 there drove the next bar's basis to `-qty` (the
        # correction's own OURS fill) against a real position of 0, i.e. a
        # false `account_mismatch` STOP one bar after a correct repair.
        # NEW-4: "the venue is flat" is the VENUE's own word
        # (`real_position`), not our accumulator's. Guarding on `|basis|`
        # made the accumulator sticky exactly when it was wrong: a
        # liquidation/ADL flattens the venue while the accumulator still
        # carries our position, so `|basis|` never fell inside the band,
        # the reset never fired, and every later bar re-raised
        # `account_mismatch` until an operator restart re-seeded.
        if s.position_size == 0.0 and our_signed_fills is None and abs(real_position) <= band:
            self._our_fills = 0.0
        self._journal_intents(out.book_diff)
        self.j.append_reconcile({"epoch_hash": self.spec.epoch_hash(), "bar_index": s.bar_index,
                                 "cause": ",".join(sorted(dec.counters)) or "clean",
                                 "detail_json": json.dumps(dec.counters, sort_keys=True)})
        return out

    def _journal_intents(self, diff: dict[str, IntentState]) -> None:
        """n9, spec §6: the settled book's own transitions, written to the
        journal's `intents` table (`INSERT OR REPLACE` per
        `(epoch, intent_key)`, so the row holds each intent's LATEST
        state).

        The book and its diff are the core's own product -- nothing else
        derives them -- and B3 needs the durable copy to re-run a
        journaled STOP's cancel step on restart (spec §6: "re-run a
        journaled STOP's cancel step and non-widening check") without
        first recomputing the ledger to find out what was resting. The
        payload is the intent's own resting shape, which is what a mirror
        sync compares against."""
        for key, state in diff.items():
            it = self.book.get(key)
            payload = {"stop": it.stop, "limit": it.limit, "activation": it.activation, "is_long": it.is_long,
                       "kind": it.kind, "qty": it.qty, "level_resolved": it.level_resolved,
                       "content_hash": it.content_hash} if it is not None else {}
            self.j.append_intent(self.spec.epoch_hash(), key, state.value, json.dumps(payload, sort_keys=True))

    def _advance_our_fills(self, ours_delta: float) -> float:
        self._our_fills += ours_delta
        return self._our_fills

    def _roll_day(self, bar: T.NormalizedBar) -> None:
        """`mirror_early_daily_cap` and `max_daily_reconciles` are PER-DAY
        counts (spec §5.4/§5.5), so both tallies reset on the UTC-day
        boundary of the settled bar's own open rather than growing for the
        life of the process."""
        day = bar.ts_open // DAY_MS
        if self._day != day:
            self._day, self.mirror_early_today, self.reconciles_today = day, 0, 0

    def _restore_day_counters(self) -> tuple[int, int]:
        """Today's `mirror_early` count and reconciling-action count, read
        back from the journaled `reconciles` rows (n12, m8).

        Both are per-day CAPS, and a cap that restarts at zero with the
        process is a cap an operator can launder by bouncing the runtime --
        the very thing the mirror-early cap exists to prevent. Each row's
        `detail_json` is that decision's own counter bag, so the day's
        totals are a sum over it.

        The day here is the WALL-CLOCK UTC day (`reconciles` rows carry
        `created_ms`, not the settled bar's `ts_open`), while `_roll_day`
        rolls on the bar's own day. The two agree in a live run, where bars
        settle as they close; they can disagree when a tape is replayed
        through the same journal, and the restore is then a bounded
        over-count on the first bar -- conservative in the direction that
        matters (the cap binds sooner, never later)."""
        day_start = int(time.time() * 1000) // DAY_MS * DAY_MS
        mirror_early = reconciling = 0
        for row in self.j.rows("reconciles", "epoch_hash=? AND created_ms>=?", (self.spec.epoch_hash(), day_start)):
            counters = json.loads(row["detail_json"] or "{}")
            mirror_early += int(counters.get("mirror_early", 0))
            reconciling += sum(int(counters.get(k, 0)) for k in RECONCILING_COUNTERS)
        return mirror_early, reconciling

    def _requests(self, out: CoreOutput, s: SettleResult, dec: ReconcileDecision,
                  classified: list[ClassifiedFill], price: float, prev_book: dict[str, Intent],
                  in_flight: set[str], mirrored: set[str], real_position: float) -> list[ActionRequest]:
        """This settlement's candidate actions, in submission order:
        engine-forced closes and this bar's settle-only (POOC) fills
        first, then the reconciler's corrections, then a HARD_FLAT if the
        STOP now demands one, then the next open's MARKET legs, then
        stale-cycle cancels.

        A correction's `reduce_only` is the RECONCILER's own (F1): a
        `MARKET_CORRECT` is exposure-increasing for a missed ENTRY and
        reduce-only for a missed EXIT, and only `reconcile` knows which
        branch it took -- re-deriving the flag from `corr.kind` here
        labelled the missed-EXIT repair exposure-increasing, so `permits()`
        refused under FLAT_ONLY the one order FLAT_ONLY exists to allow
        (spec §5.4/§5.5) and B3 was told it was not reduce-only."""
        reqs: list[ActionRequest] = []
        for c in classified:
            if c.cls == FillClass.SYNTHETIC and c.emulated is not None:
                e = c.emulated
                reqs.append(ActionRequest("SYNTHETIC_CLOSE", e.intent or SYNTHETIC_INTENT,
                                          _side_for_leg(e.is_long, "EXIT"), e.qty, None,
                                          True, "SYNTHETIC", "engine-side close (margin call / intraday cap)", s.bar_index))
            elif c.cls == FillClass.SETTLE_ONLY and c.emulated is not None:
                # M5, spec §4 settle 6: "`process_orders_on_close` fills ->
                # MARKET now". The order never rested in the settled book,
                # so there is no MARKET leg to ask for at the next open and
                # no venue counterpart to have missed -- the fill happened
                # at THIS bar's close and the venue has to be taken there
                # now. Routing it through the reconciler as MISSED instead
                # made every POOC fill a repair (2% of bars on the corpus
                # POOC probe -- straight through the spec's own 1% orphan
                # breaker), gated it behind an age/distance/budget bound
                # the spec does not put on it, and turned a same-bar POOC
                # flip into `unreconcilable_sides` on the first cross.
                e = c.emulated
                reqs.append(ActionRequest("MARKET_NOW", e.intent, _side_for_leg(e.is_long, e.leg), e.qty, None,
                                          e.leg == "EXIT", "MARKET",
                                          "process_orders_on_close fill (spec §4 settle 6)", s.bar_index))
        for corr in dec.corrections:
            kind = "FLATTEN" if corr.kind == "FLATTEN" else "CORRECTION"
            reqs.append(ActionRequest(kind, corr.intent, corr.side, corr.qty, None, corr.reduce_only, corr.kind,
                                      corr.reason, s.bar_index))
        # M3, spec §5.5(c): "under FLATTEN: one reduce-only HARD_FLAT
        # MARKET to the venue-reported position, then only cancels". This
        # is the ONE order the reconciler cannot emit -- it is not a
        # correction toward the ledger but the STOP's own disposition
        # acting on venue truth -- and without it an ADL/manual/partial
        # liquidation left the remaining venue position sitting under
        # HARD, where `permits()` allows no new reduce-only order except
        # this one, indefinitely. Emitted once: `_hard_flat_issued` is
        # journaled as an incident so a restart re-issues only if the
        # venue still holds a position.
        if (self.stop.disposition is T.StopDisposition.FLATTEN and real_position != 0.0
                and not any(a.kind == "FLATTEN" for a in reqs)):
            if not self._hard_flat_issued:
                self._hard_flat_issued = True
                self._incident(out, "hard_flat", qty=abs(real_position), cause=self.stop.cause)
                reqs.append(ActionRequest("FLATTEN", None, T.Side.SELL if real_position > 0 else T.Side.BUY,
                                          abs(real_position), None, True, "HARD_FLAT", self.stop.cause, s.bar_index))
        reqs += self._market_legs(out, s, price)
        # spec §5.2 stale-cycle cancels. `book_diff` reads CANCELLED for an
        # order that FILLED as well as one that genuinely left the book
        # (see `IntentState`); the bar's own emulated fills disambiguate, so
        # a filled order is never chased with a cancel.
        # m3: VENUE truth, not ledger truth. `book_diff` reads CANCELLED
        # for an order that FILLED as well as one that left the book
        # unfilled, and the bar's own fills disambiguate -- but only a
        # fill the VENUE reported proves the venue is no longer holding
        # the order. A mirrored exit the ledger filled and the venue did
        # not (MISSED, being repaired by a MARKET_CORRECT) is exactly the
        # resting `closePosition` order that must be chased with a cancel,
        # or it fires again on the next cycle's position.
        filled = {c.emulated.intent for c in classified if c.venue is not None and c.emulated is not None}
        placed = in_flight | mirrored
        for k, state in out.book_diff.items():
            if state != IntentState.CANCELLED:
                continue
            gone = prev_book.get(k)   # a CANCELLED key is gone from the CURRENT book by construction
            if gone is None or gone.key.order_id in filled:
                continue
            order_id = gone.key.order_id
            # N3: there is nothing to cancel unless the venue is actually
            # holding the order -- one we mirrored as a resting order
            # (§5.2) or have a non-terminal action for. Every OTHER book
            # departure is a ledger-side cycle rotation the venue never saw.
            if order_id not in placed:
                continue
            reqs.append(ActionRequest("CANCEL_STALE_CYCLE", order_id, T.Side.BUY, 0.0, None, False, "CANCEL",
                                      f"intent {k} left the settled book unfilled", s.bar_index))
        return reqs

    # --- intrabar ----------------------------------------------------------------
    def evaluate(self, forming: T.NormalizedBar, now_ms: int) -> CoreOutput:
        """spec §4 evaluate 1-4 for one coalesced tick: probe, then turn
        the confirmed intrabar fills into `TRIGGER` MARKETs with the
        ENGINE's qty.

        One `[r4]` de-duplication lives here, because the probe answers
        "what would fill if the bar settled now" afresh on every tick and
        so re-reports the same confirmed fill each time: a `(intent, leg)`
        produces at most ONE request per bar.

        A `(intent, leg)` the preceding settlement already asked for as a
        `MARKET_AT_OPEN` (spec §4 settle 6 -- the same fill, seen from the
        other side of the bar boundary) is not TRIGGERed again. This is
        the "next `evaluate()` with the open known" the spec sizes that
        order at, so the FIRST evaluate of the target bar settles the
        advance's fate three ways (m5/F6; see `ActionRequest`'s supersede
        contract):

          * the engine's qty at the open differs from the settle-time
            close proxy by more than the dead band -> a superseding
            `MARKET_AT_OPEN` with the real qty (`reason="open_requote"`),
            which for partition-3 (`AT_FILL` default) sizing B3 cannot
            re-derive afterwards (the handle's accessors describe its LAST
            run);
          * the two agree -> nothing: the advance already carries the
            right size, and re-quoting would cost an order op for nothing;
          * the probe confirms no fill for the key at all -> a `qty=0`
            withdraw, because the engine's admission gate refused the
            order at the open and the ledger will never book it (see
            `_withdraw_unconfirmed`).

        A requote/withdraw is NOT counted against
        `max_fill_actions_per_bar`: it amends an order the settlement
        already counted, gated and placed.

        The per-bar fill-action budget is NOT reset here (`settle()` owns
        `begin_bar()`), so it spans the whole bar's tick stream; a
        `RiskViolation` from it is a genuine anomaly (the probe is
        emitting more distinct fills in one bar than the epoch's budget
        allows) and escalates STOP(FLAT_ONLY), unlike a `check_*` budget
        refusal, which only refuses the one action.
        """
        out = CoreOutput()
        pr = self.probe.evaluate(forming, now_ms, journal=self.j)
        out.probe = pr
        out.book = self.book
        first_evaluate = self._triggered_bar != pr.bar_index
        if first_evaluate:
            self._triggered_bar, self._triggered = pr.bar_index, set()
        position = self.ledger.last.position_size if self.ledger.last is not None else 0.0
        price = forming.c
        band = self.dead_band.qty(price)
        for f in pr.fills:
            key = (f.intent, f.leg)
            if key in self._triggered:
                continue
            if f.intent == "?":
                # N5: classify's M4 ambiguity -- the ledger could not pin
                # this delta fill to one candidate order. A `"?"` order id
                # identity-matches no venue fill, so submitting under it
                # would create an order the next settlement cannot classify.
                self._triggered.add(key)
                self._incident(out, "ambiguous_trigger_intent", leg=f.leg, qty=f.qty, bar_index=pr.bar_index)
                continue
            pending = self.pending_market.get(key)
            requote = pending is not None and pending.target_bar_index == pr.bar_index
            if requote and abs(f.qty - pending.qty) <= band:
                # F6: the advance already carries the right size -- the
                # settle-time close proxy and the engine's own qty at the
                # open agree within the dead band -- so there is nothing to
                # amend and re-quoting would only cost an order op.
                self._triggered.add(key); self.pending_market.pop(key, None)
                continue
            reduce_only = pending.reduce_only if requote else f.leg == "EXIT"
            a = ActionRequest("MARKET_AT_OPEN" if requote else "TRIGGER", f.intent,
                              _side_for_leg(f.is_long, f.leg), f.qty, None if requote else f.price, reduce_only,
                              "MARKET" if requote else "TRIGGER",
                              "open_requote" if requote else "probe fill confirmed on both intrabar paths",
                              pr.bar_index)
            if not self._permitted(out, a, position, price):
                self._triggered.add(key)
                if requote:
                    # NEW-2: the advance was gated and emitted at settle(n)
                    # and is standing at the venue. Journaling "refused"
                    # and emitting nothing would leave an order the record
                    # says was refused to fill anyway. A cancel is always
                    # permitted, and under a STOP it is the right outcome:
                    # a MISSED entry next bar is the reconciler's to budget.
                    self.pending_market.pop(key, None)
                    out.actions.append(ActionRequest("CANCEL_STALE_CYCLE", f.intent, T.Side.BUY, 0.0, None, False,
                                                     "CANCEL", "open_requote refused; the settled advance must not stand",
                                                     pr.bar_index))
                continue
            if not requote:
                # NEW-3: a requote AMENDS an order this bar's settlement
                # already counted, gated and placed -- counting it again
                # would make a two-leg reversal need a budget of 4 where
                # spec §4's invariant is "<= P's fill count".
                try:
                    self.guard.count_fill_action()
                except RiskViolation as e:
                    # F9: mark the key so the exhausted budget is reported ONCE
                    # per key per bar -- the probe re-reports the same confirmed
                    # fill on every remaining tick of the bar, and each one used
                    # to re-count, re-journal and re-`_raise` it.
                    self._triggered.add(key)
                    self._incident(out, "risk_violation", detail=str(e), bar_index=pr.bar_index)
                    self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, str(e))
                    break
            self._triggered.add(key)
            if requote:
                self.pending_market.pop(key, None)
            out.actions.append(a)
        if first_evaluate and not pr.aborted:
            self._withdraw_unconfirmed(out, pr.bar_index)
        for f in pr.retracted:
            self._incident(out, "probe_retract", intent=f.intent, leg=f.leg, bar_index=pr.bar_index)
        return out

    def _withdraw_unconfirmed(self, out: CoreOutput, bar_index: int) -> None:
        """m5: every `MARKET_AT_OPEN` the preceding settlement asked for in
        advance that the FIRST `evaluate()` of its target bar did not
        confirm is superseded by a `qty=0` request (`reason="withdraw"`).

        The advance is priced off bar n's close because that is the only
        price `settle(n)` has; spec §4 settle 6 puts the real order "at the
        next `evaluate()` with the open known". If the engine's admission
        gate then refuses it at the open -- qty 0, a margin refusal, a
        sizing rule that no longer admits the order -- the probe reports no
        fill for that key and the ledger will never book one. Left standing,
        the advance is an order at the venue with no counterpart on the
        ledger: it fills, classifies RETRACTED at the next settlement, and
        STOPs a run that was never wrong. Withdrawing it is the same
        supersede contract the requote uses (`ActionRequest`), so B3 needs
        no second mechanism: the last request for the key wins, and a
        `qty=0` one means "do not place it"."""
        for key, pending in list(self.pending_market.items()):
            if pending.target_bar_index != bar_index:
                continue
            del self.pending_market[key]
            out.actions.append(ActionRequest("MARKET_AT_OPEN", pending.intent, pending.side, 0.0, None,
                                             pending.reduce_only, "MARKET", "withdraw", bar_index))
