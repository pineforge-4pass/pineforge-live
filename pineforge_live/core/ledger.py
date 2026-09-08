"""The recompute ledger (spec §4 settle 1–3, 8): ledger(n) = run_backtest_full(bars[0..n])."""
from __future__ import annotations
import math, time
from dataclasses import dataclass, field
from typing import Any
from pineforge_live import types as T
from pineforge_live.bars.builder import bars_hash as roll_hash, compare_bar
from pineforge_live.engine.report import RunResult, TradeRow
from pineforge_live.journal import JournalConflict
from .ids import TradeKey, trade_keys, trades_sha256

class LedgerDivergence(RuntimeError):
    def __init__(self, cause: str, detail: dict | None = None):
        super().__init__(f"{cause}: {detail}"); self.cause, self.detail = cause, detail or {}
class BarsDivergence(RuntimeError): pass
class RecomputeAborted(RuntimeError): pass

@dataclass
class SettleResult:
    bar_index: int; bar: T.NormalizedBar; trades: list[TradeRow]; keys: list[TradeKey]
    new_closed: list[TradeKey]; new_opened: list[TradeKey]; hashes: list[int]
    position_size: float; position_avg_price: float | None; equity: float; equity_mtm: float
    pending_orders: list[dict[str, Any]]; cycle_seq: int; trail_best: float | None
    recompute_ms: int; trades_sha256: str

def _mtm(r: RunResult, close: float) -> float:
    if r.position_size == 0.0 or math.isnan(r.position_avg_price):
        return r.current_equity
    # position_size is signed (short < 0): confirmed against pineforge.h's
    # strategy_position_size doxygen -- "the script-facing signed position
    # size (`strategy.position_size` ...)" -- so no sign derivation from the
    # last open trade's direction is needed here.
    return r.current_equity + r.position_size * (close - r.position_avg_price)

class Ledger:
    def __init__(self, handle, spec, journal, runtime_config_hash: str):
        self.h, self.spec, self.j, self.rc_hash = handle, spec, journal, runtime_config_hash
        self.bars: list[T.NormalizedBar] = []; self.bars_hash = 0; self.last: SettleResult | None = None
    @property
    def n(self) -> int:
        return len(self.bars)

    def _run(self) -> tuple[RunResult, int]:
        t0 = time.perf_counter()
        r = self.h.run_full([b.ohlcv() for b in self.bars], self.spec.script_tf)
        if r.status != 0:
            raise RecomputeAborted("settle recompute aborted")
        return r, int((time.perf_counter() - t0) * 1000)

    def _result(self, r: RunResult, ms: int) -> SettleResult:
        # Controller ruling: SettleResult.keys (and therefore the G1 prefix
        # check and trades_sha256) EXCLUDE open_at_end rows -- they are
        # report-only (spec §0) and appear/disappear with the range-end.
        # `s.trades` keeps the raw list (open_at_end rows included) for
        # callers that want the report-only view; keying/hashing is done
        # only over the closed trades.
        n = self.n - 1
        closed = [t for t in r.trades if not t.open_at_end]
        keys = trade_keys(closed)
        return SettleResult(n, self.bars[-1], r.trades, keys, [k for k in keys if k.exit_bar == n], [k for k in keys if k.entry_bar == n],
                            r.broker_state_hash, r.position_size, None if math.isnan(r.position_avg_price) else r.position_avg_price,
                            r.current_equity, _mtm(r, self.bars[-1].c), r.pending_orders, r.position_cycle_seq,
                            None if math.isnan(r.trail_best_price) else r.trail_best_price, ms, trades_sha256(closed))

    def _journal(self, s: SettleResult):
        e = self.spec.epoch_hash()
        self.j.append_bar(e, s.bar, self.bars_hash)
        self.j.append_settlement({"bar_index": s.bar_index, "epoch_hash": e, "runtime_config_hash": self.rc_hash,
                                  "bars_hash": self.bars_hash, "broker_state_hash": s.hashes[-1], "trades_len": len(s.trades),
                                  "position": s.position_size, "equity": s.equity_mtm, "trades_sha256": s.trades_sha256})

    def seed(self, history: list[T.NormalizedBar], expected_trades_sha256: str | None = None) -> SettleResult:
        if not history:
            raise ValueError("seed needs at least one bar")
        self.bars = list(history); self.bars_hash = 0
        for b in self.bars:
            self.bars_hash = roll_hash(self.bars_hash, b)
        r, ms = self._run(); s = self._result(r, ms)
        if len(s.hashes) != self.n:
            raise LedgerDivergence("hash_len", {"hashes": len(s.hashes), "bars": self.n})
        if expected_trades_sha256 is not None and s.trades_sha256 != expected_trades_sha256:
            raise LedgerDivergence("seed_mismatch", {"expected": expected_trades_sha256, "got": s.trades_sha256})
        self._journal(s); self.last = s
        return s

    def settle(self, bar: T.NormalizedBar, now_ms: int) -> SettleResult:
        if self.last is None:
            raise RuntimeError("seed() before settle()")
        prev = self.last
        if bar.ts_open <= self.bars[-1].ts_open:
            existing = next((b for b in reversed(self.bars) if b.ts_open == bar.ts_open), None)
            if existing is not None and compare_bar(existing, bar):
                self.j.append_incident("bars_divergence", {"ts_open": bar.ts_open, "fields": compare_bar(existing, bar), "now_ms": now_ms})
                raise BarsDivergence(f"revised settled bar {bar.ts_open}: {compare_bar(existing, bar)}")
            raise ValueError(f"bar {bar.ts_open} is not after the ledger's last bar")
        self.bars.append(bar); self.bars_hash = roll_hash(self.bars_hash, bar)
        r, ms = self._run(); s = self._result(r, ms)
        # G1: trade prefix and the previous bar's hash must be reproduced by this run
        m = prev.bar_index
        prefix = [k for k in s.keys if k.exit_bar <= m]
        if prefix != prev.keys:
            raise LedgerDivergence("g1_prefix", {"m": m, "prev": len(prev.keys), "now": len(prefix)})
        row = self.j.settlement(self.spec.epoch_hash(), m)
        if row is None or int(row["broker_state_hash"]) != s.hashes[m]:
            raise LedgerDivergence("g1_hash", {"m": m, "journaled": row and row["broker_state_hash"], "recomputed": s.hashes[m]})
        try:
            self._journal(s)
        except JournalConflict as e:
            self.j.append_incident("bars_divergence", {"ts_open": bar.ts_open, "conflict": str(e)})
            raise BarsDivergence(str(e)) from e
        self.last = s
        return s
