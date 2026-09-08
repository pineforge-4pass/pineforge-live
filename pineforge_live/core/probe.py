"""The intrabar probe (spec §4 evaluate 1–4): the same function over bars + forming with the tail flags."""
from __future__ import annotations
import dataclasses
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pineforge_live import types as T
from pineforge_live.engine.handle import PATH_ORDER_AUTO, PATH_ORDER_HIGH_FIRST, PATH_ORDER_LOW_FIRST
from pineforge_live.engine.report import RunResult
from .book import settled_book, dual_entry_guard, Intent


def path_order_other(forming: T.NormalizedBar) -> int:
    """The path order OPPOSITE the engine's own AUTO decision for `forming`
    (spec §0: AUTO picks high-first when the open is closer to the high
    than to the low). `P_other` is run with this to see whether an
    intrabar fill survives the other of the two plausible tick paths."""
    high_first = abs(forming.h - forming.o) < abs(forming.o - forming.l)   # engine rule (spec §0)
    return PATH_ORDER_LOW_FIRST if high_first else PATH_ORDER_HIGH_FIRST


@dataclass(frozen=True)
class ProbeFill:
    """One order's fill as seen by the probe: `key`-equivalent identity
    (`intent`/`leg`/`is_long`/`qty`, mirroring `TradeKey`'s entry/exit
    identity) plus the fill `price` and the bars it belongs to.
    `path_variant=True` marks a fill that only P_auto produced (P_other
    disagreed) and that was kept anyway because it closes a cycle (see
    `Probe.evaluate`)."""
    intent: str; leg: str; is_long: bool; qty: float; price: float; entry_bar: int; exit_bar: int; path_variant: bool = False

    @property
    def sig(self) -> tuple:
        """The identity tuple `evaluate()` diffs bar-over-bar to detect a retraction."""
        return (self.intent, self.leg, self.is_long, self.qty)


@dataclass
class ProbeResult:
    """One `Probe.evaluate()` call's outcome: the fills P_auto confirmed
    (`fills`), fills seen but held back (`deferred`), fills a PRIOR
    evaluate() reported on this bar that this one no longer confirms
    (`retracted`), the settled intents' resolved levels refreshed under the
    trail policy (`levels`), whether the dual-entry guard suppressed an
    entry (`guard_active`), how long the recompute took, and whether
    `P_other` had to run at all (`p_other_ran`)."""
    bar_index: int; forming: T.NormalizedBar; fills: list[ProbeFill]; deferred: list[ProbeFill]; retracted: list[ProbeFill]
    levels: dict[str, tuple[float | None, float | None, float | None]]; guard_active: bool; recompute_ms: int; p_other_ran: bool


def last_bar_fills(r: RunResult, n: int) -> list[ProbeFill]:
    """The `ProbeFill`s for bar `n` of run `r`: entries whose
    `entry_bar_index == n` and exits whose `exit_bar_index == n`.
    `open_at_end` rows (report-only, spec §0) are excluded."""
    out = []
    for t in r.trades:
        if t.open_at_end:
            continue
        if t.entry_bar_index == n:
            out.append(ProbeFill(t.entry_id, "ENTRY", t.is_long, t.qty, t.entry_price, t.entry_bar_index, t.exit_bar_index))
        if t.exit_bar_index == n:
            out.append(ProbeFill(t.exit_id, "EXIT", t.is_long, t.qty, t.exit_price, t.entry_bar_index, t.exit_bar_index))
    return out


