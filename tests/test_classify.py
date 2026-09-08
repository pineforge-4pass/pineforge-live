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
    # em()/vf() price 100.0 vs 100.5 -> 50 bps of slip on every matched
    # ENTRY pair, so `max_entry_slip_bps=50.0` is exactly at the bound and
    # does not fire (m1: the comparison is strict).
    d = dict(in_flight_intents=set(), mirrored_intents=set(), dead_band_qty=0.001, ledger_position=1.0, real_position=1.0,
             max_entry_slip_bps=50.0)
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
    # m1: the slip is computed PER MATCHED PAIR from the two prices --
    # |100.5 - 100.0| / 100.0 x 1e4 = 50 bps -- against the config's own
    # `max_entry_slip_bps`, not handed in as a bar-wide scalar.
    assert C.entry_slip_bps(100.0, 100.5) == 50.0 and C.entry_slip_bps(0.0, 100.5) == 0.0
    r = C.classify_bar([em("L")], [vf("L")], **base(max_entry_slip_bps=10.0))
    assert r[0].cls == C.FillClass.ENTRY_SLIP and "50.0 bps > 10.0" in r[0].note
    r = C.classify_bar([em("tp", "EXIT")], [vf("sl", "EXIT", side=T.Side.SELL)], **base(mirrored_intents={"tp", "sl"}, ledger_position=0.0, real_position=0.0))
    assert r[0].cls == C.FillClass.PATH_DIVERGENT


def test_entry_slip_reports_the_real_qty_delta():
    # N10 (Task 5 review): ENTRY_SLIP must not force qty_delta to 0.0 when
    # the qty also diverged (P8) -- the journal loses the number otherwise.
    # (m1: an EXIT pair is never ENTRY_SLIP however far the prices sit.)
    r = C.classify_bar([em("L", qty=1.0)], [vf("L", qty=1.5)], **base(max_entry_slip_bps=10.0))
    assert r[0].cls == C.FillClass.ENTRY_SLIP and abs(r[0].qty_delta - 0.5) < 1e-9


def test_match_key_requires_the_correct_side():
    # M2 (P3): a same-id fill on the WRONG side is a submission/journal
    # bug, not a match -- must not be absorbed as CONFIRMED.
    r = C.classify_bar([em("L", is_long=True)], [vf("L", side=T.Side.SELL)], **base(real_position=0.0))
    assert C.FillClass.CONFIRMED not in {c.cls for c in r}
    assert {c.cls for c in r} == {C.FillClass.MISSED, C.FillClass.RETRACTED}


def test_match_key_requires_the_same_target_bar_index():
    # M2 (P3b): a venue fill journaled against a DIFFERENT bar than the
    # emulated fill being classified must never match, even with the same
    # intent/leg/side.
    e = em("L")   # em()'s fixed bar_index == 10
    v = C.VenueFill("L", "ENTRY", T.Side.BUY, 1.0, 100.5, 9, T.FillCause.OURS, "c1", False)   # target_bar_index == 9
    r = C.classify_bar([e], [v], **base(real_position=0.0))
    assert C.FillClass.CONFIRMED not in {c.cls for c in r}
    assert {c.cls for c in r} == {C.FillClass.MISSED, C.FillClass.RETRACTED}


def test_path_divergent_suppresses_only_the_one_paired_missed():
    # M1 (Task 5 review finding 1, the reviewer's P2 counterexample): a
    # PATH_DIVERGENT must suppress at most the ONE deferred mirrored EXIT
    # it actually pairs with (same close direction, qty within the
    # dead-band) -- an unrelated cycle's genuine MISSED (sl2, a same-bar
    # round trip's own close that never filled on the venue) must survive
    # even though an earlier PATH_DIVERGENT fired this same call.
    tp = em("tp", "EXIT", qty=1.0, is_long=True)
    l2 = em("L2", "ENTRY", qty=1.0, is_long=True)
    sl2 = em("sl2", "EXIT", qty=1.0, is_long=True)
    sl = vf("sl", "EXIT", qty=1.0, side=T.Side.SELL)
    l2v = vf("L2", "ENTRY", qty=1.0, side=T.Side.BUY)
    r = C.classify_bar([tp, l2, sl2], [sl, l2v],
                        **base(mirrored_intents={"tp", "sl", "sl2"}, ledger_position=0.0, real_position=1.0))
    by_intent = {(c.emulated.intent if c.emulated else c.venue.intent): c.cls for c in r}
    assert by_intent == {"L2": C.FillClass.CONFIRMED, "sl": C.FillClass.PATH_DIVERGENT, "sl2": C.FillClass.MISSED}


