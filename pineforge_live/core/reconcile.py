"""The reconciler (spec §5.4): after each settlement when quiescent, turns
one bar's classified fills (`classify.classify_bar`) into at most a
handful of corrections plus a possible STOP escalation. Pure decision --
no I/O, no order submission; the caller (Task 8) journals/executes what
comes back."""
from __future__ import annotations
from dataclasses import dataclass, field
from pineforge_live import types as T
from .classify import ClassifiedFill, FillClass, EmulatedFill
from .riskguard import stronger

#: Every counter name `reconcile()` can `bump()` -- the reconciler's whole
#: observability vocabulary, exported because a G3 `Breaker`'s `name` IS
#: the counter it watches (`live.LiveCore._observe_breakers`) and a breaker
#: naming something not in here can never fire. `BreakerTable.self_test`
#: takes this set and refuses such a config at construction (m2), the same
#: way it already refuses one whose window can never reach `n_min`: the
#: only breaker the suite and the harness ever configured was `"orphan"`,
#: a name nothing bumps, so the whole `g3_breached`/`g3_alert` path was
#: dead. Keep in sync with the `bump(...)` call sites -- there is a test
#: that reads them out of this module's own source and compares.
COUNTER_NAMES: frozenset[str] = frozenset({
    "account_mismatch", "confirmed", "flattened", "gap_carried", "in_flight", "malformed", "mirror_early",
    "missed", "missed_corrected", "path_divergent", "refused_budget", "refused_by_own_stop",
    "refused_skipped_cycle", "residual_carried", "settle_only", "skipped_cycle", "skipped_not_quiescent",
    "skipped_position_mismatch", "synthetic", "topped_up", "trimmed",
})

@dataclass(frozen=True)
class DeadBand:
    """The residual/correction tolerance below which a position mismatch is
    carried rather than corrected: `max(lot_step, min_qty,
    min_notional/price)` -- the largest of the venue's own size floors and
    a notional floor at the given reference `price` (0 when `price <= 0`,
    since a non-positive price makes the notional term meaningless)."""
    lot_step: float; min_qty: float; min_notional: float
    def qty(self, price: float) -> float:
        return max(self.lot_step, self.min_qty, self.min_notional / price if price > 0 else 0.0)

@dataclass(frozen=True)
class ReconcileConfig:
    """Static reconciler policy for one epoch: `max_missed_age_bars`/
    `max_missed_entry_distance_bps` bound a MISSED correction (spec `[r4]`
    -- beyond either, the cycle is `SKIPPED` instead, unless
    `adopt_ledger_position` opts back in); `budget_notional` caps any
    single exposure-increasing correction (MISSED entry or QTY_DIVERGENT
    TOP_UP) in `qty * price` terms; `mirror_early_daily_cap` is the
    per-day count of MIRROR_EARLY fills tolerated before escalating
    `STOP(FLAT_ONLY)`; `max_entry_slip_bps` (m1, spec §5.1: seeded from the
    L2/L3 measurement per leg kind) is the per-ENTRY slip budget
    `classify.classify_bar` reads a matched pair's `ENTRY_SLIP` off --
    the MISSED distance bound is a different quantity and was the wrong
    knob for it."""
    max_missed_age_bars: int; max_missed_entry_distance_bps: float; budget_notional: float
    mirror_early_daily_cap: int; adopt_ledger_position: bool
    max_entry_slip_bps: float = 50.0

