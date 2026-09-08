"""Fill classification (spec §4 settle 4): ledger fills vs venue fills of one bar."""
from __future__ import annotations
import enum
from dataclasses import dataclass
from pineforge_live import types as T
from .book import IntentState

# Member names of `strategy_closed_trade_close_cause`'s doxygen table in
# ~/code/pineforge-engine-wt/main/include/pineforge/pineforge.h, codes 0..6
# in the order the doc lists them (there is no C enum for this -- the
# function just returns `int` -- so the names are pinned by a
# header-reading test, same idea as ids.ORDER_TYPE_NAMES for OrderType).
CLOSE_CAUSE_NAMES: tuple[str, ...] = ("UNKNOWN", "SCRIPT", "BRACKET", "MARGIN_CALL", "INTRADAY_LOSS_CAP", "INTRADAY_FILL_CAP", "RANGE_END")
CLOSE_CAUSE_MARGIN_CALL: int = CLOSE_CAUSE_NAMES.index("MARGIN_CALL")
# MARGIN_CALL + every INTRADAY_* code: closes the engine itself forced,
# not a script/bracket decision -- these never look for a venue counterpart.
SYNTHETIC_CAUSES: frozenset[int] = frozenset(i for i, n in enumerate(CLOSE_CAUSE_NAMES) if n == "MARGIN_CALL" or n.startswith("INTRADAY_"))

# Tolerance for "do these fixed Intent.qtys actually sum to the delta" (M4)
# -- float noise from the engine's own arithmetic, not a caller-configured
# dead-band (classify.py has no caller context at emulated_from_settle time).
_EPS = 1e-9

class FillClass(enum.Enum):
    CONFIRMED = "CONFIRMED"; IN_FLIGHT = "IN_FLIGHT"; MISSED = "MISSED"; SYNTHETIC = "SYNTHETIC"; QTY_DIVERGENT = "QTY_DIVERGENT"
    PATH_DIVERGENT = "PATH_DIVERGENT"; MIRROR_EARLY = "MIRROR_EARLY"; TRIGGER_REVERSED = "TRIGGER_REVERSED"; ENTRY_SLIP = "ENTRY_SLIP"
    RETRACTED = "RETRACTED"; UNATTRIBUTED_VENUE = "UNATTRIBUTED_VENUE"

@dataclass(frozen=True)
class EmulatedFill:
    """One ledger-side (emulated) fill for one bar (spec §4 PLAN DEFECT
    ruling): either a closed trade's entry/exit leg (`emulated_from_settle`
    part (a)) or a position-delta fill synthesized by `Ledger._result`
    for a still-open entry/add or a reduce-but-not-close exit
    (`emulated_from_settle` part (b)).

    `is_long` is the TRADE/POSITION DIRECTION this fill moves toward --
    for a closed-trade leg, `TradeRow.is_long` (the trade's own side); for
    a delta fill, the ledger's `entry_fills` dict's own `is_long`
    (`prev_position_size > 0` for an EXIT-leg delta). This is NOT the
    resolved `Intent`'s order side (`book.Intent.is_long`, the ORDER's
    side: an EXIT order closing a long position reads `is_long=False`,
    since it sells to close). The two conventions AGREE for an ENTRY fill
    (the order that opens a long position is itself long) and DISAGREE for
    an EXIT fill (an order that closes a long position is a SELL, i.e.
    `is_long=False`) -- `emulated_from_settle`'s candidate match flips the
    comparison for EXIT deltas accordingly (see `_delta_candidates`).

    `ambiguous=True` (spec §4 review finding 4 / M4) marks a delta fill
    whose intent attribution could not be pinned to one candidate order:
    more than one same-side pending order left the book this bar (a
    pyramided position) and their fixed `Intent.qty`s don't sum to the
    delta, so which specific order(s) filled, and for how much each,
    genuinely cannot be told apart. `intent` is `"?"` in that case.
    `classify_bar` never emits `QTY_DIVERGENT` from an ambiguous fill (see
    its docstring) -- an ambiguous fill's `"?"` intent cannot identity-match
    any real venue fill, so it always reads `MISSED`, and the venue side's
    own fill(s) reconcile independently against the ledger/real position."""
    intent: str; leg: str; is_long: bool; qty: float; price: float; bar_index: int; close_cause: int = 0
    ambiguous: bool = False

