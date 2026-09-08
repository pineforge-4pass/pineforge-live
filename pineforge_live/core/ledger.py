"""The recompute ledger (spec §4 settle 1–3, 8): ledger(n) = run_backtest_full(bars[0..n])."""
from __future__ import annotations
import math, time
from dataclasses import dataclass
from typing import Any, Sequence
from pineforge_live import types as T
from pineforge_live.bars.builder import bars_hash as roll_hash, compare_bar
from pineforge_live.bars.policy import tf_ms
from pineforge_live.engine.report import RunResult, TradeRow
from pineforge_live.journal import JournalConflict
from .ids import TradeKey, trade_keys


class LedgerDivergence(RuntimeError):
    """A ledger-consistency check failed (spec §4 G1, or a restart-time
    journal conflict): STOP(HARD) territory (spec §4.1) -- a divergent
    recompute against a previous incarnation, never a revised bar (that's
    `BarsDivergence`). `cause` names which check failed -- `hash_len`,
    `seed_mismatch`, `seed_conflict`, `g1_prefix`, `g1_hash`, `g1_trades`,
    `g1_bars`, `settlement_conflict` -- and `detail` carries the evidence.
    """
    def __init__(self, cause: str, detail: dict | None = None):
        super().__init__(f"{cause}: {detail}"); self.cause, self.detail = cause, detail or {}


class BarsDivergence(RuntimeError):
    """A settled bar was revised (spec §4.1 STOP(FLAT_ONLY)): the venue
    delivered different OHLCV for a `ts_open` the ledger already settled
    (or the last bar it settled), or the journal already holds a
    conflicting `bars` row for it. An `incidents` row (kind
    `bars_divergence`) is always journaled before this is raised."""


class RecomputeAborted(RuntimeError):
    """The engine reported a NOT_COMPLETED run (`RunResult.status != 0`)
    for this recompute. Never journaled: `seed()`/`settle()` only mutate
    ledger state after a full run *and* a successful journal write, so the
    ledger is exactly as it was before the call and the same bar/history
    can be retried."""


class LedgerGap(ValueError):
    """Raised by `settle()` when `bar` does not sit exactly one
    script-timeframe bucket after the ledger's last bar, or is itself
    still forming (spec §2: gaps are the caller's `carry_forward`
    responsibility, and a forming bar has no confirmed OHLCV to settle).
    `expected`/`got` are both `ts_open` ms values; for a forming-bar
    refusal they are equal and `forming` is True."""
    def __init__(self, expected: int, got: int, *, forming: bool = False):
        reason = "bar is still forming" if forming else f"expected ts_open {expected}, got {got}"
        super().__init__(f"non-contiguous bar {got}: {reason}")
        self.expected, self.got, self.forming = expected, got, forming


def keys_sha256(keys: Sequence[TradeKey]) -> str:
    """sha256 (hex) over an already-computed, ORDER-SENSITIVE list of
    `TradeKey`s -- the same digest `ids.trades_sha256` computes from raw
    trades, but taking keys directly so `settle()`'s G1 check can hash an
    arbitrary sub-prefix of `SettleResult.keys` without re-deriving keys
    from trades. Lives here (not in `ids.py`) per the controller ruling,
    to avoid a concurrent edit collision with Task 4's `ids.py` changes;
    a later task moves it there."""
    return T.canonical_sha256([list(k.__dict__.values()) for k in keys])


