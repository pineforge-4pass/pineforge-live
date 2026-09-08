from __future__ import annotations
import ctypes
from dataclasses import dataclass, field
from typing import Any
from . import abi

@dataclass(frozen=True)
class TradeRow:
    entry_time: int; exit_time: int; entry_price: float; exit_price: float
    pnl: float; pnl_pct: float; is_long: bool; qty: float; commission: float
    entry_bar_index: int; exit_bar_index: int; open_at_end: bool
    entry_id: str; exit_id: str; exit_comment: str; close_cause: int

@dataclass
class RunResult:
    status: int                      # 0 completed, 1 NOT_COMPLETED (aborted)
    trades: list[TradeRow]
    net_profit: float
    script_bars_processed: int
    broker_state_hash: list[int]     # empty unless recording was on
    position_size: float
    position_avg_price: float        # NaN when flat
    position_cycle_seq: int
    trail_best_price: float          # NaN when flat
    current_equity: float            # initial_capital + net profit (NOT strategy.equity)
    last_bar_dual_entry_path: int    # -1 none / engine codes
    pending_orders: list[dict[str, Any]] = field(default_factory=list)

_CTYPE = {"uint32_t": ctypes.c_uint32, "int32_t": ctypes.c_int32, "int64_t": ctypes.c_int64,
          "uint64_t": ctypes.c_uint64, "uint8_t": ctypes.c_uint8, "double": ctypes.c_double}

def pending_order_layout(lib: ctypes.CDLL) -> tuple[int, list[tuple[str, str, int, int]]]:
    """(struct size, [(name, ctype-name, offset, size)]) from strategy_pending_order_layout."""
    count = ctypes.c_int(0)
    descs = lib.strategy_pending_order_layout(ctypes.byref(count))
    fields = [(descs[i].name.decode(), descs[i].type.decode(), int(descs[i].offset), int(descs[i].size))
              for i in range(count.value)]
    size_field = next(f for f in fields if f[0] == "size")
    return max(f[2] + f[3] for f in fields), fields

def decode_pending_order(buf: bytes, fields: list[tuple[str, str, int, int]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, ty, off, size in fields:
        raw = buf[off:off + size]
        if ty.startswith("char"):
            out[name] = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        else:
            out[name] = _CTYPE[ty].from_buffer_copy(raw).value
    return out

def _s(p: bytes | None) -> str:
    return p.decode("utf-8", "replace") if p else ""

def collect(lib: ctypes.CDLL, s: ctypes.c_void_p, rep: abi.ReportC, layout) -> RunResult:
    status = lib.strategy_last_run_status(s)
    trades: list[TradeRow] = []
    if status == 0:
        for i in range(rep.trades_len):
            t = rep.trades[i]
            trades.append(TradeRow(t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.pnl, t.pnl_pct,
                                   bool(t.is_long), t.qty, t.commission, t.entry_bar_index, t.exit_bar_index,
                                   bool(t.open_at_end), _s(lib.strategy_closed_trade_entry_id(s, i)),
                                   _s(lib.strategy_closed_trade_exit_id(s, i)),
                                   _s(lib.strategy_closed_trade_exit_comment(s, i)),
                                   lib.strategy_closed_trade_close_cause(s, i)))
    hashes = [int(rep.broker_state_hash[i]) for i in range(rep.broker_state_hash_len)] if status == 0 else []
    size, fields = layout
    pending = []
    if status == 0:
        for i in range(lib.strategy_pending_orders_len(s)):
            buf = ctypes.create_string_buffer(size)
            if lib.strategy_pending_order_get(s, i, buf, size) == 0:
                row = decode_pending_order(buf.raw, fields); row["index"] = i; pending.append(row)
    return RunResult(status, trades, rep.net_profit if status == 0 else float("nan"),
                     int(rep.script_bars_processed) if status == 0 else 0, hashes,
                     lib.strategy_position_size(s), lib.strategy_position_avg_price(s),
                     lib.strategy_position_cycle_seq(s), lib.strategy_trail_best_price(s),
                     lib.strategy_current_equity(s), lib.strategy_last_bar_dual_entry_path(s), pending)