@dataclass(frozen=True)
class VenueFill:
    """One venue-reported fill for one bar. `side` is the VENUE order's
    own side (BUY/SELL) -- see `EmulatedFill`'s docstring for the
    trade-direction-vs-order-side distinction this is matched against.
    `intent` is the raw Pine order id the fill's client id/journal row
    resolves to (`None` when it can't be attributed) -- NOT an
    `IntentKey`/`IntentKey.s`. `target_bar_index` is the bar the action
    that produced this fill's order was journaled against (an
    `OrderAction`'s own bar index); `classify_bar`'s match key requires
    this equal the candidate `EmulatedFill`'s own `bar_index` (see
    `classify_bar`'s docstring) -- a venue fill journaled against a
    different bar than the one being classified is never identity-matched,
    even if the order id/leg/side otherwise agree. `executed_trigger`
    marks a fill of a TRIGGER action -- an intrabar MARKET the probe
    itself emitted (entry or exit; spec evaluate 2: "path-variant fills
    that close the same cycle are emitted ... and are PATH_DIVERGENT") --
    as opposed to a plain resting-order (mirror) fill. `price`/`client_id`
    are carried for the journal, not read by this module."""
    intent: str | None; leg: str | None; side: T.Side; qty: float; price: float; target_bar_index: int; cause: T.FillCause
    client_id: str | None; executed_trigger: bool = False

@dataclass(frozen=True)
class ClassifiedFill:
    cls: FillClass; emulated: EmulatedFill | None; venue: VenueFill | None; qty_delta: float; note: str

def _side_for_leg(is_long: bool, leg: str) -> T.Side:
    """The venue order `Side` an `EmulatedFill`'s (`is_long`, `leg`) pair
    implies (M2, leg-aware): opening (`ENTRY`) a long, or closing (`EXIT`)
    a short, is a BUY; opening a short, or closing a long, is a SELL."""
    return T.Side.BUY if ((leg == "ENTRY") == is_long) else T.Side.SELL

