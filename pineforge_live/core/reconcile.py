"""The reconciler (spec §5.4): after each settlement when quiescent, turns
one bar's classified fills (`classify.classify_bar`) into at most a
handful of corrections plus a possible STOP escalation. Pure decision --
no I/O, no order submission; the caller (Task 8) journals/executes what
comes back."""
from __future__ import annotations
from dataclasses import dataclass, field
from pineforge_live import types as T
from .classify import ClassifiedFill, FillClass, EmulatedFill

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
    `STOP(FLAT_ONLY)`."""
    max_missed_age_bars: int; max_missed_entry_distance_bps: float; budget_notional: float
    mirror_early_daily_cap: int; adopt_ledger_position: bool

@dataclass(frozen=True)
class CorrectionRequest:
    """One correction the caller should submit. `kind` is one of
    `MARKET_CORRECT` (a MISSED fill's counterpart -- exposure-increasing
    for a missed ENTRY, reduce-only for a missed EXIT), `REDUCE_ONLY_TRIM`
    (QTY_DIVERGENT excess), `TOP_UP` (QTY_DIVERGENT shortfall,
    exposure-increasing), or `FLATTEN` (TRIGGER_REVERSED/ENTRY_SLIP,
    reduce real to zero). `intent` is the originating `EmulatedFill`'s own
    id, `None` when the correction has no single originating fill
    (`FLATTEN`)."""
    kind: str; side: T.Side; qty: float; reason: str; intent: str | None

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
    correcting (beyond-bound MISSED, or a MISSED refused by the STOP gate
    -- spec: "otherwise the cycle is journaled SKIPPED"). `residual_qty`
    accumulates the still-uncorrected QTY_DIVERGENT shortfall/excess
    across every branch that carries rather than corrects, with one
    consistent sign: positive means `our_signed_fills` is SHORT of
    `ledger_position` (an uncorrected top-up), negative means it carries
    an EXCESS we tolerated (an uncorrected trim). `counters` is a bag of
    `bump()` tallies for observability/tests -- not spec-normative, just
    named the way the reconciler's own reasoning names its branches."""
    corrections: list[CorrectionRequest] = field(default_factory=list)
    stop: tuple[T.StopLevel, T.StopDisposition, str] | None = None
    skipped_cycle: bool = False; residual_qty: float = 0.0; counters: dict[str, int] = field(default_factory=dict)

_STOP_RANK = {T.StopLevel.NONE: 0, T.StopLevel.FLAT_ONLY: 1, T.StopLevel.HARD: 2}
_DISP_RANK = {T.StopDisposition.NONE: 0, T.StopDisposition.HOLD: 1, T.StopDisposition.FLATTEN: 2}

def _side_for(delta: float) -> T.Side:
    return T.Side.BUY if delta > 0 else T.Side.SELL

def _escalate(d: ReconcileDecision, level: T.StopLevel, disp: T.StopDisposition, cause: str) -> None:
    """Raises `d.stop` to `(level, disp, cause)` only when that is
    STRICTLY more severe than what's already recorded this decision --
    level first, disposition as the tiebreak at an equal level (spec
    §5.5's own lattice: `FLATTEN` outranks `HOLD` at the same STOP level).
    On an EQUAL level+disposition the FIRST cause recorded wins (N10):
    `reconcile()` may call this several times in one decision (once per
    escalating classified fill), and keeping the first is a more useful
    audit trail than whichever call happened to run last."""
    if d.stop is None:
        d.stop = (level, disp, cause); return
    cur_level, cur_disp, _ = d.stop
    if _STOP_RANK[level] > _STOP_RANK[cur_level] or (level == cur_level and _DISP_RANK[disp] > _DISP_RANK[cur_disp]):
        d.stop = (level, disp, cause)

