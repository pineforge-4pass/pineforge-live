from __future__ import annotations
import ctypes, json
from pathlib import Path
from typing import Sequence
from . import abi
from .report import RunResult, collect, pending_order_layout

PATH_ORDER_AUTO, PATH_ORDER_HIGH_FIRST, PATH_ORDER_LOW_FIRST = 0, 1, 2

class EngineHandle:
    """One strategy handle. Setters are persistent configuration (spec §3): the
    caller turns every flag back off before a plain historical run."""

    def __init__(self, so_path: str | Path, params: dict | None = None):
        self.so_path = Path(so_path)
        self.lib = abi.load_library(self.so_path)
        self._s = self.lib.strategy_create(json.dumps(params or {}).encode())
        if not self._s:
            raise abi.EngineAbiError(f"strategy_create failed for {self.so_path}")
        self._layout = pending_order_layout(self.lib)
        self.setter_log: list[tuple[str, tuple]] = []   # ordered, feeds the epoch hash (§1)

    # --- configuration -----------------------------------------------------
    def _log(self, name: str, *args):
        self.setter_log.append((name, args))

    def set_input(self, key: str, value: str):
        self.lib.strategy_set_input(self._s, key.encode(), value.encode()); self._log("set_input", key, value)
    def set_override(self, key: str, value: str):
        self.lib.strategy_set_override(self._s, key.encode(), value.encode()); self._log("set_override", key, value)
    def set_trade_start_time(self, ms: int):
        self.lib.strategy_set_trade_start_time(self._s, ms); self._log("set_trade_start_time", ms)
    def set_chart_timezone(self, tz: str):
        self.lib.strategy_set_chart_timezone(self._s, tz.encode()); self._log("set_chart_timezone", tz)
    def set_syminfo_timezone(self, tz: str):
        self.lib.strategy_set_syminfo_timezone(self._s, tz.encode()); self._log("set_syminfo_timezone", tz)
    def set_syminfo_session(self, session: str):
        self.lib.strategy_set_syminfo_session(self._s, session.encode()); self._log("set_syminfo_session", session)
    def set_syminfo_type(self, t: str):
        self.lib.strategy_set_syminfo_type(self._s, t.encode()); self._log("set_syminfo_type", t)
    def set_syminfo_string(self, key: str, value: str) -> int:
        rc = self.lib.strategy_set_syminfo_string(self._s, key.encode(), value.encode()); self._log("set_syminfo_string", key, value); return rc
    def set_syminfo_mintick(self, v: float):
        self.lib.strategy_set_syminfo_mintick(self._s, v); self._log("set_syminfo_mintick", v)
    def set_syminfo_pointvalue(self, v: float):
        self.lib.strategy_set_syminfo_pointvalue(self._s, v); self._log("set_syminfo_pointvalue", v)
    def set_syminfo_metadata(self, key: str, v: float):
        self.lib.strategy_set_syminfo_metadata(self._s, key.encode(), v); self._log("set_syminfo_metadata", key, v)
    def set_realtime_tail(self, on: bool, horizon_bars: int):
        self.lib.strategy_set_realtime_tail(self._s, 1 if on else 0, int(horizon_bars)); self._log("set_realtime_tail", on, horizon_bars)
    def set_probe_suppress_tail_logic(self, on: bool):
        self.lib.strategy_set_probe_suppress_tail_logic(self._s, 1 if on else 0); self._log("set_probe_suppress_tail_logic", on)
    def set_path_order(self, mode: int):
        self.lib.strategy_set_path_order(self._s, int(mode)); self._log("set_path_order", mode)
    def set_broker_state_hash_recording(self, on: bool):
        self.lib.strategy_set_broker_state_hash_recording(self._s, 1 if on else 0); self._log("set_broker_state_hash_recording", on)

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
        rep = abi.ReportC()
        tf = script_tf.encode()
        self.lib.run_backtest_full(self._s, arr, n, tf, tf, 0, 0, 0, ctypes.byref(rep))
        try:
            err = self.lib.strategy_get_last_error(self._s)
            if err and self.lib.strategy_last_run_status(self._s) == 0:
                raise abi.EngineAbiError(err.decode("utf-8", "replace"))
            return collect(self.lib, self._s, rep, self._layout)
        finally:
            self.lib.report_free(ctypes.byref(rep))

    # --- accessors used by the probe (Plan B2) ------------------------------
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