def classify_bar(emulated, venue, *, in_flight_intents, mirrored_intents, dead_band_qty, ledger_position, real_position,
                 entry_slip_bps, max_entry_slip_bps) -> list[ClassifiedFill]:
    """Classifies one bar's `emulated` (ledger-side) fills against its
    `venue` (venue-reported) fills (spec §4 settle 4/§5.3).

    Match key (M2, spec: "matched by intent + side + script bar first, qty
    last"): `(intent, leg, side)` with the leg-aware side mapping
    (`_side_for_leg`) PLUS `venue_fill.target_bar_index == emulated_fill.bar_index`
    -- a same-id fill on the WRONG side, or journaled against a different
    bar, is never absorbed as CONFIRMED; it is instead a genuine
    MISSED+RETRACTED/PATH_DIVERGENT pair (a submission/journal bug, not a
    match). `qty` is data reported on the `ClassifiedFill`, never part of
    the key -- see `QTY_DIVERGENT`.

    Unmatched-venue branch order (M3): venue-initiated (`UNATTRIBUTED_VENUE`)
    first, then a different-leg cycle close (an EXIT-leg venue fill that is
    either one of OUR resting mirror orders (`mirrored_intents`) or one of
    OUR emitted intrabar TRIGGER exits (`executed_trigger`) -- either way,
    an order WE are responsible for) resolves to `PATH_DIVERGENT` when the
    ledger's own emulated exit(s) this bar used a DIFFERENT intent, else
    `MIRROR_EARLY`; then an ENTRY-leg TRIGGER whose execution disagrees
    with the ledger's settled position resolves to `TRIGGER_REVERSED`;
    anything else unmatched falls through to `RETRACTED`/`CONFIRMED` on
    whether `real_position`/`ledger_position` still agree within
    `dead_band_qty`. `TRIGGER_REVERSED` is deliberately ENTRY-leg only (an
    EXIT-leg trigger is handled by the different-leg-close branch above,
    which distinguishes "the venue closed the SAME cycle we did, just via
    a different leg" (`PATH_DIVERGENT`) from "our own emitted exit fired
    with nothing on the ledger side to explain it" (`MIRROR_EARLY`, not
    `TRIGGER_REVERSED` -- an aggressive but unconfirmed-by-settlement exit
    is treated as early, not reversed, since it never contradicts an
    actual ledger position the way an ENTRY trigger against a flat/opposite
    ledger does).

    A `PATH_DIVERGENT` venue fill suppresses at most ONE otherwise-unmatched
    deferred mirrored EXIT (M1): the two are the same real-world
    bracket-close event (the venue closed the cycle via a different leg
    than the one the ledger's own recompute used), so reporting the
    ledger's leg again as `MISSED` would just restate the divergence. The
    pairing is 1:1 and scoped by close direction + qty (within
    `dead_band_qty`), NOT call-global -- an unrelated cycle's genuinely
    missed close (a same-bar round trip's own exit, spec's `new_opened`)
    survives even when an earlier PATH_DIVERGENT already fired this call.
    """
    out: list[ClassifiedFill] = []
    venue_by_key: dict[tuple, list[VenueFill]] = {}
    for v in venue:
        venue_by_key.setdefault((v.intent, v.leg, v.side), []).append(v)
    matched: set[int] = set()
    # Unmatched emulated fills are classified AFTER the venue pass below
    # (not inline here) so a mirrored EXIT that the venue pass explains as
    # PATH_DIVERGENT can suppress the redundant MISSED for its own
    # never-filled counterpart -- see the pairing pass after the venue pass.
    deferred: list[EmulatedFill] = []
    for e in emulated:
        if e.leg == "EXIT" and e.close_cause in SYNTHETIC_CAUSES:
            out.append(ClassifiedFill(FillClass.SYNTHETIC, e, None, e.qty, "engine-side close (margin call / intraday cap)")); continue
        key = (e.intent, e.leg, _side_for_leg(e.is_long, e.leg))
        cands = [c for c in venue_by_key.get(key, []) if id(c) not in matched and c.target_bar_index == e.bar_index]
        v = cands[0] if cands else None
        if v is None:
            deferred.append(e); continue
        matched.add(id(v))
        if e.leg == "ENTRY" and entry_slip_bps > max_entry_slip_bps:
            out.append(ClassifiedFill(FillClass.ENTRY_SLIP, e, v, v.qty - e.qty, f"entry slip {entry_slip_bps:.1f} bps > {max_entry_slip_bps}")); continue
        delta = v.qty - e.qty
        if abs(delta) > dead_band_qty:
            out.append(ClassifiedFill(FillClass.QTY_DIVERGENT, e, v, delta, "same intent, qty beyond dead-band")); continue
        out.append(ClassifiedFill(FillClass.CONFIRMED, e, v, delta, ""))
    emulated_exit_intents = {e.intent for e in emulated if e.leg == "EXIT"}
    path_divergent_fills: list[VenueFill] = []
    for v in venue:
        if id(v) in matched:
            continue
        if v.cause in (T.FillCause.LIQUIDATION, T.FillCause.ADL, T.FillCause.MANUAL) or (v.cause == T.FillCause.UNATTRIBUTED and v.intent is None):
            out.append(ClassifiedFill(FillClass.UNATTRIBUTED_VENUE, None, v, v.qty, f"venue-initiated ({v.cause.value})")); continue
        if v.leg == "EXIT" and (v.intent in mirrored_intents or v.executed_trigger):
            if emulated_exit_intents and v.intent not in emulated_exit_intents:
                out.append(ClassifiedFill(FillClass.PATH_DIVERGENT, None, v, v.qty, "venue closed the cycle via a different leg"))
                path_divergent_fills.append(v)
                continue
            out.append(ClassifiedFill(FillClass.MIRROR_EARLY, None, v, v.qty, "mirrored level filled; ledger holds it")); continue
        if v.leg == "ENTRY" and v.executed_trigger and (ledger_position == 0.0 or (ledger_position > 0) != (v.side == T.Side.BUY)):
            out.append(ClassifiedFill(FillClass.TRIGGER_REVERSED, None, v, v.qty, "our TRIGGER executed, ledger flat/opposite")); continue
        if abs(real_position - ledger_position) > dead_band_qty:
            out.append(ClassifiedFill(FillClass.RETRACTED, None, v, v.qty, "venue fill without emulated counterpart; positions differ")); continue
        out.append(ClassifiedFill(FillClass.CONFIRMED, None, v, 0.0, "venue fill without counterpart within dead-band"))
    # M1: pair each PATH_DIVERGENT venue fill with at most ONE deferred
    # mirrored EXIT -- same close direction, qty within the dead-band --
    # and suppress only that one; everything else in `deferred` (including
    # an unrelated cycle's genuine MISSED) is unaffected.
    suppressed: set[int] = set()
    for v in path_divergent_fills:
        for e in deferred:
            if id(e) in suppressed:
                continue
            if e.leg == "EXIT" and e.intent in mirrored_intents and e.is_long == (v.side == T.Side.SELL) and abs(e.qty - v.qty) <= dead_band_qty:
                suppressed.add(id(e))
                break
    for e in deferred:
        if id(e) in suppressed:
            continue
        if e.intent in in_flight_intents:
            out.append(ClassifiedFill(FillClass.IN_FLIGHT, e, None, e.qty, "action non-terminal; deferred"))
        else:
            out.append(ClassifiedFill(FillClass.MISSED, e, None, e.qty, "emulated fill, no venue fill, nothing in flight"))
    return out