@dataclass(frozen=True)
class CorrectionRequest:
    """One correction the caller should submit. `kind` is one of
    `MARKET_CORRECT` (a MISSED fill's counterpart -- exposure-increasing
    for a missed ENTRY, reduce-only for a missed EXIT), `REDUCE_ONLY_TRIM`
    (QTY_DIVERGENT excess), `TOP_UP` (QTY_DIVERGENT shortfall,
    exposure-increasing), or `FLATTEN` (TRIGGER_REVERSED/ENTRY_SLIP,
    reduce real to zero). `intent` is the originating `EmulatedFill`'s own
    id, `None` when the correction has no single originating fill
    (`FLATTEN`).

    `reduce_only` states whether the order REDUCES the venue-side
    position, which is this module's own knowledge and is NOT derivable
    from `kind`: a `MARKET_CORRECT` is exposure-increasing for a missed
    ENTRY and reduce-only for a missed EXIT (the venue still holds what
    the ledger closed), and the EXIT branch is deliberately the one
    `_emit_missed_correction` runs with `increases=False` so it ships
    ungated under `FLAT_ONLY`. A caller re-deriving the flag from `kind`
    alone (Task 8's `_requests` did) labels that correction
    exposure-increasing and has `StopController.permits` refuse under
    `FLAT_ONLY` the one order `FLAT_ONLY` exists to allow."""
    kind: str; side: T.Side; qty: float; reason: str; intent: str | None; reduce_only: bool = False

@dataclass(frozen=True)
class ReconcileInput:
    """One decision's whole input (frozen -- a decision is a pure function
    of this snapshot, never mutated mid-call).

    `ledger_position` is the RECOMPUTE's target (what the ledger's own
    settlement says the position should be). `our_signed_fills` is spec
    §5.4's PRIMARY comparison basis: the cumulative signed qty of fills
    WE placed and attribute to ourselves this cycle (caller-computed) --
    every MISSED/QTY_DIVERGENT correction below is sized against this,
    not `real_position`. `real_position` (the venue account's own
    reported position) is the SECONDARY check: when it disagrees with
    `our_signed_fills` by more than the dead-band, the reconciler can't
    trust its own fill-tracking this call, so it escalates
    `STOP(FLAT_ONLY)` (cause `account_mismatch`) and skips every
    MISSED/QTY_DIVERGENT correction (a FLATTEN, if independently warranted
    by a classified fill, still targets `real_position` directly -- that
    part of the venue truth isn't in question).

    `missed_age_bars` is the caller's own precomputed age estimate for
    whichever MISSED fill ends up correctable; the actual bound used is
    `max(missed_age_bars, bar_index - emulated.bar_index)` (L8) -- the
    larger of what the caller already knew and what this call can derive
    from the fill's own bar, so neither input alone has to be perfectly
    accurate. `missed_distance_bps` is the corresponding price-distance
    estimate (no `bar_index`-style fallback -- the caller is the only
    source for this one). `in_flight` and `bar_index` are otherwise
    carried for the caller's own bookkeeping / a future consumer; this
    module reads `bar_index` only for the age fallback above."""
    bar_index: int; classified: list[ClassifiedFill]; ledger_position: float; real_position: float
    our_signed_fills: float; price: float; quiescent: bool; in_flight: set[str]; stop_level: T.StopLevel
    missed_age_bars: int; missed_distance_bps: float; cfg: ReconcileConfig; dead_band: DeadBand; mirror_early_today: int

