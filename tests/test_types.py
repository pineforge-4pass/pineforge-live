import copy, dataclasses, pickle
import pytest
from pineforge_live import types as T
from pineforge_live.adapters import base as B

def test_engine_syminfo_hash_is_canonical():
    a = T.EngineSyminfo(ticker="ETHUSDT.P", tickerid="BINANCE:ETHUSDT.P", prefix="BINANCE", root="ETHUSDT", type="crypto",
                        currency="USDT", basecurrency="ETH", mintick=0.01, pricescale=100, pointvalue=1.0, minmove=1,
                        session="24x7", timezone="UTC", volumetype="base", description="ETH perp",
                        numeric_metadata={"b": 2.0, "a": 1.0}, string_metadata={"y": "1", "x": "0"})
    b = dataclasses.replace(a, numeric_metadata={"a": 1.0, "b": 2.0}, string_metadata={"x": "0", "y": "1"})
    assert a.hash() == b.hash() and len(a.hash()) == 64
    assert dataclasses.replace(a, mintick=0.1).hash() != a.hash()

def test_engine_syminfo_coerces_numeric_types_so_hash_is_stable():
    # Final 3: mintick=1 (int) vs 1.0 (float) configure the engine
    # identically -- strategy_set_syminfo_mintick takes a C double either
    # way -- but used to hash differently, so a JSON-sourced syminfo and a
    # hand-built one could silently disagree.
    kwargs = dict(ticker="ETHUSDT.P", tickerid="BINANCE:ETHUSDT.P", prefix="BINANCE", root="ETHUSDT", type="crypto",
                  currency="USDT", basecurrency="ETH", pointvalue=1.0, session="24x7", timezone="UTC",
                  volumetype="base", description="ETH perp")
    a = T.EngineSyminfo(mintick=1, pricescale=100, minmove=1, **kwargs)
    b = T.EngineSyminfo(mintick=1.0, pricescale=100.0, minmove=1.0, **kwargs)
    assert a.hash() == b.hash()
    assert isinstance(a.mintick, float) and isinstance(a.pointvalue, float)
    assert isinstance(a.pricescale, int) and isinstance(a.minmove, int)

def test_engine_syminfo_rejects_pricescale_or_minmove_in_numeric_metadata():
    # Final 1: pricescale/minmove are delivered to the engine by
    # EpochSpec.setter_sequence() itself (strategy_set_syminfo_metadata) --
    # a caller also putting either key in numeric_metadata would silently
    # double-set (and race) the value the epoch hash depends on.
    kwargs = dict(ticker="t", tickerid="t", prefix="", root="", type="crypto", currency="USD", basecurrency="USD",
                  mintick=0.01, pricescale=100, pointvalue=1.0, minmove=1, session="24x7", timezone="UTC",
                  volumetype="base", description="")
    with pytest.raises(ValueError):
        T.EngineSyminfo(numeric_metadata={"pricescale": 50.0}, **kwargs)
    with pytest.raises(ValueError):
        T.EngineSyminfo(numeric_metadata={"minmove": 2.0}, **kwargs)

def test_engine_syminfo_hash_unchanged_after_mutating_callers_dict():
    numeric = {"a": 1.0}
    a = T.EngineSyminfo(ticker="ETHUSDT.P", tickerid="BINANCE:ETHUSDT.P", prefix="BINANCE", root="ETHUSDT", type="crypto",
                        currency="USDT", basecurrency="ETH", mintick=0.01, pricescale=100, pointvalue=1.0, minmove=1,
                        session="24x7", timezone="UTC", volumetype="base", description="ETH perp",
                        numeric_metadata=numeric, string_metadata={})
    before = a.hash()
    numeric["z"] = 9.0  # mutate the caller's original dict, not a's copy
    assert a.hash() == before
    assert a.numeric_metadata == {"a": 1.0}

def test_order_action_is_frozen_and_complete():
    act = T.OrderAction(client_id="pfl-abc", intent_key="Long|ENTRY||1", level_version=0, action_seq=1,
                        kind=T.OrderKind.MARKET, side=T.Side.BUY, qty=1.5, price=None, stop_price=None,
                        reduce_only=False, close_position=False, position_side="BOTH", tif="GTC",
                        trigger_basis=T.TriggerBasis.LAST, lane=T.Lane.DISCRETIONARY, cls="TRIGGER", reason="probe fill")
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        act.qty = 2.0  # type: ignore[misc]

def test_adapter_error_raises():
    try:
        raise T.AdapterError(True, 0, T.ReasonClass.RETRYABLE, "x")
    except T.AdapterError as e:
        assert e.retryable is True and e.reason_class is T.ReasonClass.RETRYABLE and str(e) == "RETRYABLE: x"
    else:
        raise AssertionError("AdapterError was not raised/caught")

def test_adapter_error_pickle_and_deepcopy_round_trip():
    # Same-process round-trip of a value we just created (not untrusted input) —
    # exercises the __reduce__ fix that makes AdapterError picklable/deep-copyable.
    e = T.AdapterError(True, 100, T.ReasonClass.RETRYABLE, "boom")
    for got in (pickle.loads(pickle.dumps(e)), copy.deepcopy(e)):
        assert got == e
        assert (got.retryable, got.retry_after_ms, got.reason_class, got.message) == (True, 100, T.ReasonClass.RETRYABLE, "boom")

def test_canonical_sha256_enum_matches_its_value():
    assert T.canonical_sha256({"m": T.MarketType.SPOT}) == T.canonical_sha256({"m": "spot"})

def test_protocols_are_runtime_checkable():
    class FakeClock:
        def now_ms(self): return 0
        def venue_now_ms(self): return 0
        def skew_ms(self): return 0
        async def sleep_until(self, ms): return None
        def timeout(self, ms): return ms
    assert isinstance(FakeClock(), B.Clock)
    assert B.ADAPTER_API_VERSION == 1

def test_syminfo_rejects_non_integral_pricescale():
    with pytest.raises(ValueError):
        T.EngineSyminfo("t", "t", "p", "r", "crypto", "USDT", "ETH", 0.01, 100.5, 1.0, 1, "24x7", "UTC", "base", "d")
