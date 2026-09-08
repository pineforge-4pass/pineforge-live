import random
import re
import pytest
from pineforge_live.engine.report import TradeRow
from pineforge_live.core import ids


def trade(entry_bar, exit_bar, is_long=True, qty=1.0, cause=0, eid="L", xid="x"):
    return TradeRow(0, 0, 1.0, 2.0, 1.0, 1.0, is_long, qty, 0.0, entry_bar, exit_bar, False, eid, xid, "", cause)


def test_trade_keys_are_ordered_and_disambiguated():
    ks = ids.trade_keys([trade(1, 2), trade(1, 2), trade(3, 4, qty=2.0)])
    assert ks[0].fragment_ordinal == 0 and ks[1].fragment_ordinal == 1 and ks[2].fragment_ordinal == 0
    assert ids.trades_sha256([trade(1, 2)]) != ids.trades_sha256([trade(1, 2), trade(1, 2)])
    assert len(ids.trades_sha256([])) == 64


def test_trades_sha256_is_deterministic_and_order_sensitive():
    # finding 4: hand-pin the property trades_sha256/G1 actually rely on.
    a = [trade(1, 2), trade(3, 4, qty=2.0)]
    b = [trade(3, 4, qty=2.0), trade(1, 2)]
    assert ids.trades_sha256(a) == ids.trades_sha256(list(a))   # equal lists -> equal digest
    assert ids.trades_sha256(a) != ids.trades_sha256(b)         # swapped order -> different digest


def test_intent_key_string_form():
    k = ids.IntentKey("Long", "ENTRY", "", 7)
    assert k.s == "Long|ENTRY||7" and ids.IntentKey.parse(k.s) == k


def test_intent_key_round_trips_ids_containing_pipe_and_backslash():
    # finding 1: order_id/from_entry are arbitrary strings and may legally
    # contain '|' or '\\' -- .s must escape them and parse must recover them.
    k1 = ids.IntentKey("a|b", "EXIT", "c|d", 3)
    assert ids.IntentKey.parse(k1.s) == k1
    k2 = ids.IntentKey("a\\b", "ENTRY", "c\\|d", 1)
    assert ids.IntentKey.parse(k2.s) == k2


def test_intent_key_s_is_injective_for_the_collision_pair():
    # finding 1: these two distinct keys shared '.s' before escaping was added.
    k1 = ids.IntentKey("a|ENTRY", "EXIT", "", 7)
    k2 = ids.IntentKey("a", "ENTRY", "EXIT|", 7)
    assert k1.s != k2.s
    assert ids.IntentKey.parse(k1.s) == k1 and ids.IntentKey.parse(k2.s) == k2


def test_intent_key_parse_raises_on_wrong_arity():
    with pytest.raises(ValueError):
        ids.IntentKey.parse("a|b|ENTRY||7")


def test_order_type_names_match_engine_header(engine_root):
    hdr = (engine_root / "include/pineforge/engine.hpp").read_text()
    m = re.search(r"enum class OrderType\s*(?::\s*\w+)?\s*\{([^}]*)\}", hdr)
    assert m, "OrderType enum not found in engine.hpp"
    names = [n.strip().split("=")[0].strip() for n in m.group(1).split(",") if n.strip() and not n.strip().startswith("//")]
    for code, name in enumerate(names):
        assert ids.order_type_name(code) == name


def test_entry_kinds_are_nonempty_subset_of_order_type_names():
    assert ids.ENTRY_KINDS
    assert ids.ENTRY_KINDS <= set(ids.ORDER_TYPE_NAMES)
    for name in ids.ORDER_TYPE_NAMES:
        assert ids.is_entry_kind(name) == (name in ids.ENTRY_KINDS)


def test_entry_kinds_is_pinned_to_entry_only():
    # finding 2: pin the controller's ruling itself, not just the derivation.
    assert ids.ENTRY_KINDS == frozenset({"ENTRY"})


def test_trade_keys_preserve_report_order_not_sorted_order():
    # G1 compares report-order lists, not sorted ones: trade_keys() must
    # hand back keys in the same order the trades were reported, even
    # though the underlying trades are not in TradeKey sort order.
    trades = [trade(5, 6), trade(1, 2), trade(3, 4)]
    ks = ids.trade_keys(trades)
    assert [k.entry_bar for k in ks] == [5, 1, 3]
    assert ks != sorted(ks)


def test_trade_key_ordering_is_stable_under_shuffle():
    # finding 3: pin the explicit sorted sequence (order=True field
    # precedence + the fragment_ordinal tie-break), not just that sorting
    # doesn't raise -- "sorted(shuffled) == sorted(ks)" holds trivially for
    # any total order.
    trades = [trade(5, 6), trade(1, 2), trade(3, 4), trade(2, 3), trade(1, 2)]
    ks = ids.trade_keys(trades)
    shuffled = list(ks)
    random.shuffle(shuffled)
    assert sorted(shuffled) == sorted(ks)
    assert [(k.entry_bar, k.fragment_ordinal) for k in sorted(shuffled)] == [(1, 0), (1, 1), (2, 0), (3, 0), (5, 0)]
