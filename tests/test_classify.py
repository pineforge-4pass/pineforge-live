import re
from dataclasses import dataclass, field
from pineforge_live import types as T
from pineforge_live.core import classify as C
from pineforge_live.core.book import Intent, IntentState
from pineforge_live.core.ids import IntentKey
from pineforge_live.engine.report import TradeRow

def em(intent, leg="ENTRY", qty=1.0, cause=0, is_long=True):
    return C.EmulatedFill(intent, leg, is_long, qty, 100.0, 10, cause)
def vf(intent, leg="ENTRY", qty=1.0, side=T.Side.BUY, cause=T.FillCause.OURS, trig=False):
    return C.VenueFill(intent, leg, side, qty, 100.5, 10, cause, "c1", trig)

def base(**kw):
    d = dict(in_flight_intents=set(), mirrored_intents=set(), dead_band_qty=0.001, ledger_position=1.0, real_position=1.0,
             entry_slip_bps=0.0, max_entry_slip_bps=50.0)
    d.update(kw); return d

def test_confirmed_and_missed_and_in_flight():
    r = C.classify_bar([em("L")], [vf("L")], **base())
    assert [c.cls for c in r] == [C.FillClass.CONFIRMED]
    r = C.classify_bar([em("L")], [], **base(real_position=0.0))
    assert [c.cls for c in r] == [C.FillClass.MISSED]
    r = C.classify_bar([em("L")], [], **base(in_flight_intents={"L"}, real_position=0.0))
    assert [c.cls for c in r] == [C.FillClass.IN_FLIGHT]

def test_synthetic_qty_divergent_mirror_early_trigger_reversed_retracted():
    assert C.classify_bar([em("mc", "EXIT", cause=C.CLOSE_CAUSE_MARGIN_CALL)], [], **base())[0].cls == C.FillClass.SYNTHETIC
    r = C.classify_bar([em("L", qty=1.0)], [vf("L", qty=1.5)], **base(real_position=1.5))
    assert r[0].cls == C.FillClass.QTY_DIVERGENT and abs(r[0].qty_delta - 0.5) < 1e-9
    r = C.classify_bar([], [vf("x", "EXIT", side=T.Side.SELL)], **base(mirrored_intents={"x"}, real_position=0.0))
    assert r[0].cls == C.FillClass.MIRROR_EARLY
    r = C.classify_bar([], [vf("L", trig=True)], **base(ledger_position=0.0))
    assert r[0].cls == C.FillClass.TRIGGER_REVERSED
    r = C.classify_bar([], [vf(None, None)], **base(ledger_position=1.0, real_position=2.0))
    assert r[0].cls == C.FillClass.RETRACTED
    r = C.classify_bar([], [vf(None, None, cause=T.FillCause.LIQUIDATION, side=T.Side.SELL)], **base(real_position=0.0))
    assert r[0].cls == C.FillClass.UNATTRIBUTED_VENUE

def test_entry_slip_and_path_divergent():
    r = C.classify_bar([em("L")], [vf("L")], **base(entry_slip_bps=80.0))
    assert r[0].cls == C.FillClass.ENTRY_SLIP
    r = C.classify_bar([em("tp", "EXIT")], [vf("sl", "EXIT", side=T.Side.SELL)], **base(mirrored_intents={"tp", "sl"}, ledger_position=0.0, real_position=0.0))
    assert r[0].cls == C.FillClass.PATH_DIVERGENT

def test_close_cause_names_and_synthetic_causes_match_engine_header(engine_root):
    # Same idea as ids.py's test_order_type_names_match_engine_header, but
    # strategy_closed_trade_close_cause has no C enum -- just a doxygen
    # table on the function -- so the codes are parsed from that comment
    # block (bounded to the block immediately preceding the declaration,
    # not a loose regex over the whole file).
    hdr = (engine_root / "include/pineforge/pineforge.h").read_text()
    marker = "PF_API int strategy_closed_trade_close_cause"
    idx = hdr.index(marker)
    doc_end = hdr.rindex("*/", 0, idx)
    doc_start = hdr.rindex("/**", 0, doc_end)
    doc = hdr[doc_start:doc_end]
    found = dict(re.findall(r"`(\d+)`\s+([A-Z_]+)", doc))
    assert found, "no `<code>` NAME entries found in the close-cause doc block"
    max_code = max(int(k) for k in found)
    expected = tuple(found[str(i)] for i in range(max_code + 1))
    assert C.CLOSE_CAUSE_NAMES == expected
    assert C.CLOSE_CAUSE_MARGIN_CALL == expected.index("MARGIN_CALL")
    assert C.SYNTHETIC_CAUSES == frozenset(i for i, n in enumerate(expected) if n == "MARGIN_CALL" or n.startswith("INTRADAY_"))