def _delta_candidates(ef: dict, book_diff: dict[str, IntentState], prev_book: dict) -> list:
    """Every `prev_book` intent that LEFT the book this bar (`book_diff`
    reads `CANCELLED` on its key -- "no longer resting", which covers a
    fill), whose `kind` is one that can open/hold a position
    (`ENTRY`/`MARKET`/`RAW_ORDER` -- never a bare `EXIT`), and whose side
    matches the delta's own side -- in `book_diff` iteration order
    (deterministic: `book_diff`'s `CANCELLED` keys are appended in
    `prev_book`'s own insertion order, the mirror-index order).

    The side comparison is leg-aware (L6): for an ENTRY delta, the
    candidate order's side (`Intent.is_long`) equals the resulting
    position's own direction (`ef["is_long"]`) -- an order that opens a
    long position IS itself long. For an EXIT delta, the two conventions
    are OPPOSITE: the candidate is a reduce-side order (see
    `probe._delta_fill`'s docstring / `book._counts_as_entry`'s RAW_ORDER
    case) whose side is the OPPOSITE of the position it reduces -- a SELL
    order reduces a long."""
    out = []
    for k, state in book_diff.items():
        if state != IntentState.CANCELLED:
            continue
        it = prev_book.get(k)
        if it is None or it.kind not in ("ENTRY", "MARKET", "RAW_ORDER"):
            continue
        same_side = (it.is_long == ef["is_long"]) if ef["leg"] == "ENTRY" else (it.is_long != ef["is_long"])
        if same_side:
            out.append(it)
    return out

def emulated_from_settle(s, book_diff: dict[str, IntentState], prev_book: dict) -> list[EmulatedFill]:
    """The bar's `EmulatedFill`s (spec §4 PLAN DEFECT ruling): the engine's
    trade report only lists CLOSED trades, so a still-open entry (or an
    exit that merely reduces, rather than closes, a position) never shows
    up there -- `Ledger._result` already synthesizes those as
    `s.entry_fills` from the position delta. This combines both sources
    for bar `s.bar_index` (`n`):

    (a) every closed trade in `s.trades` (excluding `open_at_end`, spec
        §0 report-only rows) whose `entry_bar_index`/`exit_bar_index`
        equals `n` becomes one ENTRY/EXIT fill with the trade's own
        price and `close_cause`;
    (b) every `s.entry_fills` delta dict (already `bar_index == n` by
        construction) is resolved against `_delta_candidates` (the prior
        bar's settled book / its diff into this bar's settled book):
        zero or one candidate becomes one fill with that candidate's id
        (or `"?"` when there is no candidate at all); MORE than one
        candidate (a pyramided position: several same-side orders left
        the book this same bar) is a genuine ambiguity the engine's ABI
        does not resolve for us (spec §4 review finding 4/M4) -- if the
        candidates' own fixed `Intent.qty`s sum to the delta (within
        float noise), each becomes its OWN fill at its OWN qty (one fill
        per candidate, no ambiguity); otherwise ONE fill is emitted with
        `intent="?"` and `ambiguous=True` rather than guessing which
        order(s) actually filled by blindly attributing the WHOLE blended
        delta to an arbitrary one of them (the pre-fix bug: it read as a
        spurious `QTY_DIVERGENT` against whichever venue fill happened to
        share that arbitrary candidate's id, which the reconciler would
        then "top up", over-positioning). `close_cause` is always 0
        (UNKNOWN) for every delta fill: a position-delta fill is never a
        closed trade, so the engine's close-cause table doesn't apply.

    `book_diff`/`prev_book` are the caller's (Task 5's book_diff(prev,
    cur) and the PRIOR bar's settled_book) -- not recomputed here, so a
    caller that already has them from its own book-tracking loop pays no
    extra `settled_book` call.
    """
    n = s.bar_index
    out: list[EmulatedFill] = []
    for t in s.trades:
        if t.open_at_end:
            continue
        if t.entry_bar_index == n:
            out.append(EmulatedFill(t.entry_id, "ENTRY", t.is_long, t.qty, t.entry_price, n, t.close_cause))
        if t.exit_bar_index == n:
            out.append(EmulatedFill(t.exit_id, "EXIT", t.is_long, t.qty, t.exit_price, n, t.close_cause))
    for ef in s.entry_fills:
        cands = _delta_candidates(ef, book_diff, prev_book)
        if len(cands) <= 1:
            intent = cands[0].key.order_id if cands else "?"
            out.append(EmulatedFill(intent, ef["leg"], ef["is_long"], ef["qty"], ef["price"], ef["bar_index"], 0))
            continue
        fixed_qtys = [it.qty for it in cands]
        if all(q is not None for q in fixed_qtys) and abs(sum(fixed_qtys) - ef["qty"]) <= _EPS:
            for it, q in zip(cands, fixed_qtys):
                out.append(EmulatedFill(it.key.order_id, ef["leg"], ef["is_long"], q, ef["price"], ef["bar_index"], 0))
        else:
            out.append(EmulatedFill("?", ef["leg"], ef["is_long"], ef["qty"], ef["price"], ef["bar_index"], 0, ambiguous=True))
    return out
