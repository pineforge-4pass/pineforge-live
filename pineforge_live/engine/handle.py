from __future__ import annotations
import ctypes, json
from pathlib import Path
from typing import Sequence
from . import abi
from .report import RunResult, collect, pending_order_layout
from ..bars.policy import tf_ms

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

    Final 2 / B2 usage: `run_full(..., per_run=[("set_probe_suppress_tail_logic",
    (True,)), ("set_path_order", (mode,))])` applies additional setter
    calls on top of `setter_log` for ONE run only -- replayed onto the
    fresh strategy alongside `setter_log`, and re-applied to the resulting
    live strategy afterward so post-run accessors see them too -- without
    ever appending them to `setter_log`. This is how Plan B2 is expected to
    drive the per-run probe flags `EpochSpec.setter_sequence()` hashes but
    deliberately does not apply: calling the public `set_probe_suppress_tail_logic`/
    `set_path_order` methods instead would append to `setter_log` on every
    probe/evaluate call, growing the replay list without bound and
    diverging `setter_log` from `EpochSpec.setter_sequence()`.
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

    def run_full(self, bars: Sequence, script_tf: str, *, per_run: Sequence[tuple[str, tuple]] = ()) -> RunResult:
        # Validate BEFORE any engine call, and before a strategy is created:
        # run_backtest_full hands script_tf straight to the engine's own
        # timeframe parser, which does an uncaught C++ stoi cast on it -- a
        # non-numeric (or empty/None) timeframe aborts the whole PROCESS, not
        # just this call. Must be a non-empty string (tf_ms() itself assumes
        # .strip() and would raise AttributeError, not ValueError, on None)
        # accepted by tf_ms(); its ValueError propagates.
        # Final 2: `per_run` -- e.g. Plan B2's `set_probe_suppress_tail_logic`/
        # `set_path_order` (see EpochSpec.setter_sequence()'s docstring) --
        # is replayed onto the fresh strategy on top of setter_log for THIS
        # run only, and is never appended to setter_log: a naive B2 that
        # called the public set_* methods per probe/evaluate would grow the
        # replay list (and diverge setter_log from setter_sequence()) once
        # per call, without bound.
        if not isinstance(script_tf, str) or not script_tf:
            raise ValueError(f"bad timeframe {script_tf!r}")
        tf_ms(script_tf)
        n = len(bars)
        arr = (abi.BarC * n)()
        for i, b in enumerate(bars):
            if isinstance(b, abi.BarC):
                arr[i] = b
            else:
                # Prelim (Task 0 review finding 1): accept a bar object that
                # exposes .ohlcv() (e.g. types.NormalizedBar) directly, not
                # just an already-unpacked 6-tuple -- so callers such as
                # core.ledger.Ledger don't need a comprehension to convert
                # a bar list before every run_full() call.
                b = b.ohlcv() if hasattr(b, "ohlcv") else b
                ts, o, h, l, c, v = b
                arr[i].timestamp, arr[i].open, arr[i].high, arr[i].low, arr[i].close, arr[i].volume = ts, o, h, l, c, v
        # Hoisted above _create_strategy(): if either of these raised AFTER a
        # strategy was created, that strategy would leak (never freed) since
        # neither call is inside the setter-replay try/except below.
        rep = abi.ReportC()
        tf = script_tf.encode()
        new_s = self._create_strategy()
        try:
            for name, args in self.setter_log:
                _SETTER_APPLY[name](self.lib, new_s, *args)
            for name, args in per_run:
                _SETTER_APPLY[name](self.lib, new_s, *args)
        except Exception:
            self.lib.strategy_free(new_s)
            raise
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
            result = collect(self.lib, new_s, rep, self._layout)
        finally:
            self.lib.report_free(ctypes.byref(rep))
            if old_s:
                self.lib.strategy_free(old_s)
        # Final 2: re-apply per_run to the strategy that just produced this
        # run (now self._s) so pending-order / scalar accessors called
        # AFTER run_full() returns -- probe_fill_qty, level_resolved,
        # effective_levels, and report.collect()'s own scalar readers on a
        # future accessor call -- see the run's actual per-run
        # configuration, not just the (permanent) setter_log prefix.
        for name, args in per_run:
            _SETTER_APPLY[name](self.lib, self._s, *args)
        return result

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