def _missed_side(ledger_position: float, e: EmulatedFill) -> int:
    """+1/-1: the side a MISSED correction's gate compares `basis`
    (`our_signed_fills`) against -- `sign(ledger_position)`, or, when the
    ledger is currently flat, the side `e`'s own trade direction implies.
    `EmulatedFill.is_long` encodes the TRADE/POSITION direction for BOTH
    legs (see its docstring), so an ENTRY's own `is_long` and an EXIT's
    "opposite of the closing order's own side" land on the identical
    sign -- one formula covers both."""
    if ledger_position != 0.0:
        return 1 if ledger_position > 0 else -1
    return 1 if e.is_long else -1

def _emit_missed_correction(inp: ReconcileInput, d: ReconcileDecision, bump, e: EmulatedFill, *, increases: bool,
                            qty_cap: float, flat_only: bool, band: float, corr_side: T.Side) -> None:
    """The single MISSED `CorrectionRequest` for this decision, if any
    (M4: qty clamped to the shortfall/excess, never `e.qty` outright).
    `increases` marks a missed ENTRY (exposure-increasing: gated by
    `flat_only` and `budget_notional`) vs a missed EXIT (reduce-only:
    neither gate applies)."""
    if increases and flat_only:
        d.skipped_cycle = True; bump("refused_by_own_stop"); return
    age = max(inp.missed_age_bars, inp.bar_index - e.bar_index)
    within = age <= inp.cfg.max_missed_age_bars and inp.missed_distance_bps <= inp.cfg.max_missed_entry_distance_bps
    if not within and not inp.cfg.adopt_ledger_position:
        d.skipped_cycle = True; bump("skipped_cycle"); return
    qty = min(e.qty, qty_cap)
    if qty <= band:
        bump("skipped_position_mismatch"); return
    if increases and qty * inp.price > inp.cfg.budget_notional:
        d.skipped_cycle = True; bump("refused_budget"); return
    d.corrections.append(CorrectionRequest("MARKET_CORRECT", corr_side, qty, "MISSED", e.intent))
    bump("missed_corrected")

def _reconcile_missed(inp: ReconcileInput, d: ReconcileDecision, bump, missed_entries: list[ClassifiedFill],
                      missed_exits: list[ClassifiedFill], basis: float, band: float, flat_only: bool) -> None:
    """MISSED correction (spec §5.4 table): at most ONE per decision,
    computed from the POSITION numbers (M4/M5), not once per classified
    MISSED fill -- every fill in `missed_entries`/`missed_exits` explains
    the SAME aggregate gap, so only the first of whichever leg's gate
    passes is used as the correction's representative.

    A missed ENTRY corrects only when `basis` is a STRICT SUBSET of
    `ledger_position` on the ledger's own side (or `basis == 0`) --
    exposure-increasing. A missed EXIT (H1 fix: the gate is the mirror
    image, not the same inequality) corrects only when `basis` is
    STRICTLY LARGER than `ledger_position` in magnitude, on that side --
    the venue still holds what the ledger closed -- and the correction is
    reduce-only. A both-flat MISSED exit (`ledger_position == basis ==
    0`) matches neither gate and corrects nothing."""
    if missed_entries:
        e = missed_entries[0].emulated
        side = _missed_side(inp.ledger_position, e)
        if basis == 0.0 or (abs(basis) < abs(inp.ledger_position) and (basis > 0) == (side > 0)):
            corr_side = T.Side.BUY if e.is_long else T.Side.SELL
            _emit_missed_correction(inp, d, bump, e, increases=True, qty_cap=abs(inp.ledger_position) - abs(basis),
                                    flat_only=flat_only, band=band, corr_side=corr_side)
            return
    if missed_exits:
        e = missed_exits[0].emulated
        side = _missed_side(inp.ledger_position, e)
        if basis != 0.0 and (basis > 0) == (side > 0) and abs(basis) > abs(inp.ledger_position):
            corr_side = T.Side.SELL if e.is_long else T.Side.BUY   # reduce-only: opposite of the side still held
            _emit_missed_correction(inp, d, bump, e, increases=False, qty_cap=abs(basis) - abs(inp.ledger_position),
                                    flat_only=flat_only, band=band, corr_side=corr_side)
            return
    if missed_entries or missed_exits:
        bump("skipped_position_mismatch")