def test_exit_trigger_with_a_different_ledger_close_reads_path_divergent_not_trigger_reversed():
    # M3 (P10): our own emitted intrabar EXIT TRIGGER, when the ledger's
    # own recompute closed the SAME cycle via a DIFFERENT leg, reads
    # PATH_DIVERGENT (venue and ledger agree the cycle closed, just via
    # different legs) -- never TRIGGER_REVERSED, which is ENTRY-leg only.
    # tp's own MISSED is suppressed by the M1 pairing above.
    tp = em("tp", "EXIT", qty=1.0, is_long=True)
    sl = vf("sl", "EXIT", qty=1.0, side=T.Side.SELL, trig=True)
    r = C.classify_bar([tp], [sl], **base(mirrored_intents={"tp", "sl"}, ledger_position=0.0, real_position=0.0))
    assert [c.cls for c in r] == [C.FillClass.PATH_DIVERGENT]


def test_exit_trigger_reads_path_divergent_even_when_not_mirrored_follow_mode():
    # M3 (P10b): in follow mode nothing is in mirrored_intents, but an
    # executed_trigger EXIT fill must still be eligible for the
    # different-leg-close branch (previously it could never reach
    # PATH_DIVERGENT there and read CONFIRMED-with-note instead). tp's own
    # MISSED survives here (unlike P10 above) because the M1 pairing only
    # suppresses a MIRRORED deferred EXIT, and nothing is mirrored in
    # follow mode.
    tp = em("tp", "EXIT", qty=1.0, is_long=True)
    sl = vf("sl", "EXIT", qty=1.0, side=T.Side.SELL, trig=True)
    r = C.classify_bar([tp], [sl], **base(mirrored_intents=set(), ledger_position=0.0, real_position=0.0))
    assert {c.cls for c in r} == {C.FillClass.PATH_DIVERGENT, C.FillClass.MISSED}


def test_exit_trigger_with_no_alternate_ledger_close_reads_mirror_early():
    # M3 (P4): our own emitted EXIT TRIGGER, with NO alternate emulated
    # exit this bar to be "different" from, falls through the
    # different-leg-close branch's own inner check to MIRROR_EARLY --
    # TRIGGER_REVERSED never fires for an EXIT-leg fill (contrast with the
    # ENTRY-leg TRIGGER_REVERSED case in
    # test_synthetic_qty_divergent_mirror_early_trigger_reversed_retracted).
    sl = vf("sl", "EXIT", qty=1.0, side=T.Side.SELL, trig=True)
    r = C.classify_bar([], [sl], **base(mirrored_intents=set(), ledger_position=1.0, real_position=0.0))
    assert [c.cls for c in r] == [C.FillClass.MIRROR_EARLY]


def test_classify_bar_never_emits_qty_divergent_for_an_ambiguous_fill():
    # M4: an ambiguous delta fill (intent "?", from emulated_from_settle's
    # pyramided-candidates-don't-sum-to-the-delta case) can never
    # identity-match a real venue fill, so it always reads MISSED -- the
    # venue fills reconcile independently (CONFIRMED/RETRACTED on real vs
    # ledger position), never QTY_DIVERGENT.
    amb = C.EmulatedFill("?", "ENTRY", True, 2.0, 100.0, 10, 0, ambiguous=True)
    r = C.classify_bar([amb], [vf("L1", qty=1.0), vf("L2", qty=1.0)], **base(real_position=2.0, ledger_position=2.0))
    assert C.FillClass.QTY_DIVERGENT not in {c.cls for c in r}
    assert {c.cls for c in r} == {C.FillClass.MISSED, C.FillClass.CONFIRMED}


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


