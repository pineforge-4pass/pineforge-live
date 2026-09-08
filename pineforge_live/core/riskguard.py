"""RiskGuard, STOP levels/dispositions with the sidecar sequencing (spec §5.5), G3 breakers (spec §1)."""
from __future__ import annotations
import math
import time
from collections import deque
from dataclasses import dataclass
from pineforge_live import types as T

Z95 = 1.959963984540054

def _now_ms() -> int:
    return int(time.time() * 1000)

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
    def self_test(self, known_names: frozenset[str] | None = None) -> None:
        """Refuses, at construction, any breaker that could never fire.

        `known_names` (m2) is the vocabulary of counters a sample can come
        from -- `reconcile.COUNTER_NAMES` for the live wiring, where a
        breaker's `name` IS the reconciler counter it watches. A breaker
        naming something nothing bumps observes `False` forever: no
        breach, no alert, and no error either, so a G3 lane that looks
        configured is silently absent (which is exactly what the suite and
        the harness had, both watching a counter called `orphan`). Passed
        `None` the check is skipped -- for a caller testing the rate
        machinery itself, with no counter vocabulary in play."""
        if known_names is not None:
            unknown = sorted({b.name for b in self.breakers} - known_names)
            if unknown:
                raise RuntimeError(f"G3 self-test failed: breaker(s) watch no known counter: {unknown}")
        bad_ub = [b.name for b in self.breakers if ub95(0, b.n_min) >= b.theta]
        if bad_ub:
            raise RuntimeError(f"G3 self-test failed: UB_95(0, n_min) >= theta for {bad_ub}")
        # n9: a breaker whose window is smaller than its own n_min can never
        # reach the n >= n_min gate `breached()` requires -- it would sit in
        # `alert()` forever no matter how bad the rate gets. Config error,
        # not a runtime bug, but self_test is exactly where to catch it.
        bad_window = [b.name for b in self.breakers if b.window_n < b.n_min]
        if bad_window:
            raise RuntimeError(f"G3 self-test failed: window_n < n_min (can never breach) for {bad_window}")

class RateWindow:
    def __init__(self, window_n: int):
        # N3: a non-positive window builds a `deque(maxlen=0)` that silently
        # drops every observation -- `n` stays 0, so `alert()`/`breached()`
        # can never fire and the breaker is a no-op nobody notices. Same
        # class of config error as `BreakerTable.self_test`'s
        # `window_n < n_min`, caught at construction for the same reason.
        if window_n < 1:
            raise ValueError(f"window_n must be >= 1, got {window_n}")
        self._d: deque[bool] = deque(maxlen=window_n)
        self._x = 0  # n9: running count of True in the window, kept in sync by observe() so x/breached are O(1) instead of a full rescan
    def observe(self, hit: bool) -> None:
        if self._d.maxlen and len(self._d) == self._d.maxlen and self._d[0]:
            self._x -= 1  # about to be evicted by the append below
        self._d.append(hit)
        if hit:
            self._x += 1
    @property
    def n(self) -> int: return len(self._d)
    @property
    def x(self) -> int: return self._x
    def rate(self) -> float: return self._x / self.n if self.n else 0.0
    def alert(self, b: Breaker) -> bool: return self.n < b.n_min and self._x > 0
    def breached(self, b: Breaker) -> bool: return self.n >= b.n_min and (self.rate() > b.theta or self._x > b.x_max)