def _prefix_mismatch_detail(m: int, prefix: list[TradeKey], prev_keys: list[TradeKey]) -> dict[str, Any]:
    """An actionable `LedgerDivergence("g1_prefix", ...)` detail (review
    finding 7): the index of the first key where the recomputed `prefix`
    and the previously-settled `prev_keys` disagree, both keys at that
    index, and each side's length. `{'prev': N, 'now': N}` alone gives an
    operator nothing when the lengths already match and only one key
    differs; when they agree everywhere up to the shorter length, the
    divergence is a pure length mismatch and both key fields are `None`.
    """
    for i, (a, b) in enumerate(zip(prefix, prev_keys)):
        if a != b:
            return {"m": m, "index": i, "prefix_key": a.__dict__, "prev_key": b.__dict__,
                    "prefix_len": len(prefix), "prev_len": len(prev_keys)}
    return {"m": m, "index": min(len(prefix), len(prev_keys)), "prefix_key": None, "prev_key": None,
            "prefix_len": len(prefix), "prev_len": len(prev_keys)}


@dataclass
class SettleResult:
    """One `Ledger.seed()`/`settle()` call's outcome: the full recomputed
    state of ledger(n) (spec §4) plus the per-bar deltas Task 5/8 classify
    fills from.

    `keys`/`trades_sha256` EXCLUDE `open_at_end` trades (report-only, spec
    §0, and they appear/disappear with the range-end); `trades` is the raw
    engine report (open_at_end rows included) for callers that want the
    report-only view. `new_closed` is keys whose `exit_bar == n`;
    `new_opened` is keys whose `entry_bar == n` -- despite the name this is
    CLOSED trades that both opened and closed on bar n (same-bar round
    trips), NOT the position opened on bar n if it is still open (the
    engine's report only lists closed trades) -- see `entry_fills` for
    that.

    `position_delta` is `position_size(n) - position_size(n-1)` (always
    0.0 on `seed()`, which has no n-1 to diff against). `entry_fills`
    holds the single synthesized fill needed to explain `position_delta`
    when the bar-n closed trades above don't already account for all of it
    (a still-open entry, or an exit that reduced but didn't close a
    position) -- see `Ledger._result`. Always empty on `seed()`.
    """
    bar_index: int; bar: T.NormalizedBar; trades: list[TradeRow]; keys: list[TradeKey]
    new_closed: list[TradeKey]; new_opened: list[TradeKey]; hashes: list[int]
    position_size: float; position_avg_price: float | None; equity: float; equity_mtm: float
    pending_orders: list[dict[str, Any]]; cycle_seq: int; trail_best: float | None
    recompute_ms: int; trades_sha256: str
    position_delta: float; prev_position_size: float; entry_fills: list[dict[str, Any]]


def _mtm(r: RunResult, close: float) -> float:
    """Mark-to-market equity at `close` (spec §5.5): `current_equity` plus
    the open position's unrealized P&L, `position_size * (close -
    position_avg_price)`. `position_size` is confirmed against
    pineforge.h's `strategy_position_size` doxygen as the engine's
    script-facing SIGNED position size (short < 0), so no separate sign
    derivation from the last open trade's direction is needed here.
    Returns `current_equity` unchanged when flat (`position_size == 0`) or
    `position_avg_price` is NaN."""
    if r.position_size == 0.0 or math.isnan(r.position_avg_price):
        return r.current_equity
    return r.current_equity + r.position_size * (close - r.position_avg_price)