def _reconcile_qty_divergent(inp: ReconcileInput, d: ReconcileDecision, bump, qty_divergent: list[ClassifiedFill],
                             basis: float, band: float, flat_only: bool) -> None:
    """QTY_DIVERGENT correction (spec §5.4/§4.4): one trim or one budgeted
    top-up for the TOTAL position delta (M5), regardless of how many
    QTY_DIVERGENT fills were classified this decision -- every one of them
    describes the same aggregate `basis` vs `ledger_position` gap, so only
    the first is used as the correction's representative (its `intent`).

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

    (Known simplification, not exercised by the corpus fixtures or a
    required pin: when `ledger_position` and `basis` are both nonzero and
    on OPPOSITE sides, this still emits one order sized to the full swing
    rather than a flatten-to-zero followed by a separate top-up.)"""
    if not qty_divergent:
        return
    e = qty_divergent[0].emulated
    ledger = inp.ledger_position
    side = 1 if ledger > 0 else (-1 if ledger < 0 else (1 if basis >= 0 else -1))
    delta = basis - ledger
    if abs(delta) <= band:
        d.residual_qty += -delta; bump("residual_carried"); return
    excess = delta * side
    if excess > 0:
        d.corrections.append(CorrectionRequest("REDUCE_ONLY_TRIM", _side_for(-delta), abs(delta), "QTY_DIVERGENT", e.intent))
        bump("trimmed"); return
    if flat_only:
        d.residual_qty += -delta; bump("refused_by_own_stop"); return
    qty = abs(delta)
    if qty * inp.price > inp.cfg.budget_notional:
        d.residual_qty += -delta; bump("refused_budget"); return
    d.corrections.append(CorrectionRequest("TOP_UP", _side_for(-delta), qty, "QTY_DIVERGENT", e.intent))
    bump("topped_up")

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

    MISSED and QTY_DIVERGENT corrections are each computed ONCE per
    decision from the aggregate position numbers (M4/M5), not once per
    classified fill -- see `_reconcile_missed`/`_reconcile_qty_divergent`.
    TRIGGER_REVERSED/ENTRY_SLIP emit at most one FLATTEN per decision
    (L6), sized to `real_position` (the venue truth), reduce-only and
    therefore never gated by `flat_only`.

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
        elif cls in (FillClass.CONFIRMED, FillClass.IN_FLIGHT, FillClass.SYNTHETIC):
            bump(cls.value.lower())

    # ---- L8 secondary check: escalate + skip every position-level
    # correction when our own fill-tracking disagrees with the account.
    account_mismatch = abs(inp.real_position - basis) > band
    if account_mismatch:
        _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "account_mismatch")
        bump("account_mismatch")

    # ---- gate: this decision's OWN escalation (if any) applies to its
    # OWN corrections (M3) -- derive the FINAL level before building any.
    level = inp.stop_level
    if d.stop is not None and _STOP_RANK[d.stop[0]] > _STOP_RANK[level]:
        level = d.stop[0]
    flat_only = level in (T.StopLevel.FLAT_ONLY, T.StopLevel.HARD)

    # ---- L6: at most one FLATTEN per decision, reduce-only to the real
    # (venue-truth) position -- always allowed.
    if need_flatten and inp.real_position != 0.0:
        d.corrections.append(CorrectionRequest("FLATTEN", _side_for(-inp.real_position), abs(inp.real_position), flatten_cause, None))

    if not account_mismatch:
        _reconcile_missed(inp, d, bump, missed_entries, missed_exits, basis, band, flat_only)
        _reconcile_qty_divergent(inp, d, bump, qty_divergent, basis, band, flat_only)

    return d
