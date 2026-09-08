"""ctypes mirror of pineforge.h (ABI v4). Layouts copied field-for-field from the
header; pf_report_t is CALLER-allocated, so a mismatch corrupts memory -- the
ABI version is asserted at load. The streaming lifecycle is deliberately NOT
declared here (spec §1, G0)."""
from __future__ import annotations
import ctypes
from pathlib import Path

EXPECTED_PF_ABI = 4

class EngineAbiError(RuntimeError):
    pass

class BarC(ctypes.Structure):
    _fields_ = [("open", ctypes.c_double), ("high", ctypes.c_double), ("low", ctypes.c_double),
                ("close", ctypes.c_double), ("volume", ctypes.c_double), ("timestamp", ctypes.c_int64)]

class TradeC(ctypes.Structure):
    _fields_ = [("entry_time", ctypes.c_int64), ("exit_time", ctypes.c_int64),
                ("entry_price", ctypes.c_double), ("exit_price", ctypes.c_double),
                ("pnl", ctypes.c_double), ("pnl_pct", ctypes.c_double), ("is_long", ctypes.c_int),
                ("max_runup", ctypes.c_double), ("max_drawdown", ctypes.c_double),
                ("qty", ctypes.c_double), ("commission", ctypes.c_double),
                ("entry_bar_index", ctypes.c_int32), ("exit_bar_index", ctypes.c_int32),
                ("open_at_end", ctypes.c_int32)]

class TradeStatsC(ctypes.Structure):
    _fields_ = [("num_trades", ctypes.c_int32), ("num_wins", ctypes.c_int32),
                ("num_losses", ctypes.c_int32), ("num_even", ctypes.c_int32),
                ("percent_profitable", ctypes.c_double),
                ("net_profit", ctypes.c_double), ("net_profit_pct", ctypes.c_double),
                ("gross_profit", ctypes.c_double), ("gross_profit_pct", ctypes.c_double),
                ("gross_loss", ctypes.c_double), ("gross_loss_pct", ctypes.c_double),
                ("profit_factor", ctypes.c_double),
                ("avg_trade", ctypes.c_double), ("avg_trade_pct", ctypes.c_double),
                ("avg_win", ctypes.c_double), ("avg_win_pct", ctypes.c_double),
                ("avg_loss", ctypes.c_double), ("avg_loss_pct", ctypes.c_double),
                ("ratio_avg_win_avg_loss", ctypes.c_double),
                ("largest_win", ctypes.c_double), ("largest_win_pct", ctypes.c_double),
                ("largest_loss", ctypes.c_double), ("largest_loss_pct", ctypes.c_double),
                ("commission_paid", ctypes.c_double), ("expectancy", ctypes.c_double),
                ("max_consecutive_wins", ctypes.c_int32), ("max_consecutive_losses", ctypes.c_int32),
                ("avg_bars_in_trade", ctypes.c_double), ("avg_bars_in_wins", ctypes.c_double),
                ("avg_bars_in_losses", ctypes.c_double)]

class EquityStatsC(ctypes.Structure):
    _fields_ = [("max_equity_drawdown", ctypes.c_double), ("max_equity_drawdown_pct", ctypes.c_double),
                ("max_equity_runup", ctypes.c_double), ("max_equity_runup_pct", ctypes.c_double),
                ("buy_hold_return", ctypes.c_double), ("buy_hold_return_pct", ctypes.c_double),
                ("sharpe_tv", ctypes.c_double), ("sortino_tv", ctypes.c_double),
                ("sharpe_bar", ctypes.c_double), ("sortino_bar", ctypes.c_double),
                ("cagr", ctypes.c_double), ("calmar", ctypes.c_double),
                ("recovery_factor", ctypes.c_double), ("time_in_market_pct", ctypes.c_double),
                ("open_pl", ctypes.c_double)]

class MetricsC(ctypes.Structure):
    _fields_ = [("all", TradeStatsC), ("longs", TradeStatsC), ("shorts", TradeStatsC), ("equity", EquityStatsC)]

class EquityPointC(ctypes.Structure):
    _fields_ = [("time_ms", ctypes.c_int64), ("equity", ctypes.c_double), ("open_profit", ctypes.c_double)]

class SecurityDiagC(ctypes.Structure):
    _fields_ = [("sec_id", ctypes.c_int), ("feed_count", ctypes.c_int64),
                ("complete_count", ctypes.c_int64), ("partial_count", ctypes.c_int64)]

class TraceEntryC(ctypes.Structure):
    _fields_ = [("timestamp", ctypes.c_int64), ("bar_index", ctypes.c_int32),
                ("name_id", ctypes.c_int32), ("value", ctypes.c_double)]

class ReportC(ctypes.Structure):
    _fields_ = [("total_trades", ctypes.c_int), ("trades", ctypes.POINTER(TradeC)), ("trades_len", ctypes.c_int),
                ("net_profit", ctypes.c_double), ("input_bars_processed", ctypes.c_int64),
                ("script_bars_processed", ctypes.c_int64), ("security_feeds_total", ctypes.c_int64),
                ("security_complete_total", ctypes.c_int64), ("security_partial_total", ctypes.c_int64),
                ("magnifier_sub_bars_total", ctypes.c_int64), ("magnifier_sample_ticks_total", ctypes.c_int64),
                ("input_tf_seconds", ctypes.c_int), ("script_tf_seconds", ctypes.c_int),
                ("script_tf_ratio", ctypes.c_int), ("needs_aggregation", ctypes.c_int),
                ("bar_magnifier_enabled", ctypes.c_int),
                ("security_diag", ctypes.POINTER(SecurityDiagC)), ("security_diag_len", ctypes.c_int),
                ("trace", ctypes.POINTER(TraceEntryC)), ("trace_len", ctypes.c_int),
                ("trace_names", ctypes.POINTER(ctypes.c_char_p)), ("trace_names_len", ctypes.c_int),
                ("metrics", MetricsC),
                ("equity_curve", ctypes.POINTER(EquityPointC)), ("equity_curve_len", ctypes.c_int64),
                ("broker_state_hash", ctypes.POINTER(ctypes.c_uint64)), ("broker_state_hash_len", ctypes.c_int64)]

