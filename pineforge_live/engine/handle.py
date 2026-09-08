from __future__ import annotations
import ctypes, json
from pathlib import Path
from typing import Sequence
from . import abi
from .report import RunResult, collect, pending_order_layout

PATH_ORDER_AUTO, PATH_ORDER_HIGH_FIRST, PATH_ORDER_LOW_FIRST = 0, 1, 2

# --- setter application, factored out so run_full() can replay the ordered
# log onto a brand-new pf_strategy_t (see EngineHandle docstring). Each
# function takes (lib, s, *python-args) and does the encode + call; the
# public EngineHandle.set_* methods apply it to the CURRENT live strategy
# (so accessors between runs see the change) and append it to setter_log
# (the ordered record replayed on every future run).
def _apply_set_input(lib, s, key: str, value: str):
    lib.strategy_set_input(s, key.encode(), value.encode())
def _apply_set_override(lib, s, key: str, value: str):
    lib.strategy_set_override(s, key.encode(), value.encode())
def _apply_set_trade_start_time(lib, s, ms: int):
    lib.strategy_set_trade_start_time(s, ms)
def _apply_set_chart_timezone(lib, s, tz: str):
    lib.strategy_set_chart_timezone(s, tz.encode())
def _apply_set_syminfo_timezone(lib, s, tz: str):
    lib.strategy_set_syminfo_timezone(s, tz.encode())
def _apply_set_syminfo_session(lib, s, session: str):
    lib.strategy_set_syminfo_session(s, session.encode())
def _apply_set_syminfo_type(lib, s, t: str):
    lib.strategy_set_syminfo_type(s, t.encode())
def _apply_set_syminfo_string(lib, s, key: str, value: str) -> int:
    return lib.strategy_set_syminfo_string(s, key.encode(), value.encode())
def _apply_set_syminfo_mintick(lib, s, v: float):
    lib.strategy_set_syminfo_mintick(s, v)
def _apply_set_syminfo_pointvalue(lib, s, v: float):
    lib.strategy_set_syminfo_pointvalue(s, v)
def _apply_set_syminfo_metadata(lib, s, key: str, v: float):
    lib.strategy_set_syminfo_metadata(s, key.encode(), v)
def _apply_set_realtime_tail(lib, s, on: bool, horizon_bars: int):
    lib.strategy_set_realtime_tail(s, 1 if on else 0, int(horizon_bars))
def _apply_set_probe_suppress_tail_logic(lib, s, on: bool):
    lib.strategy_set_probe_suppress_tail_logic(s, 1 if on else 0)
def _apply_set_path_order(lib, s, mode: int):
    lib.strategy_set_path_order(s, int(mode))
def _apply_set_broker_state_hash_recording(lib, s, on: bool):
    lib.strategy_set_broker_state_hash_recording(s, 1 if on else 0)

_SETTER_APPLY = {
    "set_input": _apply_set_input,
    "set_override": _apply_set_override,
    "set_trade_start_time": _apply_set_trade_start_time,
    "set_chart_timezone": _apply_set_chart_timezone,
    "set_syminfo_timezone": _apply_set_syminfo_timezone,
    "set_syminfo_session": _apply_set_syminfo_session,
    "set_syminfo_type": _apply_set_syminfo_type,
    "set_syminfo_string": _apply_set_syminfo_string,
    "set_syminfo_mintick": _apply_set_syminfo_mintick,
    "set_syminfo_pointvalue": _apply_set_syminfo_pointvalue,
    "set_syminfo_metadata": _apply_set_syminfo_metadata,
    "set_realtime_tail": _apply_set_realtime_tail,
    "set_probe_suppress_tail_logic": _apply_set_probe_suppress_tail_logic,
    "set_path_order": _apply_set_path_order,
    "set_broker_state_hash_recording": _apply_set_broker_state_hash_recording,
}