@dataclass(frozen=True)
class RiskLimits:
    """Spec §5.5's full budget/breaker list. Enforced HERE (B2, this
    module): `max_abs_position`/`max_notional` (`RiskGuard.check_position`),
    `max_order_notional` (`check_order_notional`),
    `max_fill_actions_per_bar`/`max_book_ops_per_bar` (per-bar counters
    reset by `begin_bar`), `horizon_alert_pct` (`horizon`, called by
    `LiveCore.settle` before each recompute), and `hard_stop_max_hold_ms`
    (`StopController.hold_expired`).

    Enforced by `LiveCore` rather than here, because each needs state this
    module does not hold (n11 -- the old docstring claimed the reconciler
    counted them, which it never did): `max_daily_reconciles` is a per-UTC-day
    tally of the CORRECTION/FLATTEN actions `settle()` actually emits -- all
    but the `HARD_FLAT` and reconciler `FLATTEN` classes, which must not
    strand venue exposure, so neither is counted against this cap nor
    `max_order_notional` (NEW-A) -- and
    `disagree_twice` bounds consecutive settlements the reconciler skipped
    as not quiescent (spec §5.4). NOT enforced anywhere yet:
    `max_daily_realized_loss`; `stale_feed_ms`,
    `stale_eval_ms`, `bar_mismatch_streak`, `unexplained_divergence_pct`,
    `liquidation_distance_pct_min`, and `recompute_ms_p99_max` are declared
    but unwired -- they need venue/account inputs B3 wires into
    RateWindows/limits (plan §Spec coverage: "feed/eval staleness,
    bar-mismatch streak, unexplained-divergence and liquidation-distance
    breakers need venue/account inputs").
    """
    max_abs_position: float; max_notional: float; max_order_notional: float; max_fill_actions_per_bar: int; max_book_ops_per_bar: int
    max_daily_realized_loss: float; max_daily_reconciles: int; stale_feed_ms: int; stale_eval_ms: int; bar_mismatch_streak: int
    disagree_twice: int; unexplained_divergence_pct: float; liquidation_distance_pct_min: float; recompute_ms_p99_max: int
    horizon_alert_pct: float = 0.8
    hard_stop_max_hold_ms: int = 0  # 0 = disabled; spec §5.5 "HOLD carries hard_stop_max_hold after which the position is flattened"

class RiskViolation(RuntimeError): pass

_LEVEL_RANK = {T.StopLevel.NONE: 0, T.StopLevel.FLAT_ONLY: 1, T.StopLevel.HARD: 2}
_DISP_RANK = {T.StopDisposition.NONE: 0, T.StopDisposition.HOLD: 1, T.StopDisposition.FLATTEN: 2}
_HARD_ALLOWED_REDUCE_ONLY = {"dead_man", "static_exit"}

def stronger(a: tuple[T.StopLevel, T.StopDisposition], b: tuple[T.StopLevel, T.StopDisposition]) -> bool:
    """True iff STOP `a` outranks STOP `b` (spec §5.5): `level` first
    (`NONE < FLAT_ONLY < HARD`); at equal level, disposition breaks the tie
    (`FLATTEN > HOLD > NONE`). The single source of truth for "which of two
    (level, disposition) pairs wins" -- used by `raise_stop`'s monotonic
    escalation and `restore()`'s highest-open-row selection so ties resolve
    to the actually-stronger pair rather than whichever row was written or
    read last. Exported for `reconcile._escalate` (Task 8), which
    duplicated this rank table before this fix."""
    al, ad = a; bl, bd = b
    if _LEVEL_RANK[al] != _LEVEL_RANK[bl]:
        return _LEVEL_RANK[al] > _LEVEL_RANK[bl]
    return _DISP_RANK[ad] > _DISP_RANK[bd]

def bounded_disposition(level: T.StopLevel, disposition: T.StopDisposition) -> T.StopDisposition:
    """m4: a `HARD` stop always carries a bounded hold. `HARD` is the level
    at which no new order may be sent at all, so a `HARD` raised with
    disposition `NONE` would sit there with nothing to act on and -- worse
    -- no `raised_ms`, which is what `hard_stop_max_hold_ms` measures the
    hold against: the one automatic way out of a HARD stop (`hold_expired`
    -> flatten, spec 5.5 "HOLD carries hard_stop_max_hold after which the
    position is flattened") would never become reachable. So `(HARD,
    NONE)` normalises to `(HARD, HOLD)` on both the raise path and the
    restore path, in memory and in the durable row alike. Every other pair
    passes through untouched: `FLAT_ONLY` genuinely has no disposition,
    and `HOLD`/`FLATTEN` are already bounded/terminal."""
    return T.StopDisposition.HOLD if (level is T.StopLevel.HARD and disposition is T.StopDisposition.NONE) else disposition