class Ledger:
    """The recompute ledger (spec §4): each `seed()`/`settle()` call is one
    full `run_backtest_full(bars[0..n])`, cross-checked against the
    journal (G1) and committed only after the journal write itself
    succeeds -- so a caught exception (`LedgerGap`, `RecomputeAborted`,
    `LedgerDivergence`, `BarsDivergence`) always leaves the ledger exactly
    as it was before the call, and the triggering bar/history can be
    retried unchanged."""

    def __init__(self, handle, spec, journal, runtime_config_hash: str):
        """`handle` is the `EngineHandle` this ledger recomputes through;
        `spec` the `EpochSpec` (`script_tf`, `epoch_hash()`); `journal` the
        `Journal` every bar/settlement row is appended to;
        `runtime_config_hash` the runtime-config identity journaled
        alongside every settlement."""
        self.h, self.spec, self.j, self.rc_hash = handle, spec, journal, runtime_config_hash
        self.bars: list[T.NormalizedBar] = []; self.bars_hash = 0; self.last: SettleResult | None = None

    @property
    def n(self) -> int:
        """Number of bars the ledger has settled so far (0 before `seed()`)."""
        return len(self.bars)

    def _run(self, bars: list[T.NormalizedBar]) -> tuple[RunResult, int]:
        """Runs the engine over `bars` from a fresh recompute and raises
        `RecomputeAborted` if the engine reports a NOT_COMPLETED run
        (`status != 0`) -- never journaled, and `bars` is a plain local
        list here, not `self.bars`, so an abort leaves nothing to unwind."""
        t0 = time.perf_counter()
        r = self.h.run_full([b.ohlcv() for b in bars], self.spec.script_tf)
        if r.status != 0:
            raise RecomputeAborted("settle recompute aborted")
        return r, int((time.perf_counter() - t0) * 1000)

    def _result(self, bars: list[T.NormalizedBar], r: RunResult, ms: int, prev: SettleResult | None) -> SettleResult:
        """Builds this run's `SettleResult` from the engine's `RunResult`
        over the (still-staged) full `bars`. Enforces the hash_len
        invariant (`len(r.broker_state_hash) == len(bars)`, review finding
        6) for both `seed()` and `settle()` -- a truncated hash list would
        otherwise silently journal the previous bar's hash under this
        bar's index.

        `prev` is the ledger's PRIOR `SettleResult` (`self.last` as it
        stood before this call): `None` on `seed()` (no earlier settlement
        to diff against, so `position_delta` is 0.0 and `entry_fills` is
        always empty), the previous settlement on `settle()`.
        """
        n = len(bars) - 1
        if len(r.broker_state_hash) != len(bars):
            raise LedgerDivergence("hash_len", {"hashes": len(r.broker_state_hash), "bars": len(bars)})
        # Controller ruling: SettleResult.keys (and therefore the G1 prefix
        # check and trades_sha256) EXCLUDE open_at_end rows -- they are
        # report-only (spec §0) and appear/disappear with the range-end.
        # `s.trades` keeps the raw list (open_at_end rows included) for
        # callers that want that report-only view; keying/hashing is done
        # only over the closed trades.
        closed = [t for t in r.trades if not t.open_at_end]
        keys = trade_keys(closed)
        avg_price = None if math.isnan(r.position_avg_price) else r.position_avg_price
        prev_position_size = prev.position_size if prev is not None else r.position_size
        position_delta = r.position_size - prev_position_size
        entry_fills: list[dict[str, Any]] = []
        if prev is not None:
            # PLAN DEFECT fix: the engine's trade report lists CLOSED
            # trades only, so a still-open entry (or an exit that merely
            # reduced, rather than closed, a position) never appears in
            # `keys` -- the part of position_delta these leave
            # "unexplained" is synthesized into one fill here for Task
            # 5/8 to classify (`intent` resolved from the book diff).
            sign = lambda k: k.qty if k.is_long else -k.qty
            explained = sum(sign(k) for k in keys if k.entry_bar == n) - sum(sign(k) for k in keys if k.exit_bar == n)
            unexplained = position_delta - explained
            if abs(unexplained) > 1e-12:
                leg = "ENTRY" if abs(r.position_size) > abs(prev_position_size) else "EXIT"
                entry_fills.append({
                    "leg": leg,
                    "is_long": (unexplained > 0) if leg == "ENTRY" else (prev_position_size > 0),
                    "qty": abs(unexplained),
                    "price": avg_price if leg == "ENTRY" else bars[-1].c,
                    "bar_index": n,
                    "intent": None,
                })
        return SettleResult(n, bars[-1], r.trades, keys, [k for k in keys if k.exit_bar == n], [k for k in keys if k.entry_bar == n],
                            r.broker_state_hash, r.position_size, avg_price, r.current_equity, _mtm(r, bars[-1].c),
                            r.pending_orders, r.position_cycle_seq, None if math.isnan(r.trail_best_price) else r.trail_best_price,
                            ms, keys_sha256(keys), position_delta, prev_position_size, entry_fills)

    def _settlement_row(self, s: SettleResult, bars_hash: int) -> dict[str, Any]:
        return {"bar_index": s.bar_index, "epoch_hash": self.spec.epoch_hash(), "runtime_config_hash": self.rc_hash,
                "bars_hash": bars_hash, "broker_state_hash": s.hashes[-1], "trades_len": len(s.keys),
                "position": s.position_size, "equity": s.equity_mtm, "trades_sha256": s.trades_sha256}

    def _journal_bar(self, s: SettleResult, bars_hash: int) -> dict[str, Any]:
        """Journals `s.bar`'s row at `bars_hash` (the ledger's rolling hash
        through this bar). Raises the bare `journal.JournalConflict` on a
        natural-key (`epoch_hash`, `ts_open`) conflict; `seed()`/`settle()`
        each translate that into the typed divergence that fits their
        context."""
        return self.j.append_bar(self.spec.epoch_hash(), s.bar, bars_hash)

    def _journal_settlement(self, s: SettleResult, bars_hash: int) -> dict[str, Any]:
        """Journals `s`'s settlement row. Raises the bare
        `journal.JournalConflict` on a natural-key (`epoch_hash`,
        `bar_index`) conflict; see `_journal_bar`."""
        return self.j.append_settlement(self._settlement_row(s, bars_hash))

    def seed(self, history: list[T.NormalizedBar], expected_trades_sha256: str | None = None) -> SettleResult:
        """Seeds the ledger from a restart/cold-start `history` (spec §4
        settle 1, B3 restart): runs the full history once and journals
        only its LAST bar + settlement, establishing `self.last` at
        `history`'s final bar so `settle()` can continue from there.

        `expected_trades_sha256`, when given (the restart path, spec §4
        settle 3), must match this run's recomputed `trades_sha256` or a
        `LedgerDivergence("seed_mismatch", ...)` is raised. A journal
        conflict while writing the bar/settlement row (e.g. a shifted
        history, or a different `runtime_config_hash`, replaying onto an
        already-populated journal) raises `LedgerDivergence("seed_conflict", ...)`
        -- always this cause here, regardless of which row conflicted;
        `settle()` distinguishes bar vs. settlement conflicts because it
        has the additional "revised settled bar" case to rule out.

        Like `settle()`, the ledger's own state (`self.bars`/`bars_hash`/
        `last`) is only assigned after the journal write succeeds, so a
        failed seed leaves the ledger unseeded (retryable) rather than
        half-seeded.
        """
        if not history:
            raise ValueError("seed needs at least one bar")
        bars = list(history)
        bh = 0
        for b in bars:
            bh = roll_hash(bh, b)
        r, ms = self._run(bars)
        s = self._result(bars, r, ms, None)
        if expected_trades_sha256 is not None and s.trades_sha256 != expected_trades_sha256:
            raise LedgerDivergence("seed_mismatch", {"expected": expected_trades_sha256, "got": s.trades_sha256})
        try:
            self._journal_bar(s, bh)
            self._journal_settlement(s, bh)
        except JournalConflict as ex:
            raise LedgerDivergence("seed_conflict", {"detail": str(ex)}) from ex
        self.bars, self.bars_hash, self.last = bars, bh, s
        return s

    def settle(self, bar: T.NormalizedBar, now_ms: int) -> SettleResult:
        """Recomputes the ledger through `bar` (spec §4 settle 1-3): stages
        the new bar, reruns the engine over the full staged history,
        verifies G1 (the settled prefix and the previous bar's
        hash/bars_hash/trades digest must be reproduced byte-for-byte AND
        match what the journal holds for them), journals the bar +
        settlement, and only then commits the staged state -- so any
        exception raised along the way leaves the ledger exactly as it was
        before this call and `bar` can be retried unchanged.

        `bar.ts_open` at or before the ledger's last bar is either an
        idempotent re-delivery of that exact last bar (returns `self.last`
        unchanged, no journal write -- spec §4 settle 8), a revised bar
        (`BarsDivergence` + a journaled `bars_divergence` incident), or an
        older, unrevised bar (plain `ValueError`, no incident -- not
        interesting enough to raise the ledger's own alarm over).
        Otherwise `bar` must sit exactly one script-timeframe bucket after
        the last bar and must not be forming, or `LedgerGap`.
        """
        if self.last is None:
            raise RuntimeError("seed() before settle()")
        prev = self.last
        last_bar = self.bars[-1]
        if bar.ts_open <= last_bar.ts_open:
            if bar.ts_open == last_bar.ts_open:
                diff = compare_bar(last_bar, bar)
                if not diff:
                    return self.last
                self.j.append_incident("bars_divergence", {"ts_open": bar.ts_open, "fields": diff, "now_ms": now_ms})
                raise BarsDivergence(f"revised settled bar {bar.ts_open}: {diff}")
            existing = next((b for b in reversed(self.bars) if b.ts_open == bar.ts_open), None)
            if existing is not None:
                diff = compare_bar(existing, bar)
                if diff:
                    self.j.append_incident("bars_divergence", {"ts_open": bar.ts_open, "fields": diff, "now_ms": now_ms})
                    raise BarsDivergence(f"revised settled bar {bar.ts_open}: {diff}")
            raise ValueError(f"bar {bar.ts_open} is not after the ledger's last bar")

        expected = last_bar.ts_open + tf_ms(self.spec.script_tf)
        if bar.is_forming or bar.ts_open != expected:
            raise LedgerGap(expected, bar.ts_open, forming=bar.is_forming)

        prev_bars_hash = self.bars_hash   # captured before rolling (finding 3)
        bars = self.bars + [bar]
        bh = roll_hash(self.bars_hash, bar)
        r, ms = self._run(bars)
        s = self._result(bars, r, ms, prev)

        # G1 (spec §4 settle 3): the trade prefix ending at the previous
        # bar, and that previous bar's hash/bars_hash/trades digest, must
        # be reproduced by this run AND agree with what the journal holds.
        m = prev.bar_index
        prefix = [k for k in s.keys if k.exit_bar <= m]
        if prefix != prev.keys:
            raise LedgerDivergence("g1_prefix", _prefix_mismatch_detail(m, prefix, prev.keys))
        row = self.j.settlement(self.spec.epoch_hash(), m)
        if row is None or int(row["broker_state_hash"]) != s.hashes[m]:
            raise LedgerDivergence("g1_hash", {"m": m, "journaled": row and row["broker_state_hash"], "recomputed": s.hashes[m]})
        prefix_sha = keys_sha256(prefix)
        if prefix_sha != row["trades_sha256"]:
            raise LedgerDivergence("g1_trades", {"m": m, "journaled": row["trades_sha256"], "recomputed": prefix_sha})
        if prev_bars_hash != row["bars_hash"]:
            raise LedgerDivergence("g1_bars", {"m": m, "journaled": row["bars_hash"], "recomputed": prev_bars_hash})

        try:
            self._journal_bar(s, bh)
        except JournalConflict as ex:
            self.j.append_incident("bars_divergence", {"ts_open": bar.ts_open, "conflict": str(ex)})
            raise BarsDivergence(str(ex)) from ex
        try:
            self._journal_settlement(s, bh)
        except JournalConflict as ex:
            raise LedgerDivergence("settlement_conflict", {"bar_index": s.bar_index, "detail": str(ex)}) from ex

        self.bars, self.bars_hash, self.last = bars, bh, s
        return s
