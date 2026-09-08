import pytest
from pineforge_live.core import book as B
from pineforge_live.core import ids
from pineforge_live.core.ids import IntentKey
from pineforge_live.core.ledger import Ledger
from tests.helpers import load_bars, make_handle, corpus_spec, corpus_spec_bracket, open_journal

def intent(oid, kind, is_long, stop=None, limit=None, from_entry="", resolved=True, created_bar=0, seq=1,
           qty=None, qty_percent=None, requested_partial=False, full_percent_exit_request=False):
    return B.Intent(IntentKey(oid, kind, from_entry, seq), 0, is_long, kind, from_entry, stop, limit, None, resolved, created_bar,
                     qty, qty_percent, requested_partial, full_percent_exit_request,
                     B.content_hash(stop, limit, None, is_long, qty))

def test_book_diff_states():
    a = {"x": intent("x", "EXIT", True, stop=90.0), "y": intent("y", "EXIT", True, limit=110.0)}
    b = {"x": intent("x", "EXIT", True, stop=91.0), "z": intent("z", "ENTRY", False, stop=120.0)}
    d = B.book_diff(a, b)
    assert d == {"x": B.IntentState.MODIFIED, "y": B.IntentState.CANCELLED, "z": B.IntentState.RESTING}

def test_book_diff_qty_only_change_reads_modified():
    # Nit 8 (Task 5 review): content_hash covers qty (L3), but nothing
    # pinned that a qty-only change (same stop/limit) actually reads
    # MODIFIED, not "unchanged".
    a = {"x": intent("x", "EXIT", True, stop=90.0, qty=1.0)}
    b = {"x": intent("x", "EXIT", True, stop=90.0, qty=2.0)}
    assert B.book_diff(a, b) == {"x": B.IntentState.MODIFIED}


def test_dual_entry_guard():
    two = {"L": intent("L", "ENTRY", True, stop=105.0), "S": intent("S", "ENTRY", False, stop=95.0)}
    assert B.dual_entry_guard(two, position_size=0.0)
    one = {"L": intent("L", "ENTRY", True, stop=105.0)}
    assert not B.dual_entry_guard(one, position_size=0.0)
    rev = {"S": intent("S", "ENTRY", False, limit=95.0)}
    assert B.dual_entry_guard(rev, position_size=1.0) and not B.dual_entry_guard(rev, position_size=-1.0)

def test_raw_order_counts_as_entry_only_when_opening_or_increasing_exposure():
    # Controller ruling: a RAW_ORDER (spec §3.6: a scripted strategy.order()
    # call bypasses the engine's ENTRY/EXIT classification) counts as an
    # "entry" for dual_entry_guard only when it would open or increase
    # exposure -- its side agrees with the held position's sign, or the
    # book is flat. Same two intents, only position_size varies below: the
    # guard result flips purely on which RAW_ORDER(s) that admits.
    book = {"Rl": intent("Rl", "RAW_ORDER", True, stop=105.0), "Rs": intent("Rs", "RAW_ORDER", False, stop=95.0)}
    assert B.dual_entry_guard(book, position_size=0.0)        # flat: both sides open exposure -> both count
    assert not B.dual_entry_guard(book, position_size=1.0)    # long: Rs (opposite) excluded, Rl alone can't pair
    assert not B.dual_entry_guard(book, position_size=-1.0)   # short: Rl (opposite) excluded, Rs alone can't pair

def test_mirrorable():
    exit_ok = intent("x", "EXIT", True, stop=90.0, resolved=True)
    exit_unresolved = intent("x", "EXIT", True, stop=90.0, resolved=False)
    exit_no_level = intent("x", "EXIT", True, resolved=True)
    entry = intent("e", "ENTRY", True, stop=90.0, resolved=True)
    assert B.mirrorable(exit_ok, position_size=0.0)
    assert not B.mirrorable(exit_unresolved, position_size=0.0)
    assert not B.mirrorable(exit_no_level, position_size=0.0)
    assert not B.mirrorable(entry, position_size=0.0)

def test_mirrorable_excludes_partial_exit_and_pyramided_legs():
    # Task 3 review finding 4: a partial-exit or pyramided-leg intent must
    # not be mirrored as a whole-position close -- the mirror carries the
    # signal directly (`requested_partial`, `qty` vs the held position),
    # not derived. No bracket fixture in this suite exposes a real
    # requested_partial row (the corpus's `bracket-partial-exit-qty-percent-01`
    # probe isn't wired as a test fixture), so this is synthetic, shaped
    # after the review's HALF_TP example (qty_percent=50, qty=1.0 against
    # a held position of 2.0).
    half_tp = intent("HALF_TP", "EXIT", False, limit=144.51, resolved=True, qty=1.0, qty_percent=50.0, requested_partial=True)
    assert not B.mirrorable(half_tp, position_size=2.0)   # requested_partial alone excludes it
    # A ladder leg (e.g. Compass's 40%-sized first leg) can carry a fixed
    # qty smaller than the held position without requested_partial being
    # set -- still excluded, on the qty-vs-position comparison.
    ladder_leg = intent("Compass First", "EXIT", False, stop=90.0, resolved=True, qty=2.0, requested_partial=False)
    assert not B.mirrorable(ladder_leg, position_size=5.0)
    # A whole-position exit (qty == the held size, or no fixed qty at all)
    # is unaffected.
    whole_qty = intent("REST_SL", "EXIT", False, stop=88.0, resolved=True, qty=2.0, requested_partial=False)
    assert B.mirrorable(whole_qty, position_size=2.0)
    whole_no_qty = intent("XL", "EXIT", False, stop=90.0, resolved=True, qty=None)
    assert B.mirrorable(whole_no_qty, position_size=2.0)