class PfFieldDescC(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("type", ctypes.c_char_p),
                ("offset", ctypes.c_uint32), ("size", ctypes.c_uint32)]

H = ctypes.c_void_p  # pf_strategy_t
_PROTOTYPES: dict[str, tuple[list, object]] = {
    "pf_abi_version": ([], ctypes.c_int),
    "pf_version_string": ([], ctypes.c_char_p),
    "strategy_create": ([ctypes.c_char_p], H),
    "strategy_free": ([H], None),
    "run_backtest_full": ([H, ctypes.POINTER(BarC), ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ReportC)], None),
    "report_free": ([ctypes.POINTER(ReportC)], None),
    "strategy_get_last_error": ([H], ctypes.c_char_p),
    "strategy_set_input": ([H, ctypes.c_char_p, ctypes.c_char_p], None),
    "strategy_set_override": ([H, ctypes.c_char_p, ctypes.c_char_p], None),
    "strategy_set_trade_start_time": ([H, ctypes.c_int64], None),
    "strategy_set_chart_timezone": ([H, ctypes.c_char_p], None),
    "strategy_set_syminfo_timezone": ([H, ctypes.c_char_p], None),
    "strategy_set_syminfo_session": ([H, ctypes.c_char_p], None),
    "strategy_set_syminfo_type": ([H, ctypes.c_char_p], None),
    "strategy_set_syminfo_string": ([H, ctypes.c_char_p, ctypes.c_char_p], ctypes.c_int),
    "strategy_set_syminfo_mintick": ([H, ctypes.c_double], None),
    "strategy_set_syminfo_pointvalue": ([H, ctypes.c_double], None),
    "strategy_set_syminfo_metadata": ([H, ctypes.c_char_p, ctypes.c_double], None),
    # --- ABI v4 live surface ---
    "strategy_request_abort": ([H], None),
    "strategy_last_run_status": ([H], ctypes.c_int),
    "strategy_set_realtime_tail": ([H, ctypes.c_int, ctypes.c_int], None),
    "strategy_set_probe_suppress_tail_logic": ([H, ctypes.c_int], None),
    "strategy_set_path_order": ([H, ctypes.c_int], None),
    "strategy_last_bar_dual_entry_path": ([H], ctypes.c_int),
    "strategy_set_broker_state_hash_recording": ([H, ctypes.c_int], None),
    "strategy_broker_state_hash": ([H], ctypes.c_uint64),
    "strategy_pending_orders_len": ([H], ctypes.c_int),
    "strategy_pending_order_get": ([H, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int),
    "strategy_pending_order_layout": ([ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(PfFieldDescC)),
    "strategy_pending_order_fill_qty": ([H, ctypes.c_int, ctypes.c_double, ctypes.POINTER(ctypes.c_double),
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
    "strategy_pending_order_level_resolved": ([H, ctypes.c_int], ctypes.c_int),
    "strategy_pending_order_effective_levels": ([H, ctypes.c_int, ctypes.POINTER(ctypes.c_double),
                                                 ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
    "strategy_trail_best_price": ([H], ctypes.c_double),
    "strategy_position_avg_price": ([H], ctypes.c_double),
    "strategy_position_cycle_seq": ([H], ctypes.c_int64),
    "strategy_closed_trade_entry_id": ([H, ctypes.c_int], ctypes.c_char_p),
    "strategy_closed_trade_exit_id": ([H, ctypes.c_int], ctypes.c_char_p),
    "strategy_closed_trade_exit_comment": ([H, ctypes.c_int], ctypes.c_char_p),
    "strategy_closed_trade_close_cause": ([H, ctypes.c_int], ctypes.c_int),
    "strategy_closed_trade_entry_incarnation": ([H, ctypes.c_int], ctypes.c_uint64),
    "strategy_position_size": ([H], ctypes.c_double),
    "strategy_current_equity": ([H], ctypes.c_double),
    "strategy_script_bars_processed": ([H], ctypes.c_int64),
}
V4_EXPORTS = tuple(n for n in _PROTOTYPES if n.startswith("strategy_") and n not in
                   ("strategy_create", "strategy_free", "strategy_get_last_error", "strategy_set_input",
                    "strategy_set_override", "strategy_set_trade_start_time", "strategy_set_chart_timezone",
                    "strategy_set_syminfo_timezone", "strategy_set_syminfo_session", "strategy_set_syminfo_type",
                    "strategy_set_syminfo_string", "strategy_set_syminfo_mintick", "strategy_set_syminfo_pointvalue",
                    "strategy_set_syminfo_metadata"))

def load_library(path: str | Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(path))
    for name, (argtypes, restype) in _PROTOTYPES.items():
        try:
            fn = getattr(lib, name)
        except AttributeError as e:
            raise EngineAbiError(f"{path}: missing export {name} (needs ABI v{EXPECTED_PF_ABI})") from e
        fn.argtypes, fn.restype = argtypes, restype
    abi = lib.pf_abi_version()
    if abi != EXPECTED_PF_ABI:
        raise EngineAbiError(f"{path}: pf_abi_version()={abi}, expected {EXPECTED_PF_ABI}")
    return lib
