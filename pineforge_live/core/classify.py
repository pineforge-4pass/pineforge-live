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

class FillClass(enum.Enum):
    CONFIRMED = "CONFIRMED"; IN_FLIGHT = "IN_FLIGHT"; MISSED = "MISSED"; SYNTHETIC = "SYNTHETIC"; QTY_DIVERGENT = "QTY_DIVERGENT"
    PATH_DIVERGENT = "PATH_DIVERGENT"; MIRROR_EARLY = "MIRROR_EARLY"; TRIGGER_REVERSED = "TRIGGER_REVERSED"; ENTRY_SLIP = "ENTRY_SLIP"
    RETRACTED = "RETRACTED"; UNATTRIBUTED_VENUE = "UNATTRIBUTED_VENUE"

@dataclass(frozen=True)
class EmulatedFill:
    intent: str; leg: str; is_long: bool; qty: float; price: float; bar_index: int; close_cause: int = 0
@dataclass(frozen=True)
class VenueFill:
    intent: str | None; leg: str | None; side: T.Side; qty: float; price: float; target_bar_index: int; cause: T.FillCause
    client_id: str | None; executed_trigger: bool = False
@dataclass(frozen=True)
class ClassifiedFill:
    cls: FillClass; emulated: EmulatedFill | None; venue: VenueFill | None; qty_delta: float; note: str

def classify_bar(emulated, venue, *, in_flight_intents, mirrored_intents, dead_band_qty, ledger_position, real_position,
                 entry_slip_bps, max_entry_slip_bps) -> list[ClassifiedFill]:
    out: list[ClassifiedFill] = []
    venue_by_key: dict[tuple, list[VenueFill]] = {}
    for v in venue:
        venue_by_key.setdefault((v.intent, v.leg), []).append(v)
    matched: set[int] = set()
    # Unmatched emulated fills are classified AFTER the venue pass below
    # (not inline here) so a mirrored EXIT that the venue pass explains as
    # PATH_DIVERGENT can suppress the redundant MISSED for its own
    # never-filled counterpart -- see the loop after the venue pass.
    deferred: list[EmulatedFill] = []
    for e in emulated:
        if e.leg == "EXIT" and e.close_cause in SYNTHETIC_CAUSES:
            out.append(ClassifiedFill(FillClass.SYNTHETIC, e, None, e.qty, "engine-side close (margin call / intraday cap)")); continue
        cands = venue_by_key.get((e.intent, e.leg), [])
        v = next((c for c in cands if id(c) not in matched), None)
        if v is None:
            deferred.append(e); continue
        matched.add(id(v))
        if e.leg == "ENTRY" and entry_slip_bps > max_entry_slip_bps:
            out.append(ClassifiedFill(FillClass.ENTRY_SLIP, e, v, 0.0, f"entry slip {entry_slip_bps:.1f} bps > {max_entry_slip_bps}")); continue
        delta = v.qty - e.qty
        if abs(delta) > dead_band_qty:
            out.append(ClassifiedFill(FillClass.QTY_DIVERGENT, e, v, delta, "same intent, qty beyond dead-band")); continue
        out.append(ClassifiedFill(FillClass.CONFIRMED, e, v, delta, ""))
    emulated_exit_intents = {e.intent for e in emulated if e.leg == "EXIT"}
    path_divergence_seen = False
    for v in venue:
        if id(v) in matched:
            continue
        if v.cause in (T.FillCause.LIQUIDATION, T.FillCause.ADL, T.FillCause.MANUAL) or (v.cause == T.FillCause.UNATTRIBUTED and v.intent is None):
            out.append(ClassifiedFill(FillClass.UNATTRIBUTED_VENUE, None, v, v.qty, f"venue-initiated ({v.cause.value})")); continue
        if v.executed_trigger and (ledger_position == 0.0 or (ledger_position > 0) != (v.side == T.Side.BUY)):
            out.append(ClassifiedFill(FillClass.TRIGGER_REVERSED, None, v, v.qty, "our TRIGGER executed, ledger flat/opposite")); continue
        if v.intent in mirrored_intents and v.leg == "EXIT":
            if emulated_exit_intents and v.intent not in emulated_exit_intents:
                out.append(ClassifiedFill(FillClass.PATH_DIVERGENT, None, v, v.qty, "venue closed the cycle via a different leg"))
                path_divergence_seen = True
                continue
            out.append(ClassifiedFill(FillClass.MIRROR_EARLY, None, v, v.qty, "mirrored level filled; ledger holds it")); continue
        if abs(real_position - ledger_position) > dead_band_qty:
            out.append(ClassifiedFill(FillClass.RETRACTED, None, v, v.qty, "venue fill without emulated counterpart; positions differ")); continue
        out.append(ClassifiedFill(FillClass.CONFIRMED, None, v, 0.0, "venue fill without counterpart within dead-band"))
    for e in deferred:
        # A mirrored EXIT that never got its own venue fill, on a bar
        # where a PATH_DIVERGENT already fired (the venue closed the same
        # cycle via a DIFFERENT leg), is the same real-world event as that
        # divergence -- reporting it again as MISSED would just be a
        # noisier restatement of what PATH_DIVERGENT already says. v1
        # simplification: any PATH_DIVERGENT this call suppresses every
        # otherwise-unmatched mirrored EXIT (classify_bar's caller scopes
        # one bar's fills to one instrument/epoch, so there is normally at
        # most one such bracket-close event per call).
        if e.leg == "EXIT" and e.intent in mirrored_intents and path_divergence_seen:
            continue
        if e.intent in in_flight_intents:
            out.append(ClassifiedFill(FillClass.IN_FLIGHT, e, None, e.qty, "action non-terminal; deferred"))
        else:
            out.append(ClassifiedFill(FillClass.MISSED, e, None, e.qty, "emulated fill, no venue fill, nothing in flight"))
    return out

def _resolve_delta_intent(ef: dict, book_diff: dict[str, IntentState], prev_book: dict) -> str:
    """The intent id an `entry_fills` delta dict (spec §4 PLAN DEFECT
    ruling, `Ledger._result`) belongs to: the id of a `prev_book` intent
    that LEFT the book this bar (`book_diff` reads `CANCELLED`ON its key --
    "no longer resting", which covers a fill), whose `kind` is one that can
    open/hold a position (`ENTRY`/`MARKET`/`RAW_ORDER` -- never a bare
    `EXIT`) and whose side (`is_long`) matches the delta's own `is_long`.
    `"?"` when nothing in the pre-bar book explains it (the engine doesn't
    expose a last-entry-id fallback through this ABI)."""
    for k, state in book_diff.items():
        if state != IntentState.CANCELLED:
            continue
        it = prev_book.get(k)
        if it is None or it.kind not in ("ENTRY", "MARKET", "RAW_ORDER"):
            continue
        if it.is_long == ef["is_long"]:
            return it.key.order_id
    return "?"

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
        construction) becomes one fill whose `intent` is resolved via
        `_resolve_delta_intent` against `book_diff`/`prev_book` (the
        settled book from the PRIOR bar, and its diff into this bar's
        settled book) -- `close_cause` is always 0 (UNKNOWN): a
        position-delta fill is never a closed trade, so the engine's
        close-cause table doesn't apply to it.

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
        intent = _resolve_delta_intent(ef, book_diff, prev_book)
        out.append(EmulatedFill(intent, ef["leg"], ef["is_long"], ef["qty"], ef["price"], ef["bar_index"], 0))
    return out