@dataclass
class ReconcileDecision:
    """One `reconcile()` call's outcome. `corrections` is the (small, spec
    `[r4]`-bounded) list of orders to submit. `stop` is `None` or
    `(level, disposition, cause)` -- the STOP this decision itself wants
    to raise (on top of whatever `inp.stop_level` already was; the caller
    is expected to `_raise` this before submitting `corrections`, which
    are already gated against it -- see `reconcile()`'s docstring).
    `skipped_cycle` marks a decision that journaled `SKIPPED` instead of
    correcting (beyond-bound MISSED, a MISSED refused by the STOP gate or
    the budget, a MISSED dropped by the L8 account cross-check, a MISSED
    that fails both position gates with a real gap (NEW-8), or a FLATTEN
    that superseded an independent MISSED entry (NEW-7) -- spec:
    "otherwise the cycle is journaled SKIPPED"). `residual_qty`
    accumulates the still-uncorrected QTY_DIVERGENT shortfall/excess
    across every branch that carries rather than corrects, with one
    consistent sign: positive means `our_signed_fills` is SHORT of
    `ledger_position` (an uncorrected top-up), negative means it carries
    an EXCESS we tolerated (an uncorrected trim). Invariant: this is the
    SIGNED correction that was NOT issued THIS decision, not
    `ledger_position - our_signed_fills` outright (a correction may have
    shipped too); on the opposite-side split path (NEW-3) it is the
    un-issued TOP_UP leg only -- the REDUCE_ONLY_TRIM half, when it ships,
    is not reflected here. `counters` is a bag of
    `bump()` tallies for observability/tests -- not spec-normative, just
    named the way the reconciler's own reasoning names its branches."""
    corrections: list[CorrectionRequest] = field(default_factory=list)
    stop: tuple[T.StopLevel, T.StopDisposition, str] | None = None
    skipped_cycle: bool = False; residual_qty: float = 0.0; counters: dict[str, int] = field(default_factory=dict)

def _side_for(delta: float) -> T.Side:
    return T.Side.BUY if delta > 0 else T.Side.SELL

def _escalate(d: ReconcileDecision, level: T.StopLevel, disp: T.StopDisposition, cause: str) -> None:
    """Raises `d.stop` to `(level, disp, cause)` only when that STRICTLY
    outranks what's already recorded this decision, judged by
    `riskguard.stronger()` -- the ONE rank table in the codebase (level
    first, `NONE < FLAT_ONLY < HARD`; disposition as the tiebreak at an
    equal level, `FLATTEN > HOLD > NONE`). This module used to carry its
    own copy of that lattice; `StopController.raise_stop`/`restore()` and
    this function now agree by construction rather than by two tables
    happening to stay in sync.

    On an EQUAL (level, disposition) the FIRST cause recorded wins (N10)
    -- `stronger()` is strict, so an equal pair is not stronger than
    itself: `reconcile()` may call this several times in one decision
    (once per escalating classified fill), and keeping the first is a
    more useful audit trail than whichever call happened to run last."""
    if d.stop is None:
        d.stop = (level, disp, cause); return
    cur_level, cur_disp, _ = d.stop
    if stronger((level, disp), (cur_level, cur_disp)):
        d.stop = (level, disp, cause)

def _emit_missed_correction(inp: ReconcileInput, d: ReconcileDecision, bump, e: EmulatedFill, *, increases: bool,
                            qty_cap: float, flat_only: bool, band: float, corr_side: T.Side) -> float:
    """The single MISSED `CorrectionRequest` for this decision, if any
    (M4: qty clamped to the shortfall/excess, never `e.qty` outright).
    `increases` marks a missed ENTRY (exposure-increasing: gated by
    `flat_only` and `budget_notional`) vs a missed EXIT (reduce-only:
    neither gate applies).

    Returns the SIGNED qty this call actually ISSUED (`+qty` for a BUY,
    `-qty` for a SELL, `0.0` when nothing shipped) -- NEW-1: the
    QTY_DIVERGENT pass that runs next describes the SAME aggregate gap,
    so `reconcile()` nets this out of its basis and only the remainder is
    corrected there."""
    if increases and flat_only:
        d.skipped_cycle = True; bump("refused_by_own_stop"); return 0.0
    age = max(inp.missed_age_bars, inp.bar_index - e.bar_index)
    within = age <= inp.cfg.max_missed_age_bars and inp.missed_distance_bps <= inp.cfg.max_missed_entry_distance_bps
    if not within and not inp.cfg.adopt_ledger_position:
        d.skipped_cycle = True; bump("skipped_cycle"); return 0.0
    qty = min(e.qty, qty_cap)
    if qty <= band:
        # NEW-10: the gate-passing sibling of NEW-8 -- a MISSED fill whose
        # OWN qty is sub-dead-band cannot be corrected, but if the gap it
        # was found under is real (`qty_cap > band`) the cycle is still one
        # we declined to act on and must be journaled SKIPPED rather than
        # counted silently.
        bump("skipped_position_mismatch")
        if qty_cap > band:
            d.skipped_cycle = True; bump("skipped_cycle")
        return 0.0
    if increases and qty * inp.price > inp.cfg.budget_notional:
        d.skipped_cycle = True; bump("refused_budget"); return 0.0
    d.corrections.append(CorrectionRequest("MARKET_CORRECT", corr_side, qty, "MISSED", e.intent, reduce_only=not increases))
    bump("missed_corrected")
    return qty if corr_side == T.Side.BUY else -qty

