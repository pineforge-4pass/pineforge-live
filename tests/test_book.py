from pineforge_live.core import book as B
from pineforge_live.core import ids
from pineforge_live.core.ids import IntentKey
from pineforge_live.core.ledger import Ledger
from tests.helpers import load_bars, make_handle, corpus_spec, open_journal

def intent(oid, kind, is_long, stop=None, limit=None, from_entry="", resolved=True, created_bar=0, seq=1):
    return B.Intent(IntentKey(oid, kind, from_entry, seq), 0, is_long, kind, from_entry, stop, limit, None, resolved, created_bar, B.content_hash(stop, limit, None, is_long))

def test_book_diff_states():
    a = {"x": intent("x", "EXIT", True, stop=90.0), "y": intent("y", "EXIT", True, limit=110.0)}
    b = {"x": intent("x", "EXIT", True, stop=91.0), "z": intent("z", "ENTRY", False, stop=120.0)}
    d = B.book_diff(a, b)
    assert d == {"x": B.IntentState.MODIFIED, "y": B.IntentState.CANCELLED, "z": B.IntentState.RESTING}

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
    assert B.mirrorable(exit_ok)
    assert not B.mirrorable(exit_unresolved)
    assert not B.mirrorable(exit_no_level)
    assert not B.mirrorable(entry)

def test_settled_book_from_engine(test_so, test_feed, tmp_path):
    spec = corpus_spec(); h = make_handle(test_so, spec); j, _ = open_journal(tmp_path); j.append_epoch(spec.epoch_hash(), "{}")
    L = Ledger(h, spec, j, "rc"); s = L.seed(load_bars(test_feed, 3000))   # 3000 bars: the probe holds ≥1 pending order here
    # Pin the mirror's real field names this module relies on (verified against
    # a live run: pending_order_mirror.hpp's created_bar/created_seq match the
    # plan's guess exactly, no adaptation needed).
    assert "created_bar" in s.pending_orders[0] and "created_seq" in s.pending_orders[0]
    bk = B.settled_book(h, s)
    assert len(bk) == len(s.pending_orders) >= 1
    for k, it in bk.items():
        assert k == it.key.s and it.kind in ids.ORDER_TYPE_NAMES  # kind comes from the engine's OrderType names
        assert isinstance(it.level_resolved, bool)