def _intent(oid, kind, is_long, seq=0, qty=None):
    return Intent(IntentKey(oid, kind, "", seq), 0, is_long, kind, "", None, None, None, True, 0, qty, None, False, False,
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


def test_emulated_from_settle_resolves_an_exit_delta_via_the_flipped_side_comparison():
    # L6: an EXIT delta's candidate is a REDUCE-side order -- Intent.is_long
    # is the ORDER's own side, the OPPOSITE of the position it reduces (a
    # SELL order reduces a long) -- so the side comparison must flip for
    # EXIT deltas, unlike ENTRY deltas where the two conventions agree.
    s = _StubSettle(bar_index=10, entry_fills=[
        {"leg": "EXIT", "is_long": True, "qty": 0.5, "price": 99.0, "bar_index": 10, "intent": None},
    ])
    prev_book = {"R|RAW_ORDER||0": _intent("R", "RAW_ORDER", False, qty=0.5)}
    book_diff = {"R|RAW_ORDER||0": IntentState.CANCELLED}
    fills = C.emulated_from_settle(s, book_diff, prev_book)
    assert len(fills) == 1 and fills[0].intent == "R" and fills[0].leg == "EXIT"


def test_emulated_from_settle_splits_pyramided_delta_by_fixed_candidate_qty():
    # M4 (Task 5 review finding 4, fix (a)): when the pyramided delta's
    # candidates' own fixed qtys sum to the delta, split into ONE
    # EmulatedFill per candidate at its OWN qty -- never blindly attribute
    # the WHOLE blended delta to an arbitrary first candidate (the pre-fix
    # bug: a spurious QTY_DIVERGENT the reconciler would wrongly "top up").
    s = _StubSettle(bar_index=10, entry_fills=[
        {"leg": "ENTRY", "is_long": True, "qty": 2.0, "price": 100.0, "bar_index": 10, "intent": None},
    ])
    prev_book = {"L1|ENTRY||0": _intent("L1", "ENTRY", True, qty=1.0), "L2|ENTRY||0": _intent("L2", "ENTRY", True, qty=1.0)}
    book_diff = {"L1|ENTRY||0": IntentState.CANCELLED, "L2|ENTRY||0": IntentState.CANCELLED}
    fills = C.emulated_from_settle(s, book_diff, prev_book)
    sigs = {(f.intent, f.qty, f.ambiguous) for f in fills}
    assert sigs == {("L1", 1.0, False), ("L2", 1.0, False)}


def test_emulated_from_settle_marks_ambiguous_when_candidate_qtys_dont_sum_to_the_delta():
    # M4 fix (c): when the candidates' fixed qtys don't sum to the delta,
    # the attribution genuinely can't be pinned -- emit ONE fill, intent
    # "?", flagged ambiguous, rather than guessing.
    s = _StubSettle(bar_index=10, entry_fills=[
        {"leg": "ENTRY", "is_long": True, "qty": 2.0, "price": 100.0, "bar_index": 10, "intent": None},
    ])
    prev_book = {"L1|ENTRY||0": _intent("L1", "ENTRY", True, qty=1.0), "L2|ENTRY||0": _intent("L2", "ENTRY", True, qty=0.5)}
    book_diff = {"L1|ENTRY||0": IntentState.CANCELLED, "L2|ENTRY||0": IntentState.CANCELLED}
    fills = C.emulated_from_settle(s, book_diff, prev_book)
    assert len(fills) == 1
    f = fills[0]
    assert f.intent == "?" and f.ambiguous is True and f.qty == 2.0


def test_emulated_from_settle_marks_ambiguous_when_a_candidate_has_no_fixed_qty():
    # M4: a candidate with no fixed qty at all (a strategy.exit leg sized
    # to "close the position") can't be summed either -- also ambiguous.
    s = _StubSettle(bar_index=10, entry_fills=[
        {"leg": "ENTRY", "is_long": True, "qty": 2.0, "price": 100.0, "bar_index": 10, "intent": None},
    ])
    prev_book = {"L1|ENTRY||0": _intent("L1", "ENTRY", True, qty=None), "L2|ENTRY||0": _intent("L2", "ENTRY", True, qty=1.0)}
    book_diff = {"L1|ENTRY||0": IntentState.CANCELLED, "L2|ENTRY||0": IntentState.CANCELLED}
    fills = C.emulated_from_settle(s, book_diff, prev_book)
    assert len(fills) == 1 and fills[0].intent == "?" and fills[0].ambiguous is True


# ---- final wave: M2 (PATH_DIVERGENT by pairability) ----

def _flat_only_over(classified):
    """The STOP `reconcile()` raises over `classified` -- the second half
    of the M2 claim (a venue double-close must reach the reconciler as
    something it escalates over, not as a `path_divergent` counter)."""
    from pineforge_live.core import reconcile as R
    d = R.reconcile(R.ReconcileInput(bar_index=10, classified=classified, ledger_position=0.0, real_position=-1.0,
                                     our_signed_fills=-1.0, price=100.0, quiescent=True, in_flight=set(),
                                     stop_level=T.StopLevel.NONE, missed_age_bars=0, missed_distance_bps=0.0,
                                     cfg=R.ReconcileConfig(1, 30.0, 10_000.0, 3, False),
                                     dead_band=R.DeadBand(0.001, 0.001, 5.0), mirror_early_today=0))
    return d.stop


def test_a_venue_double_close_reads_retracted_not_path_divergent():
    """M2: PATH_DIVERGENT means the venue closed the SAME cycle via the
    OTHER leg -- "net position equal". When BOTH bracket legs fill on the
    venue (OCO not honoured, or `closePosition` firing after the other
    leg), the ledger's `tp` is CONFIRMED by the first fill and the second
    (`sl`) has NO unmatched emulated exit left to pair with: the venue is
    a whole position short against a flat ledger. Pre-fix that read
    PATH_DIVERGENT purely because `sl` was a different id from the
    ledger's own exit -- the reconciler bumped a counter, `basis == real`
    so the account check passed, and nothing corrected or STOPped."""
    tp_e = em("tp", "EXIT", qty=1.0, is_long=True)
    tp_v = vf("tp", "EXIT", qty=1.0, side=T.Side.SELL)
    sl_v = vf("sl", "EXIT", qty=1.0, side=T.Side.SELL)
    r = C.classify_bar([tp_e], [tp_v, sl_v], **base(mirrored_intents={"tp", "sl"}, ledger_position=0.0, real_position=-1.0))
    assert [c.cls for c in r] == [C.FillClass.CONFIRMED, C.FillClass.RETRACTED]
    assert _flat_only_over(r) == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "RETRACTED: real ≠ ledger beyond dead-band")


def test_path_divergent_needs_a_qty_and_direction_pairable_exit():
    """M2, the two other halves of pairability: a deferred emulated EXIT
    on the OTHER close direction, or one whose qty is beyond the
    dead-band, is not the same cycle close and must not license
    PATH_DIVERGENT either."""
    other_direction = em("xs", "EXIT", qty=1.0, is_long=False)     # closes a SHORT -> a BUY
    r = C.classify_bar([other_direction], [vf("sl", "EXIT", qty=1.0, side=T.Side.SELL)],
                       **base(mirrored_intents={"xs", "sl"}, ledger_position=0.0, real_position=-1.0))
    assert {c.cls for c in r} == {C.FillClass.MISSED, C.FillClass.RETRACTED}
    wrong_qty = em("tp", "EXIT", qty=5.0, is_long=True)
    r = C.classify_bar([wrong_qty], [vf("sl", "EXIT", qty=1.0, side=T.Side.SELL)],
                       **base(mirrored_intents={"tp", "sl"}, ledger_position=0.0, real_position=-1.0))
    assert {c.cls for c in r} == {C.FillClass.MISSED, C.FillClass.RETRACTED}