class StopController:
    """The STOP controller (spec §5.5): tracks the current STOP `level`/
    `disposition` and enforces the sidecar sequencing on every raise --
    the out-of-band `StopMarker` is written BEFORE the journal, and the
    journal BEFORE in-memory state, so a crash between any two steps
    still leaves the more durable record set (marker > journal > memory)
    and `restore()` can recover the true level on the next startup.

    `level` only ever escalates: `raise_stop` is a monotonic max over
    `stronger()`'s rank (`NONE < FLAT_ONLY < HARD` first, then at the SAME
    level `FLATTEN` beats `HOLD` beats `NONE` as the disposition -- a later
    escalation can toughen the response without lowering the level, but
    never softens it). `clear()` is the one path back to `NONE` -- by
    design it is not reachable from `raise_stop` at all, only from an
    explicit operator call (see `clear`'s own docstring).

    STOP durability does not depend on the journal (spec §5.5 `[r4]`): the
    in-memory level/disposition/cause is set unconditionally in a
    `finally` around the marker + journal writes in `raise_stop`, so a
    `JournalFault` from either write still leaves the process correctly
    STOPped in memory (and re-raises, so the caller still sees the
    fault) -- a STOP is never silently lost to a sidecar/journal problem."""

    def __init__(self, journal, marker, hard_stop_max_hold_ms: int = 0):
        self.j, self.m = journal, marker; self.level = T.StopLevel.NONE; self.disposition = T.StopDisposition.NONE; self.cause = ""
        self.hard_stop_max_hold_ms = hard_stop_max_hold_ms
        self.raised_ms: int | None = None  # n6: wall-clock ms of the current HARD/HOLD, for hold_expired(); None off HARD/HOLD
    def raise_stop(self, level: T.StopLevel, disposition: T.StopDisposition, cause: str) -> None:
        disposition = bounded_disposition(level, disposition)   # m4: HARD always holds
        if not stronger((level, disposition), (self.level, self.disposition)):
            return
        try:
            self.m.write(level.value, disposition.value, cause)         # 1. out-of-band marker
            self.j.append_stop(level.value, disposition.value, cause)   # 2. journal
        finally:
            # F1: the in-memory STOP takes effect regardless of what the
            # marker/journal writes did -- a marker-write JournalFault
            # (disk-full) or an append_stop JournalFault landing AFTER the
            # marker is written must never leave the process un-STOPped.
            # The exception (if any) still propagates past this finally so
            # the caller sees the fault; memory is set either way.
            self.level, self.disposition, self.cause = level, disposition, cause   # 3. memory
            self.raised_ms = _now_ms() if (level is T.StopLevel.HARD and disposition is T.StopDisposition.HOLD) else None
    def restore(self) -> None:
        """Startup recovery (spec §5.5 B3): replays every still-open
        (`cleared_ms IS NULL`) `stops` row from the journal, keeping the
        `stronger()` one (level first, then disposition) rather than the
        last row read -- so a restart after a crash mid-escalation
        (marker/journal written, memory never updated because the process
        died first) still comes back at the STOP level the durable record
        actually holds, not `NONE`, even when the open rows are not in
        rank order."""
        open_rows = self.j.rows("stops", "cleared_ms IS NULL", ())
        for r in open_rows:
            lvl = T.StopLevel(r["level"])
            disp = bounded_disposition(lvl, T.StopDisposition(r["disposition"]))   # m4, durable half
            if stronger((lvl, disp), (self.level, self.disposition)):
                self.level, self.disposition, self.cause = lvl, disp, r["cause"]
                self.raised_ms = r["created_ms"] if (lvl is T.StopLevel.HARD and disp is T.StopDisposition.HOLD) else None
    def clear(self, cause: str) -> bool:
        """Clears the STOP: `marker.clear()` (deletes the out-of-band
        file) then drains EVERY still-open `stops` row via repeated
        `journal.append_stop_cleared(cause)` calls (each stamps
        `cleared_ms`/`cleared_cause` on the latest open row, so who/why is
        auditable per row), then resets in-memory state to `NONE`.

        F2: a STOP raised through several escalations leaves one open row
        per escalation (`raise_stop` never mutates or supersedes an
        earlier row); clearing only the latest one left older rows open,
        and `restore()` would resurrect the highest of THOSE on the next
        startup even after the operator cleared. Looping until
        `append_stop_cleared` returns False drains all of them in one
        call.

        ONLY the operator calls this -- there is no automatic path back
        from a raised STOP. `raise_stop`'s monotonic-escalation contract
        exists precisely so nothing in the runtime can silently talk
        itself down from HARD/FLAT_ONLY; clearing is deliberately the one
        state transition that requires a human (or an explicit
        operator-driven tool) to say so via `cause`. Returns True iff at
        least one journal row was actually cleared -- calling this with
        nothing open is a no-op, not a fault, and still leaves the marker
        cleared and memory at `NONE`."""
        self.m.clear()
        cleared = False
        while self.j.append_stop_cleared(cause):
            cleared = True
        self.level, self.disposition, self.cause = T.StopLevel.NONE, T.StopDisposition.NONE, ""
        self.raised_ms = None
        return cleared
    def hold_expired(self, now_ms: int) -> bool:
        """True iff the controller has been continuously HARD/HOLD for at
        least `hard_stop_max_hold_ms` (spec §5.5: "HOLD carries
        hard_stop_max_hold after which the position is flattened").
        `hard_stop_max_hold_ms == 0` (the default) disables the check --
        an unbounded HOLD never expires on its own. Acting on a True
        result (escalating via `raise_stop(HARD, FLATTEN, ...)`) is B3's
        sequencing, not this method's."""
        return (
            self.hard_stop_max_hold_ms > 0
            and self.level is T.StopLevel.HARD
            and self.disposition is T.StopDisposition.HOLD
            and self.raised_ms is not None
            and now_ms - self.raised_ms >= self.hard_stop_max_hold_ms
        )
    def permits(self, action_kind: str, increases_exposure: bool, reduce_only: bool) -> bool:
        """spec §5.5(b): `cancel` is always permitted; `NONE` permits
        everything; any level forbids exposure-increasing actions;
        `FLAT_ONLY` permits every reduce-only/cancel action; `HARD` additionally
        permits only `dead_man`, `static_exit`, and -- disposition-aware,
        m3/F3 -- `hard_flat`, which is a reduce-only pass ONLY under
        `FLATTEN` (spec: "under FLATTEN: one reduce-only HARD_FLAT MARKET"),
        never under plain `HOLD`.

        Two pieces of §5.5(b) are NOT expressible from this signature and
        are the executor's (B3), not this method's: (1) the cancel-set
        COMPOSITION -- "resting reduce-only exits and the dead-man are
        never in a cancel set" requires knowing which order is being
        cancelled, which `permits("cancel", ...)` does not see; (2) the
        post-`HARD_FLAT` one-shot ("then only cancels") and
        `hard_stop_max_hold` expiry (see `hold_expired`) are sequencing
        the executor drives -- this method only answers "is this action
        permitted right now," not "has HARD_FLAT already fired."""
        if action_kind == "cancel":
            return True
        if self.level == T.StopLevel.NONE:
            return True
        if increases_exposure:
            return False
        if self.level == T.StopLevel.FLAT_ONLY:
            return True
        if action_kind == "hard_flat":
            return reduce_only and self.disposition == T.StopDisposition.FLATTEN
        return reduce_only and action_kind in _HARD_ALLOWED_REDUCE_ONLY

class RiskGuard:
    def __init__(self, limits: RiskLimits):
        self.limits = limits; self._fills = 0; self._book_ops = 0
    def check_position(self, new_abs_position: float, price: float) -> str | None:
        new_abs_position = abs(new_abs_position)  # n7: "abs" in the name is not a guarantee -- trust the value, not the caller
        if new_abs_position > self.limits.max_abs_position: return "max_abs_position"
        if new_abs_position * price > self.limits.max_notional: return "max_notional"
        return None
    def check_order_notional(self, qty: float, price: float) -> str | None:
        return "max_order_notional" if abs(qty) * price > self.limits.max_order_notional else None
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