def _reconcile_missed(inp: ReconcileInput, d: ReconcileDecision, bump, missed_entries: list[ClassifiedFill],
                      missed_exits: list[ClassifiedFill], basis: float, band: float, flat_only: bool) -> float:
    """MISSED correction (spec §5.4 table): at most ONE per decision,
    computed from the POSITION numbers (M4/M5), not once per classified
    MISSED fill -- every fill in `missed_entries`/`missed_exits` explains
    the SAME aggregate gap, so ONE of them is used as the correction's
    representative. Returns the SIGNED qty issued (see
    `_emit_missed_correction`), `0.0` when nothing shipped.

    A missed ENTRY corrects only when `basis` is a STRICT SUBSET of
    `ledger_position` on the ledger's own side (or `basis == 0`) --
    exposure-increasing. NEW-2: the representative is the FIRST entry
    whose own trade direction (`+1 if is_long else -1`) EQUALS the
    ledger's side, not `missed_entries[0]` outright -- the correction
    takes its side from that fill, and on the `basis == 0` path the gate
    has no other side check, so an unfiltered first-fill representative
    made the answer depend on list order and could open a position
    against the ledger (X6: a one-bar `0 → +1 → 0 → −1` round trip whose
    long leg is listed first). When no entry agrees with the ledger's
    side (a flat ledger included, where there is no side to agree with),
    nothing is issued and the fall-through counts
    `skipped_position_mismatch`.

    A missed EXIT (H1 fix: the gate is the mirror image, not the same
    inequality) corrects only when `basis` is STRICTLY LARGER than
    `ledger_position` in magnitude, on that side -- the venue still holds
    what the ledger closed -- and the correction is reduce-only. A
    both-flat MISSED exit (`ledger_position == basis == 0`) matches
    neither gate and corrects nothing. NEW-6: mirrors NEW-2 -- the
    representative is the FIRST exit whose own trade direction equals
    `side` (`sign(ledger)`, or, when the ledger is flat, `sign(basis)`),
    and `corr_side` is derived from that validated `side`, never from the
    unfiltered fill's own `is_long` (an unfiltered representative could
    gate on the ledger's side while shipping an ungated correction on the
    fill's own, opposite, side).

    A MISSED fill that fails both the ENTRY and EXIT gates with a real
    `ledger`/`basis` gap beyond the dead-band (NEW-8) is not a "nothing to
    do": the caller marks `skipped_cycle` so Task 8 journals it rather
    than leaving the gap unrecorded."""
    ledger = inp.ledger_position
    if missed_entries:
        side = 1 if ledger > 0 else (-1 if ledger < 0 else 0)
        e = next((c.emulated for c in missed_entries if (1 if c.emulated.is_long else -1) == side), None)
        if e is not None and (basis == 0.0 or (abs(basis) < abs(ledger) and (basis > 0) == (side > 0))):
            corr_side = T.Side.BUY if e.is_long else T.Side.SELL
            return _emit_missed_correction(inp, d, bump, e, increases=True, qty_cap=abs(ledger) - abs(basis),
                                           flat_only=flat_only, band=band, corr_side=corr_side)
    if missed_exits:
        side = 1 if ledger > 0 else (-1 if ledger < 0 else (1 if basis > 0 else -1))
        e = next((c.emulated for c in missed_exits if (1 if c.emulated.is_long else -1) == side), None)
        if e is not None and basis != 0.0 and (basis > 0) == (side > 0) and abs(basis) > abs(ledger):
            corr_side = T.Side.SELL if side > 0 else T.Side.BUY   # reduce-only: opposite of the validated side, never e.is_long
            return _emit_missed_correction(inp, d, bump, e, increases=False, qty_cap=abs(basis) - abs(ledger),
                                           flat_only=flat_only, band=band, corr_side=corr_side)
    if missed_entries or missed_exits:
        bump("skipped_position_mismatch")
        if abs(ledger - basis) > band:
            d.skipped_cycle = True; bump("skipped_cycle")
    return 0.0

