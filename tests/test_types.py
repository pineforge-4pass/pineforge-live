import dataclasses, hashlib, json
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

def test_protocols_are_runtime_checkable():
    class FakeClock:
        def now_ms(self): return 0
        def venue_now_ms(self): return 0
        def skew_ms(self): return 0
        async def sleep_until(self, ms): return None
        def timeout(self, ms): return ms
    assert isinstance(FakeClock(), B.Clock)
    assert B.ADAPTER_API_VERSION == 1