def test_mirrorable_qty_vs_position_has_a_dead_band_epsilon():
    # Nit 9 (Task 5 review): a fp-summed pyramided position (0.1+0.1+0.1 ==
    # 0.30000000000000004) with a whole-position leg at qty=0.3 must not
    # misread abs(qty) < abs(position_size) as "smaller" (a difference of
    # ~5.5e-17) and wrongly exclude it as a partial leg -- the default eps
    # (1e-9) covers this; a caller-supplied dead-band (LiveCore's own
    # DeadBand) works the same way for a coarser tolerance.
    position = 0.1 + 0.1 + 0.1
    assert position != 0.3   # sanity: this really is fp noise, not exact
    whole_qty_leg = intent("XL", "EXIT", False, stop=90.0, resolved=True, qty=0.3)
    assert B.mirrorable(whole_qty_leg, position_size=position)
    # A genuinely smaller qty (beyond eps) is still excluded.
    half_leg = intent("HALF", "EXIT", False, stop=90.0, resolved=True, qty=0.15)
    assert not B.mirrorable(half_leg, position_size=position)
    # A caller-supplied (coarser) dead-band widens the tolerance further.
    almost_whole = intent("ALMOST", "EXIT", False, stop=90.0, resolved=True, qty=0.299)
    assert not B.mirrorable(almost_whole, position_size=position)
    assert B.mirrorable(almost_whole, position_size=position, eps=0.01)

class _FakeHandle:
    """Stub `effective_levels`/`level_resolved` accessors, keyed by index --
    for pinning `settled_book`'s stale-handle raise path (L5, restored in
    the Task 6 prelim) without a real engine run."""
    def __init__(self, by_index: dict[int, tuple]):
        self._by_index = by_index
    def effective_levels(self, i):
        return self._by_index[i][0]
    def level_resolved(self, i):
        return self._by_index[i][1]

def _po(**kw):
    d = dict(index=0, id="x", type=2, is_long=True, from_entry="", created_position_cycle_seq=1,
             created_bar=5, stop_price=90.0, limit_price=float("nan"), qty=float("nan"), qty_percent=float("nan"),
             requested_partial=0, full_percent_exit_request=0)
    d.update(kw); return d

def test_settled_book_raises_on_a_stale_index_instead_of_fabricating_a_price():
    # L5 (Task 5 review, restored in the Task 6 prelim now that `book` is
    # only ever built inside the settle/seed accessor window -- see
    # `Ledger._settled_book`/`SettleResult.book` and `Probe.evaluate`'s use
    # of `self.L.last.book`): a stale handle read (effective_levels
    # rc != 0, or level_resolved < 0) is a genuine contract violation now,
    # not a normal not-yet-resolved order (rc==0, level_resolved==0) --
    # must raise loudly instead of fabricating/degrading a plausible Intent
    # from the mirror row's raw (possibly stale) price fields.
    class R:
        pending_orders = [_po()]
    h = _FakeHandle({0: ((-1, float("nan"), float("nan"), float("nan")), -1)})   # bad index: rc=-1, level_resolved=-1
    with pytest.raises(RuntimeError):
        B.settled_book(h, R())
    # rc==0 but level_resolved==-1 (shouldn't happen from a real accessor,
    # but the guard is `rc != 0 or lr < 0`, not `rc != 0 and lr < 0`) raises too.
    h2 = _FakeHandle({0: ((0, 90.0, float("nan"), float("nan")), -1)})
    with pytest.raises(RuntimeError):
        B.settled_book(h2, R())

def test_settled_book_normal_read_is_unaffected_by_the_stale_raise():
    # Companion pin: a normal not-yet-resolved order (rc==0, level_resolved==0)
    # -- the shape every probe path produces via self.L.last.book, never a
    # re-read against a stale handle -- must NOT raise.
    class R:
        pending_orders = [_po()]
    h = _FakeHandle({0: ((0, float("nan"), float("nan"), float("nan")), 0)})
    bk = B.settled_book(h, R())
    assert bk["x|EXIT||1"].level_resolved is False and bk["x|EXIT||1"].stop is None