def _emit_top_up(inp: ReconcileInput, d: ReconcileDecision, bump, e: EmulatedFill, *, signed: float,
                 band: float, flat_only: bool, skipped: bool = False) -> None:
    """The exposure-increasing half of a QTY_DIVERGENT correction: move
    the position by `signed` (`+` = BUY, `-` = SELL). Refused by the STOP
    level (`flat_only`), by a cycle this same decision already marked
    SKIPPED (`skipped`, NEW-9), and by the budget in `qty * price` terms
    exactly like a MISSED entry; every refusal (and a sub-dead-band
    `signed`) carries `signed` into `residual_qty` under that field's own
    sign convention (the signed correction that was NOT issued).

    The two refusals are counted apart (re-review nit b): a SKIPPED-cycle
    refusal under NO stop used to bump `refused_by_own_stop`, so a breaker
    watching that name would count cycles nothing had STOPped. `flat_only`
    wins the attribution when both hold -- the stronger reason."""
    qty = abs(signed)
    if qty <= band:
        d.residual_qty += signed; bump("residual_carried"); return
    if flat_only or skipped:
        d.residual_qty += signed; bump("refused_by_own_stop" if flat_only else "refused_skipped_cycle"); return
    if qty * inp.price > inp.cfg.budget_notional:
        d.residual_qty += signed; bump("refused_budget"); return
    d.corrections.append(CorrectionRequest("TOP_UP", _side_for(signed), qty, "QTY_DIVERGENT", e.intent, reduce_only=False))
    bump("topped_up")

