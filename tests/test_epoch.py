import dataclasses
import pytest
from pineforge_live import types as T
from pineforge_live.epoch import CodeIdentity, RuntimeConfig, EpochSpec, apply_epoch

def syminfo():
    return T.EngineSyminfo("ETHUSDT.P", "BINANCE:ETHUSDT.P", "BINANCE", "ETHUSDT", "crypto", "USDT", "ETH", 0.01, 100, 1.0, 1,
                           "24x7", "UTC", "base", "ETH perp")

def spec(**kw):
    base = dict(venue="BINANCE", instrument=T.InstrumentId("BINANCE", T.MarketType.PERP, "ETHUSDT"), script_tf="15",
                history_start_ms=1_577_836_800_000, horizon_bars=500_000,
                code_identity=CodeIdentity("e" * 64, "c" * 64, "s" * 64, {"compiler": "clang"}),
                syminfo=syminfo(), reference_tape_sha256="t" * 64,
                inputs=[("length", "152")], overrides=[("commission_value", "0.04"), ("slippage", "1")])
    base.update(kw)
    return EpochSpec(**base)

def test_epoch_hash_is_deterministic_and_order_sensitive():
    a, b = spec(), spec()
    assert a.epoch_hash() == b.epoch_hash() and len(a.epoch_hash()) == 64
    c = spec(overrides=[("slippage", "1"), ("commission_value", "0.04")])
    assert c.epoch_hash() != a.epoch_hash()          # setter ORDER is part of the epoch
    assert spec(horizon_bars=1).epoch_hash() != a.epoch_hash()
    assert spec(trail_refresh_policy="intrabar_best").epoch_hash() != a.epoch_hash()

def test_runtime_config_hash_separate_from_epoch():
    rc1 = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000, grace_ms=3_000, open_wait_ms=2_000, risk_limits={"max_abs_position": 1.0})
    rc2 = dataclasses.replace(rc1, dead_band_ticks=3)
    assert rc1.hash() != rc2.hash() and spec().epoch_hash() == spec().epoch_hash()

def test_apply_epoch_replays_exact_setter_sequence(test_so):
    from pineforge_live.engine import EngineHandle
    s = spec()
    with EngineHandle(test_so) as h:
        log = apply_epoch(h, s)
    assert log == s.setter_sequence()
    assert log[0][0] == "set_chart_timezone" and ("set_override", ("slippage", "1")) in log

class _FakeHandle:
    """Tiny stand-in for EngineHandle: setter methods are no-ops that log their
    call, except set_syminfo_string for one chosen key, which reports rejection
    (rc=1) the way the real engine ABI does."""
    def __init__(self, reject_key: str):
        self.setter_log: list[tuple[str, tuple]] = []
        self._reject_key = reject_key
    def _log(self, name, *args):
        self.setter_log.append((name, args))
    def set_chart_timezone(self, tz): self._log("set_chart_timezone", tz)
    def set_syminfo_timezone(self, tz): self._log("set_syminfo_timezone", tz)
    def set_syminfo_session(self, session): self._log("set_syminfo_session", session)
    def set_syminfo_type(self, t): self._log("set_syminfo_type", t)
    def set_syminfo_string(self, key, value):
        self._log("set_syminfo_string", key, value)
        return 1 if key == self._reject_key else 0
    def set_syminfo_mintick(self, v): self._log("set_syminfo_mintick", v)
    def set_syminfo_pointvalue(self, v): self._log("set_syminfo_pointvalue", v)
    def set_syminfo_metadata(self, key, v): self._log("set_syminfo_metadata", key, v)
    def set_input(self, key, value): self._log("set_input", key, value)
    def set_override(self, key, value): self._log("set_override", key, value)
    def set_trade_start_time(self, ms): self._log("set_trade_start_time", ms)

def test_apply_epoch_raises_on_rejected_syminfo_string():
    h = _FakeHandle(reject_key="currency")
    with pytest.raises(RuntimeError, match=r"currency.*rc=1"):
        apply_epoch(h, spec())
    # Fails fast: the rejected setter is the last thing logged, none of the
    # setters that would follow it in the sequence ran.
    assert h.setter_log[-1] == ("set_syminfo_string", ("currency", "USDT"))
    assert not any(name == "set_input" for name, _ in h.setter_log)