class Probe:
    """The intrabar probe: recomputes a full backtest over the ledger's
    settled bars plus the currently-forming one, at each tick, to see
    whether any order would fill before the bar actually settles (spec §4
    evaluate 1–4)."""

    def __init__(self, handle, spec, ledger, trail_refresh_policy: str = "bar_open_level"):
        self.h, self.spec, self.L, self.policy = handle, spec, ledger, trail_refresh_policy
        self.prev_fills: dict[int, set[tuple]] = defaultdict(set)      # bar_index -> sigs seen by the previous evaluate
        self.retracted_history: dict[int, list[ProbeFill]] = defaultdict(list)

    def _run(self, bars, path_order: int) -> RunResult:
        return self.h.run_full(bars, self.spec.script_tf,
                               per_run=[("set_probe_suppress_tail_logic", (True,)), ("set_path_order", (path_order,))])

    def evaluate(self, forming: T.NormalizedBar, now_ms: int, journal=None) -> ProbeResult:
        """Recompute through `forming` (the bar currently building) and
        return what would fill if it settled right now. Runs `P_auto`
        (the engine's own path-order choice) first; if it fills anything
        new, also runs `P_other` (the opposite path order) to see whether
        the fill survives both plausible intrabar paths -- a fill only
        `P_auto` produces is kept (`path_variant=True`) when it closes a
        cycle (EXIT), and deferred otherwise (ENTRY, since that would
        change net position on unconfirmed information)."""
        if self.L.last is None:
            raise RuntimeError("seed the ledger first")
        n = self.L.n; t0 = time.perf_counter()
        bars = [b.ohlcv() for b in self.L.bars] + [forming.ohlcv()]
        book: dict[str, Intent] = settled_book(self.h, self.L.last)          # settled book from the LAST settlement's strategy is stale after a probe run:
        # settled_book reads the handle's live strategy, so capture it BEFORE the probe run (the ledger's run was the last run).
        guard = dual_entry_guard(book, self.L.last.position_size)
        p_auto = self._run(bars, PATH_ORDER_AUTO)
        if p_auto.status != 0:
            if journal is not None:
                journal.append_evaluation({"epoch_hash": self.spec.epoch_hash(), "trigger": "evaluate", "tick_seq_from": None, "tick_seq_to": None,
                                           "forming_json": json.dumps(forming.ohlcv()), "outcome": "aborted", "recompute_ms": int((time.perf_counter() - t0) * 1000)})
            return ProbeResult(n, forming, [], [], [], {}, guard, int((time.perf_counter() - t0) * 1000), False)
        auto_fills = last_bar_fills(p_auto, n)
        created_now = {it.key.order_id for it in book.values() if it.created_bar == n}
        auto_fills = [f for f in auto_fills if f.intent not in created_now]
        fills, deferred, other_ran = [], [], False
        if auto_fills:
            p_other = self._run(bars, path_order_other(forming)); other_ran = True
            if p_other.status != 0:
                other_sigs = set()
            else:
                other_sigs = {f.sig for f in last_bar_fills(p_other, n)}
            for f in auto_fills:
                if f.sig in other_sigs:
                    fills.append(f)
                else:
                    # path-variant: emitted only when it closes the same cycle (EXIT), deferred when it changes net position
                    (fills if f.leg == "EXIT" else deferred).append(dataclasses.replace(f, path_variant=True))
        if guard:
            deferred += [f for f in fills if f.leg == "ENTRY"]; fills = [f for f in fills if f.leg != "ENTRY"]
        sigs = {f.sig for f in fills}
        retracted = [ProbeFill(*s[:2], s[2], s[3], math.nan, n, -1) for s in (self.prev_fills[n] - sigs)]
        self.prev_fills[n] = sigs; self.retracted_history[n] += retracted
        # level refresh for settled intents (spec §4 evaluate 3)
        levels: dict[str, tuple] = {}
        if self.policy == "intrabar_best":
            probe_book = settled_book(self.h, dataclasses.replace(self.L.last, pending_orders=p_auto.pending_orders, cycle_seq=p_auto.position_cycle_seq))
            for k, it in book.items():
                pit = probe_book.get(k); levels[k] = (pit.stop, pit.limit, pit.activation) if pit else (it.stop, it.limit, it.activation)
        else:
            for k, it in book.items():
                levels[k] = (it.stop, it.limit, it.activation)
        ms = int((time.perf_counter() - t0) * 1000)
        if journal is not None:
            journal.append_evaluation({"epoch_hash": self.spec.epoch_hash(), "trigger": "evaluate", "tick_seq_from": None, "tick_seq_to": None,
                                       "forming_json": json.dumps(forming.ohlcv()), "outcome": "ran", "recompute_ms": ms})
        return ProbeResult(n, forming, fills, deferred, retracted, levels, guard, ms, other_ran)