def test_settled_book_from_engine(test_so, test_feed, tmp_path):
    spec = corpus_spec(); h = make_handle(test_so, spec); j, _ = open_journal(tmp_path); j.append_epoch(spec.epoch_hash(), "{}")
    L = Ledger(h, spec, j, "rc"); s = L.seed(load_bars(test_feed, 3000))   # 3000 bars: the probe holds ≥1 pending order here
    # Pin the mirror's real field names this module relies on (verified against
    # a live run: pending_order_mirror.hpp's created_bar/created_seq/
    # created_position_cycle_seq match the plan's guess exactly, no adaptation
    # needed).
    assert "created_bar" in s.pending_orders[0] and "created_seq" in s.pending_orders[0]
    assert "created_position_cycle_seq" in s.pending_orders[0]
    # L7 (Task 5 review finding 7): pin the four mirror field names
    # `settled_book` now reads strictly (po[...], not po.get(...)) -- a
    # mirror rename must fail this test loudly, not silently mirror a
    # half-position close as closePosition=true.
    for field in ("qty", "qty_percent", "requested_partial", "full_percent_exit_request"):
        assert field in s.pending_orders[0]
    bk = B.settled_book(h, s)
    assert len(bk) == len(s.pending_orders) >= 1
    for k, it in bk.items():
        assert k == it.key.s and it.kind in ids.ORDER_TYPE_NAMES  # kind comes from the engine's OrderType names
        assert isinstance(it.level_resolved, bool)
    # Review finding 8: pin the concrete facts this fixture actually settles
    # to, not just field names -- the probe's sole pending order is a bare
    # MARKET entry with no fixed qty.
    assert len(bk) == 1
    it = bk["S|MARKET||99"]
    assert it.kind == "MARKET" and it.is_long is False and it.created_bar == 2999
    assert it.level_resolved is True and it.stop is None and it.limit is None
    assert it.key.order_id == "S" and it.qty is None

def test_settled_book_from_bracket_probe_holds_a_non_market_intent(test_so_bracket, test_feed, tmp_path):
    # The sma probe (test_so above) only ever exposes a MARKET pending
    # order; this bracket probe (strategy.exit ATR stop/target) settles
    # with a real EXIT-kind mirror row -- pinned here at a bar count
    # (500, within the reported 500-5000 window) where that holds.
    spec = corpus_spec_bracket(); h = make_handle(test_so_bracket, spec); j, _ = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    L = Ledger(h, spec, j, "rc"); s = L.seed(load_bars(test_feed, 500))
    bk = B.settled_book(h, s)
    non_market = [it for it in bk.values() if it.kind != "MARKET"]
    assert non_market, f"expected >=1 non-MARKET intent, got kinds {[it.kind for it in bk.values()]}"
    assert all(it.kind in ids.ORDER_TYPE_NAMES for it in bk.values())

def test_settled_book_keys_by_created_cycle_seq_across_a_flat_to_long_transition(test_so_bracket, test_feed, tmp_path):
    # Review finding 1 (Medium, plan-vs-spec deviation): an exit order
    # created while the book was flat (`created_position_cycle_seq == 0`)
    # must keep the SAME IntentKey once the entry that follows it fills and
    # `result.cycle_seq` moves on to a new (nonzero) cycle -- keying by
    # `result.cycle_seq` instead re-keys it and reads as a brand-new order.
    # Verified live on `ta-pivot-atr-stop-target-01`/the 15m ETH feed: `XL`
    # (an ATR-stop EXIT bracket for the `Long` entry) is created flat at bar
    # 2004 and the `Long` MARKET entry fills on bar 2005 (cycle_seq 0 -> 100).
    spec = corpus_spec_bracket(); h = make_handle(test_so_bracket, spec); j, _ = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    L = Ledger(h, spec, j, "rc"); bars = load_bars(test_feed, 3000)
    s = L.seed(bars[:2001])
    for n in range(2001, 2005):
        s = L.settle(bars[n], 0)
    assert s.bar_index == 2004 and s.position_size == 0.0
    book_2004 = B.settled_book(h, s)
    xl = book_2004["XL|EXIT|Long|0"]
    assert xl.level_resolved is False and not B.mirrorable(xl, s.position_size)   # unresolved: not yet mirrorable

    s = L.settle(bars[2005], 0)
    assert s.bar_index == 2005 and s.position_size == 1.0 and s.cycle_seq != 0   # the entry filled, new cycle
    book_2005 = B.settled_book(h, s)
    assert "XL|EXIT|Long|0" in book_2005                           # same key survives the cycle change
    xl2 = book_2005["XL|EXIT|Long|0"]
    assert xl2.level_resolved is True and B.mirrorable(xl2, s.position_size)     # now resolved and mirrorable
    d = B.book_diff(book_2004, book_2005)
    assert d.get("XL|EXIT|Long|0") is None                         # unchanged -- no RESTING/CANCELLED pair on it
    assert d == {"Long|MARKET||0": B.IntentState.CANCELLED}        # only the filled MARKET entry left the book

    # Two bars later the ATR levels move (a same-id re-issue): the key is
    # still stable, and the diff correctly reads MODIFIED, not a
    # cancel+resting pair.
    s = L.settle(bars[2006], 0); book_2006 = B.settled_book(h, s)
    s = L.settle(bars[2007], 0); book_2007 = B.settled_book(h, s)
    d2 = B.book_diff(book_2006, book_2007)
    assert d2 == {"XL|EXIT|Long|0": B.IntentState.MODIFIED}
