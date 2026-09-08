"""RiskGuard, STOP levels/dispositions with the sidecar sequencing (spec §5.5), G3 breakers (spec §1)."""
from __future__ import annotations
import math
from collections import deque
from dataclasses import dataclass
from pineforge_live import types as T

Z95 = 1.959963984540054

def ub95(x: int, n: int) -> float:
    if n <= 0:
        return 1.0
    p = x / n; z2 = Z95 * Z95
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return centre + half

def n_min_for(theta: float) -> int:
    return math.ceil(Z95 * Z95 * (1 - theta) / theta)

@dataclass(frozen=True)
class Breaker:
    name: str; theta: float; window_n: int; n_min: int; x_max: int

class BreakerTable:
    def __init__(self, breakers: list[Breaker]):
        self.breakers = list(breakers)
    def self_test(self) -> None:
        bad = [b.name for b in self.breakers if ub95(0, b.n_min) >= b.theta]
        if bad:
            raise RuntimeError(f"G3 self-test failed: UB_95(0, n_min) >= theta for {bad}")

class RateWindow:
    def __init__(self, window_n: int):
        self._d: deque[bool] = deque(maxlen=window_n)
    def observe(self, hit: bool) -> None: self._d.append(hit)
    @property
    def n(self) -> int: return len(self._d)
    @property
    def x(self) -> int: return sum(1 for h in self._d if h)
    def rate(self) -> float: return self.x / self.n if self.n else 0.0
    def alert(self, b: Breaker) -> bool: return self.n < b.n_min and self.x > 0
    def breached(self, b: Breaker) -> bool: return self.n >= b.n_min and (self.rate() > b.theta or self.x > b.x_max)

@dataclass(frozen=True)
class RiskLimits:
    max_abs_position: float; max_notional: float; max_order_notional: float; max_fill_actions_per_bar: int; max_book_ops_per_bar: int
    max_daily_realized_loss: float; max_daily_reconciles: int; stale_feed_ms: int; stale_eval_ms: int; bar_mismatch_streak: int
    disagree_twice: int; unexplained_divergence_pct: float; liquidation_distance_pct_min: float; recompute_ms_p99_max: int
    horizon_alert_pct: float = 0.8

class RiskViolation(RuntimeError): pass

_RANK = {T.StopLevel.NONE: 0, T.StopLevel.FLAT_ONLY: 1, T.StopLevel.HARD: 2}
_HARD_ALLOWED_REDUCE_ONLY = {"dead_man", "static_exit", "hard_flat"}

class StopController:
    """The STOP controller (spec §5.5): tracks the current STOP `level`/
    `disposition` and enforces the sidecar sequencing on every raise --
    the out-of-band `StopMarker` is written BEFORE the journal, and the
    journal BEFORE in-memory state, so a crash between any two steps
    still leaves the more durable record set (marker > journal > memory)
    and `restore()` can recover the true level on the next startup.

    `level` only ever escalates: `raise_stop` is a monotonic max over
    `T.StopLevel`'s rank (`NONE < FLAT_ONLY < HARD`), and within the SAME
    level `FLATTEN` beats `HOLD`/`NONE` as the disposition (a later
    escalation can toughen the response without lowering the level, but
    never softens it). `clear()` is the one path back to `NONE` -- by
    design it is not reachable from `raise_stop` at all, only from an
    explicit operator call (see `clear`'s own docstring)."""

    def __init__(self, journal, marker):
        self.j, self.m = journal, marker; self.level = T.StopLevel.NONE; self.disposition = T.StopDisposition.NONE; self.cause = ""
    def raise_stop(self, level: T.StopLevel, disposition: T.StopDisposition, cause: str) -> None:
        if _RANK[level] < _RANK[self.level]:
            return
        if _RANK[level] == _RANK[self.level] and not (disposition == T.StopDisposition.FLATTEN and self.disposition != T.StopDisposition.FLATTEN):
            return
        self.m.write(level.value, disposition.value, cause)         # 1. out-of-band marker
        self.j.append_stop(level.value, disposition.value, cause)   # 2. journal
        self.level, self.disposition, self.cause = level, disposition, cause   # 3. memory
    def restore(self) -> None:
        """Startup recovery (spec §5.5 B3): replays every still-open
        (`cleared_ms IS NULL`) `stops` row from the journal, keeping the
        highest-ranked one -- so a restart after a crash mid-escalation
        (marker/journal written, memory never updated because the process
        died first) still comes back at the STOP level the durable record
        actually holds, not `NONE`."""
        open_rows = self.j.rows("stops", "cleared_ms IS NULL", ())
        for r in open_rows:
            lvl, disp = T.StopLevel(r["level"]), T.StopDisposition(r["disposition"])
            if _RANK[lvl] >= _RANK[self.level]:
                self.level, self.disposition, self.cause = lvl, disp, r["cause"]
    def clear(self, cause: str) -> bool:
        """Clears the STOP: `marker.clear()` (deletes the out-of-band
        file) then `journal.append_stop_cleared(cause)` (stamps
        `cleared_ms`/`cleared_cause` on the latest open `stops` row, so
        who/why is auditable), then resets in-memory state to `NONE`.

        ONLY the operator calls this -- there is no automatic path back
        from a raised STOP. `raise_stop`'s monotonic-escalation contract
        exists precisely so nothing in the runtime can silently talk
        itself down from HARD/FLAT_ONLY; clearing is deliberately the one
        state transition that requires a human (or an explicit
        operator-driven tool) to say so via `cause`. Returns
        `append_stop_cleared`'s own bool (True iff a journal row was
        actually cleared) -- calling this with nothing open is a no-op,
        not a fault, and still leaves the marker cleared and memory at
        `NONE`."""
        self.m.clear(); ok = self.j.append_stop_cleared(cause)
        self.level, self.disposition, self.cause = T.StopLevel.NONE, T.StopDisposition.NONE, ""
        return ok
    def permits(self, action_kind: str, increases_exposure: bool, reduce_only: bool) -> bool:
        if action_kind == "cancel":
            return True
        if self.level == T.StopLevel.NONE:
            return True
        if increases_exposure:
            return False
        if self.level == T.StopLevel.FLAT_ONLY:
            return True
        return reduce_only and action_kind in _HARD_ALLOWED_REDUCE_ONLY

class RiskGuard:
    def __init__(self, limits: RiskLimits, dead_band=None):
        self.limits, self.dead_band = limits, dead_band; self._fills = 0; self._book_ops = 0
    def check_position(self, new_abs_position: float, price: float) -> str | None:
        if new_abs_position > self.limits.max_abs_position: return "max_abs_position"
        if new_abs_position * price > self.limits.max_notional: return "max_notional"
        return None
    def check_order_notional(self, qty: float, price: float) -> str | None:
        return "max_order_notional" if qty * price > self.limits.max_order_notional else None
    def begin_bar(self) -> None: self._fills = 0; self._book_ops = 0
    def count_fill_action(self) -> None:
        self._fills += 1
        if self._fills > self.limits.max_fill_actions_per_bar: raise RiskViolation("max_fill_actions_per_bar")
    def count_book_op(self) -> None:
        self._book_ops += 1
        if self._book_ops > self.limits.max_book_ops_per_bar: raise RiskViolation("max_book_ops_per_bar")
    def horizon(self, consumed_bars: int, horizon_bars: int) -> str:
        if consumed_bars >= horizon_bars: return "rotate"
        return "alert" if consumed_bars >= self.limits.horizon_alert_pct * horizon_bars else "ok"