def _reconcile_qty_divergent(inp: ReconcileInput, d: ReconcileDecision, bump, qty_divergent: list[ClassifiedFill],
                             basis: float, band: float, flat_only: bool, skipped: bool = False) -> None:
    """QTY_DIVERGENT correction (spec §5.4/§4.4): one trim and/or one
    budgeted top-up for the TOTAL position delta (M5), regardless of how
    many QTY_DIVERGENT fills were classified this decision -- every one of
    them describes the same aggregate `basis` vs `ledger_position` gap, so
    only the first is used as the correction's representative (its
    `intent`). NEW-1: `basis` here is the caller's basis ALREADY NETTED of
    whatever a MISSED correction issued earlier in the same decision, so
    this pass only ever corrects the REMAINDER.

    H2 fix: `side` -- the sign the excess/shortfall is measured against --
    is `sign(ledger_position)`, or, when the ledger is FLAT, `sign(basis)`
    itself (never an emulated-fill-implied side): this is what makes "flat
    ledger, any residual real position" ALWAYS read as an excess (a trim
    back toward the ledger's own flat target), never a top-up, no matter
    which side that residual happens to be on. `excess = (basis -
    ledger_position) * side`: `> band` is an excess (REDUCE_ONLY_TRIM,
    never gated -- reduce-only), `< -band` is a shortfall (TOP_UP,
    exposure-increasing: gated by `flat_only` and budget-checked exactly
    like a MISSED entry), within the band is carried as residual.

    NEW-3: when `ledger_position` and `basis` are both nonzero and on
    OPPOSITE sides, the move is TWO orders, not one full-swing order --
    an ungated `REDUCE_ONLY_TRIM |basis|` back to flat first, then a
    gated/budgeted `TOP_UP |ledger_position|` to re-open the other way.
    Sizing the whole swing as one exposure-increasing order lost the
    reducing half under FLAT_ONLY (the ONE order FLAT_ONLY exists to
    permit) and mislabelled it as exposure-increasing under NONE."""
    if not qty_divergent:
        return
    e = qty_divergent[0].emulated
    ledger = inp.ledger_position
    side = 1 if ledger > 0 else (-1 if ledger < 0 else (1 if basis >= 0 else -1))
    delta = basis - ledger
    if abs(delta) <= band:
        d.residual_qty += -delta; bump("residual_carried"); return
    if ledger != 0.0 and basis != 0.0 and (basis > 0) != (ledger > 0):
        # NEW-3: reduce to flat first (reduce-only, ungated), then re-open.
        if abs(basis) > band:
            d.corrections.append(CorrectionRequest("REDUCE_ONLY_TRIM", _side_for(-basis), abs(basis), "QTY_DIVERGENT", e.intent, reduce_only=True))
            bump("trimmed")
        else:
            d.residual_qty += -basis; bump("residual_carried")
        _emit_top_up(inp, d, bump, e, signed=ledger, band=band, flat_only=flat_only, skipped=skipped)
        return
    if delta * side > 0:
        d.corrections.append(CorrectionRequest("REDUCE_ONLY_TRIM", _side_for(-delta), abs(delta), "QTY_DIVERGENT", e.intent, reduce_only=True))
        bump("trimmed"); return
    _emit_top_up(inp, d, bump, e, signed=-delta, band=band, flat_only=flat_only, skipped=skipped)