def _intent(oid, kind, is_long, seq=0):
    return Intent(IntentKey(oid, kind, "", seq), 0, is_long, kind, "", None, None, None, True, 0, None, None, False,
                  "h")  # content_hash is opaque to classify.py; any string does


def trade(entry_bar, exit_bar, is_long=True, qty=1.0, cause=0, eid="L", xid="x", open_at_end=False):
    return TradeRow(0, 0, 100.0, 105.0, 1.0, 1.0, is_long, qty, 0.0, entry_bar, exit_bar, open_at_end, eid, xid, "", cause)


@dataclass
class _StubSettle:
    """A minimal SettleResult-shaped stub -- classify.emulated_from_settle
    only reads bar_index/trades/entry_fills."""
    bar_index: int
    trades: list = field(default_factory=list)
    entry_fills: list = field(default_factory=list)


def test_emulated_from_settle_maps_closed_trades_at_bar_n():
    s = _StubSettle(bar_index=10, trades=[
        trade(9, 10, is_long=True, qty=2.0, cause=2, eid="L", xid="XL"),   # exits this bar
        trade(10, 10, is_long=False, qty=1.0, cause=1, eid="S", xid="XS"), # same-bar round trip: both legs this bar
        trade(9, 9, is_long=True, qty=1.0, eid="old", xid="old_x"),        # neither leg this bar -- excluded
        trade(10, 20, is_long=True, qty=3.0, eid="oae", xid="", open_at_end=True),  # report-only -- excluded
    ])
    fills = C.emulated_from_settle(s, {}, {})
    sigs = {(f.intent, f.leg, f.qty, f.close_cause) for f in fills}
    assert sigs == {("XL", "EXIT", 2.0, 2), ("S", "ENTRY", 1.0, 1), ("XS", "EXIT", 1.0, 1)}


def test_emulated_from_settle_resolves_open_entry_intent():
    # Team-lead ruling: an entry_fills delta dict (position_delta 1.0,
    # explained by no closed trade) resolves its intent id from prev_book
    # -- the intent that left the book this bar (book_diff CANCELLED) on
    # the matching side, kind ENTRY/MARKET/RAW_ORDER.
    s = _StubSettle(bar_index=10, trades=[], entry_fills=[
        {"leg": "ENTRY", "is_long": True, "qty": 1.0, "price": 101.0, "bar_index": 10, "intent": None},
    ])
    prev_book = {"L|ENTRY||0": _intent("L", "ENTRY", True)}
    book_diff = {"L|ENTRY||0": IntentState.CANCELLED}
    fills = C.emulated_from_settle(s, book_diff, prev_book)
    assert len(fills) == 1
    f = fills[0]
    assert f.intent == "L" and f.leg == "ENTRY" and f.is_long is True and f.qty == 1.0 and f.price == 101.0
    assert f.bar_index == 10 and f.close_cause == 0


def test_emulated_from_settle_falls_back_to_unknown_intent_when_unresolved():
    s = _StubSettle(bar_index=10, entry_fills=[{"leg": "ENTRY", "is_long": True, "qty": 1.0, "price": 101.0, "bar_index": 10, "intent": None}])
    # nothing CANCELLED this bar -> nothing to resolve against
    assert C.emulated_from_settle(s, {}, {})[0].intent == "?"
    # a CANCELLED EXIT-kind intent on the matching side doesn't count (must be ENTRY/MARKET/RAW_ORDER)
    prev_book = {"X|EXIT||0": _intent("X", "EXIT", True)}
    book_diff = {"X|EXIT||0": IntentState.CANCELLED}
    assert C.emulated_from_settle(s, book_diff, prev_book)[0].intent == "?"