class EngineHandle:
    """One loaded library + one ordered configuration log (spec §3, §1
    epoch hash) + the pf_strategy_t that produced the LAST run.

    A compiled strategy's own indicator/series (`var`-like) state is NOT
    reset by the engine's reset_run_state() -- confirmed against the
    engine's own tests/test_handle_reuse_reset.cpp, which resets
    engine-owned state only and requires the CALLER to reset any
    script/subclass-owned state (there is no C-ABI hook to do that for a
    compiled .dylib). So run_backtest_full on a REUSED pf_strategy_t is
    NOT a pure function of (bars, config) for a compiled script: a second
    run on the same handle can differ from a fresh one (observed: a
    phantom extra trade near the feed start; tracked as pineforge-engine
    issue #219). A strategy object here is therefore SINGLE-USE: every
    run_full() creates a fresh pf_strategy_t, replays the ordered
    setter_log onto it (so configuration is exactly what the caller asked
    for, in order), publishes it as the live strategy BEFORE calling into
    the engine (so a concurrent request_abort() targets the strategy that
    is actually running, not the previous idle one), runs, and only THEN
    frees the previous strategy -- so pending-order / scalar accessors
    called right after run_full() keep reading the strategy that produced
    that run. The handle is the configuration (setter_log) plus the last
    run's accessors, not a single persistent engine object.
    """

    def __init__(self, so_path: str | Path, params: dict | None = None):
        self.so_path = Path(so_path)
        self.lib = abi.load_library(self.so_path)
        self._params_json = json.dumps(params or {}).encode()
        self._layout = pending_order_layout(self.lib)
        self.setter_log: list[tuple[str, tuple]] = []   # ordered, feeds the epoch hash (§1)
        self._s = self._create_strategy()

    def _create_strategy(self):
        s = self.lib.strategy_create(self._params_json)
        if not s:
            raise abi.EngineAbiError(f"strategy_create failed for {self.so_path}")
        return s

    # --- configuration -----------------------------------------------------
    # Applied to the CURRENT live strategy (so an accessor called before the
    # next run sees it) and appended to setter_log (replayed on every future
    # run onto that run's fresh strategy). setter_log is the authoritative
    # state; Task 5's apply_epoch clears it, and the next run_full() replays
    # exactly the cleared-and-rebuilt list onto a brand-new strategy.
    def _apply_and_log(self, name: str, *args):
        result = _SETTER_APPLY[name](self.lib, self._s, *args)
        self.setter_log.append((name, args))
        return result

    def set_input(self, key: str, value: str):
        self._apply_and_log("set_input", key, value)
    def set_override(self, key: str, value: str):
        self._apply_and_log("set_override", key, value)
    def set_trade_start_time(self, ms: int):
        self._apply_and_log("set_trade_start_time", ms)
    def set_chart_timezone(self, tz: str):
        self._apply_and_log("set_chart_timezone", tz)
    def set_syminfo_timezone(self, tz: str):
        self._apply_and_log("set_syminfo_timezone", tz)
    def set_syminfo_session(self, session: str):
        self._apply_and_log("set_syminfo_session", session)
    def set_syminfo_type(self, t: str):
        self._apply_and_log("set_syminfo_type", t)
    def set_syminfo_string(self, key: str, value: str) -> int:
        return self._apply_and_log("set_syminfo_string", key, value)
    def set_syminfo_mintick(self, v: float):
        self._apply_and_log("set_syminfo_mintick", v)
    def set_syminfo_pointvalue(self, v: float):
        self._apply_and_log("set_syminfo_pointvalue", v)
    def set_syminfo_metadata(self, key: str, v: float):
        self._apply_and_log("set_syminfo_metadata", key, v)
    def set_realtime_tail(self, on: bool, horizon_bars: int):
        self._apply_and_log("set_realtime_tail", on, horizon_bars)
    def set_probe_suppress_tail_logic(self, on: bool):
        self._apply_and_log("set_probe_suppress_tail_logic", on)
    def set_path_order(self, mode: int):
        self._apply_and_log("set_path_order", mode)
    def set_broker_state_hash_recording(self, on: bool):
        self._apply_and_log("set_broker_state_hash_recording", on)

    # --- runs --------------------------------------------------------------
    def request_abort(self):
        self.lib.strategy_request_abort(self._s)

    def run_full(self, bars: Sequence, script_tf: str) -> RunResult:
        n = len(bars)
        arr = (abi.BarC * n)()
        for i, b in enumerate(bars):
            if isinstance(b, abi.BarC):
                arr[i] = b
            else:
                ts, o, h, l, c, v = b
                arr[i].timestamp, arr[i].open, arr[i].high, arr[i].low, arr[i].close, arr[i].volume = ts, o, h, l, c, v
        new_s = self._create_strategy()
        try:
            for name, args in self.setter_log:
                _SETTER_APPLY[name](self.lib, new_s, *args)
        except Exception:
            self.lib.strategy_free(new_s)
            raise
        rep = abi.ReportC()
        tf = script_tf.encode()
        # Publish BEFORE calling into the engine: request_abort() (called from
        # another thread while this run is in flight) reads self._s, so the
        # swap must land before run_backtest_full so an abort reaches the
        # strategy that is actually running, not the previous (idle) one.
        old_s, self._s = self._s, new_s
        try:
            self.lib.run_backtest_full(new_s, arr, n, tf, tf, 0, 0, 0, ctypes.byref(rep))
            err = self.lib.strategy_get_last_error(new_s)
            if err and self.lib.strategy_last_run_status(new_s) == 0:
                raise abi.EngineAbiError(f"{self.so_path}: {err.decode('utf-8', 'replace')}")
            return collect(self.lib, new_s, rep, self._layout)
        finally:
            self.lib.report_free(ctypes.byref(rep))
            if old_s:
                self.lib.strategy_free(old_s)

    # --- accessors used by the probe (Plan B2) ------------------------------
    # These read the strategy that produced the LAST run_full() (or, before
    # any run, the freshly-created init-time strategy).
    def probe_fill_qty(self, index: int, fill_price: float) -> tuple[int, float, bool, int]:
        qty, close_only, part = ctypes.c_double(float("nan")), ctypes.c_int(0), ctypes.c_int(-1)
        rc = self.lib.strategy_pending_order_fill_qty(self._s, index, fill_price, ctypes.byref(qty), ctypes.byref(close_only), ctypes.byref(part))
        return rc, qty.value, bool(close_only.value), part.value
    def level_resolved(self, index: int) -> int:
        return self.lib.strategy_pending_order_level_resolved(self._s, index)
    def effective_levels(self, index: int) -> tuple[int, float, float, float]:
        stop, limit, act = ctypes.c_double(float("nan")), ctypes.c_double(float("nan")), ctypes.c_double(float("nan"))
        rc = self.lib.strategy_pending_order_effective_levels(self._s, index, ctypes.byref(stop), ctypes.byref(limit), ctypes.byref(act))
        return rc, stop.value, limit.value, act.value

    def close(self):
        if self._s:
            self.lib.strategy_free(self._s); self._s = None
    def __enter__(self): return self
    def __exit__(self, *exc): self.close()