def reconcile(inp: ReconcileInput) -> ReconcileDecision:
    """spec §5.4: turn one bar's classified fills into corrections plus a
    possible STOP escalation.

    Two passes (M3 fix): the first classifies every fill and raises any
    STOP this decision's OWN findings call for (`d.stop`); ONLY THEN is
    the effective level -- `max(inp.stop_level, d.stop[0])` -- derived and
    used to gate every correction this SAME call is about to build. The
    prior version gated against `inp.stop_level` alone, so a decision that
    escalated FLAT_ONLY/HARD from e.g. a RETRACTED or TRIGGER_REVERSED
    fill could still ship an exposure-increasing correction (a MISSED
    entry, a QTY_DIVERGENT TOP_UP) from another fill in the SAME call --
    the caller (Task 8) evaluates `permits` before `_raise`, so anything
    still here at return time ships as-is.

    Corrections are once per DECISION, NETTED (M4/M5 + NEW-1) -- not once
    per classified fill and not once per class. Every builder describes
    the SAME aggregate position gap, so:
      * a FLATTEN ends the decision (it already takes the venue to zero,
        so a MISSED/QTY_DIVERGENT correction sized against the
        pre-flatten gap would over-shoot straight through zero -- and
        both can be reduce-only, so the `flat_only` gate never catches
        them);
      * otherwise `_reconcile_missed` runs first and returns the SIGNED
        qty it issued, and `_reconcile_qty_divergent` is handed
        `basis + issued` so it corrects only the remainder.
    TRIGGER_REVERSED/ENTRY_SLIP emit at most one FLATTEN per decision
    (L6), sized to `real_position` (the venue truth), reduce-only and
    therefore never gated by `flat_only`.

    A MISSED fill whose ledger and `basis` sit on OPPOSITE sides (both
    nonzero) can never pass either MISSED gate; rather than counting that
    silently, the decision escalates `STOP(FLAT_ONLY, HOLD)` with cause
    `unreconcilable_sides` (X13).

    L8: `our_signed_fills` (`basis` below) is the PRIMARY comparison basis
    for MISSED/QTY_DIVERGENT, not `real_position` -- see `ReconcileInput`'s
    docstring. `real_position` is the secondary account check: beyond the
    dead-band from `basis`, this escalates `STOP(FLAT_ONLY)` (cause
    `account_mismatch`) and skips every MISSED/QTY_DIVERGENT correction
    for the call (a FLATTEN, being sized to `real_position` directly, is
    unaffected)."""
    d = ReconcileDecision()
    def bump(k): d.counters[k] = d.counters.get(k, 0) + 1
    if not inp.quiescent:
        bump("skipped_not_quiescent"); return d

    band = inp.dead_band.qty(inp.price)
    basis = inp.our_signed_fills

    # ---- pass 1: classify every fill, escalate this decision's OWN STOP,
    # and collect what a MISSED/QTY_DIVERGENT/FLATTEN correction needs --
    # corrections themselves are built only after the gate below.
    missed_entries: list[ClassifiedFill] = []
    missed_exits: list[ClassifiedFill] = []
    qty_divergent: list[ClassifiedFill] = []
    need_flatten = False
    flatten_cause: str | None = None
    for c in inp.classified:
        e, cls = c.emulated, c.cls
        if cls == FillClass.MISSED:
            if e is None:
                bump("malformed"); continue
            # m2: the spec's orphan+missed breaker numerator -- one per
            # MISSED FILL, unlike `missed_corrected`/`skipped_*`, which
            # count what the DECISION did about all of them together.
            bump("missed")
            (missed_entries if e.leg == "ENTRY" else missed_exits).append(c)
        elif cls == FillClass.QTY_DIVERGENT:
            if e is None:
                bump("malformed"); continue
            qty_divergent.append(c)
        elif cls == FillClass.PATH_DIVERGENT:
            bump("path_divergent")
        elif cls == FillClass.MIRROR_EARLY:
            bump("mirror_early")
            if inp.mirror_early_today + d.counters["mirror_early"] > inp.cfg.mirror_early_daily_cap:
                _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "MIRROR_EARLY daily cap")
        elif cls in (FillClass.TRIGGER_REVERSED, FillClass.ENTRY_SLIP):
            need_flatten = True
            flatten_cause = flatten_cause or cls.value
            _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, cls.value)
        elif cls == FillClass.RETRACTED:
            _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "RETRACTED: real ≠ ledger beyond dead-band")
        elif cls == FillClass.UNATTRIBUTED_VENUE:
            _escalate(d, T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")
        elif cls in (FillClass.CONFIRMED, FillClass.IN_FLIGHT, FillClass.SYNTHETIC, FillClass.SETTLE_ONLY):
            # M5: `SETTLE_ONLY` is tallied like `IN_FLIGHT` and corrected
            # like neither -- spec §4 settle 6 places the MARKET for it
            # NOW, from the caller, and the venue reports it at n+1. It is
            # not a fill the venue missed, so no MISSED bound, no budget
            # and no `[r4]` age applies to it.
            bump(cls.value.lower())

    # ---- X13: the ledger and our OWN fills point OPPOSITE ways (both
    # nonzero), so every MISSED gate below necessarily fails and no
    # correction is derivable from a MISSED fill at all. That disagreement
    # about which way the position even points is a finding in its own
    # right, not a quiet `skipped_position_mismatch`: escalate
    # `FLAT_ONLY / HOLD`. Raised HERE, before the gate is derived, so it
    # governs this same decision's own corrections (M3) like any other
    # escalation, and through `_escalate` so it stays monotonic.
    if (missed_entries or missed_exits) and basis != 0.0 and inp.ledger_position != 0.0 \
            and (basis > 0) != (inp.ledger_position > 0):
        _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.HOLD, "unreconcilable_sides")

    # ---- L8 secondary check: escalate + skip every position-level
    # correction when our own fill-tracking disagrees with the account.
    account_mismatch = abs(inp.real_position - basis) > band
    if account_mismatch:
        _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "account_mismatch")
        bump("account_mismatch")

    # ---- gate: this decision's OWN escalation (if any) applies to its
    # OWN corrections (M3) -- derive the FINAL level before building any.
    level = inp.stop_level
    # Level-only comparison (the gate below reads nothing but the level):
    # pairing both sides with the SAME disposition makes `stronger()`
    # fall through to its level rank, so this stays the shared table too.
    if d.stop is not None and stronger((d.stop[0], T.StopDisposition.NONE), (level, T.StopDisposition.NONE)):
        level = d.stop[0]
    flat_only = level in (T.StopLevel.FLAT_ONLY, T.StopLevel.HARD)

    # ---- L6: at most one FLATTEN per decision, reduce-only to the real
    # (venue-truth) position -- always allowed. NEW-1: a FLATTEN takes the
    # venue to ZERO, so every other builder's sizing (which is against the
    # PRE-flatten gap) is at best redundant and at worst an over-shoot that
    # opens a position the other way -- both legs reduce-only, so the M3
    # gate never catches it (X3/X4). Emitting one ends the decision.
    if need_flatten and inp.real_position != 0.0:
        d.corrections.append(CorrectionRequest("FLATTEN", _side_for(-inp.real_position), abs(inp.real_position), flatten_cause, None, reduce_only=True))
        bump("flattened")
        if missed_entries:
            # NEW-7: the FLATTEN already covers the venue position, but it
            # must not also drop the L7/NEW-5 SKIPPED marking -- a MISSED
            # entry found this same decision would have been refused by
            # this decision's own FLAT_ONLY escalation anyway (a MISSED
            # exit is covered by the flatten itself).
            d.skipped_cycle = True; bump("refused_by_own_stop")
        return d

    if not account_mismatch:
        # NEW-1: corrections are once per DECISION, NETTED -- not once per
        # class. The MISSED pass returns the SIGNED qty it issued, and the
        # QTY_DIVERGENT pass is handed `basis + issued` so it corrects only
        # the remainder of the same aggregate gap (X1/X2).
        issued = _reconcile_missed(inp, d, bump, missed_entries, missed_exits, basis, band, flat_only)
        # NEW-9: a decision already marked SKIPPED (a MISSED correction
        # refused by [r4]'s age/distance bound, or NEW-8's both-gates-
        # failed fall-through) issues no exposure-increasing correction
        # from the QTY_DIVERGENT pass either -- trims still ship
        # (reduce-only, ungated); budget/own-stop refusals are unchanged
        # since the top-up would be refused for the same reason. The two
        # refusals are counted apart (nit b): `flat_only` is the STOP
        # level, `skipped` this decision's own SKIPPED marking.
        _reconcile_qty_divergent(inp, d, bump, qty_divergent, basis + issued, band, flat_only, skipped=d.skipped_cycle)
    elif missed_entries:
        # NEW-5: a MISSED entry dropped by the account cross-check is a
        # cycle we declined to act on -- say so, so Task 8 journals the
        # `cycle_skipped` row rather than leaving only the STOP as evidence.
        d.skipped_cycle = True; bump("skipped_cycle")

    # ---- M4(c): a quiescent decision that ends with the ledger and our
    # own fills apart by more than the dead-band and issued NOTHING is a
    # carried gap. Spec §5.4 puts the ledger-vs-our-fills comparison at the
    # decision level, but every branch above only reaches it THROUGH a
    # MISSED/QTY_DIVERGENT fill, so a bar whose fills were all explained
    # (or whose only finding was a deliberate hold-flat) left a real
    # divergence with no counter at all. A counter, not a STOP:
    # MIRROR_EARLY's "hold flat, retain the intent" is exactly this shape
    # and is legal (spec §5.4), and the G3 breaker is what decides whether
    # the RATE of it is not.
    if not d.corrections and abs(inp.ledger_position - basis) > band:
        bump("gap_carried")

    return d
