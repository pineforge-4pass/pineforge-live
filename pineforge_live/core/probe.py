"""The intrabar probe (spec §4 evaluate 1–4): the same function over bars + forming with the tail flags."""
from __future__ import annotations
import dataclasses
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from pineforge_live import types as T
from pineforge_live.engine.handle import PATH_ORDER_AUTO, PATH_ORDER_HIGH_FIRST, PATH_ORDER_LOW_FIRST
from pineforge_live.engine.report import RunResult
from .book import settled_book, dual_entry_guard, Intent
from .classify import delta_candidates
from .ids import intent_key_for

# Tolerance for "is this float delta/qty actually nonzero/different", not
# just floating-point noise from the engine's own arithmetic -- matches
# `ledger.py`'s `_result` (`position_delta` unexplained-residual check) and
# `ledger.py`'s `entry_fills` construction, which this module mirrors.
_EPS = 1e-12
# Tolerance for "do P_auto and P_other actually disagree on qty" (m4):
# looser than _EPS since a genuine qty disagreement (percent-of-equity
# sizing diverging between the two intrabar paths) is orders of magnitude
# larger than float noise; kept as its own constant so the two concerns
# don't have to share one number.
_QTY_EPS = 1e-9


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
    (`intent`/`leg`/`is_long`, mirroring `TradeKey`'s entry/exit identity)
    plus the fill `qty`/`price` and the bars it belongs to.
    `path_variant=True` marks a fill that only P_auto produced (P_other
    disagreed) and that was kept anyway because it closes a cycle (see
    `Probe.evaluate`). `qty_disagreement=True` marks a fill P_auto and
    P_other both confirmed on `(intent, leg, is_long)` but disagreed on
    `qty` (spec §4 review finding 4/m4): the fill is still emitted --
    P_auto's `qty` is kept, since P_other running at all already means the
    order is confirmed to fill on both plausible intrabar paths, only its
    *size* (typically percent-of-equity sizing reacting to a different
    intrabar PnL path) differs -- the flag lets a caller treat the qty
    itself as less certain than the fill's existence."""
    intent: str; leg: str; is_long: bool; qty: float; price: float; entry_bar: int; exit_bar: int
    path_variant: bool = False; qty_disagreement: bool = False

    @property
    def sig(self) -> tuple:
        """The identity tuple `evaluate()` diffs bar-over-bar to detect a
        retraction, and P_auto/P_other results are intersected on (spec §4
        review finding 4/m4): `(intent, leg, is_long)` -- "same order, same
        leg", per spec, NOT qty (a tick-to-tick qty drift on a
        percent-of-equity order must not read as a retract+re-fill pair;
        see `qty_disagreement`)."""
        return (self.intent, self.leg, self.is_long)


@dataclass
class ProbeResult:
    """One `Probe.evaluate()` call's outcome: the fills P_auto confirmed
    (`fills`), fills seen but held back (`deferred`), fills a PRIOR
    evaluate() reported on this bar that this one no longer confirms
    (`retracted`), ENTRY-leg fills dropped by the `created_now` ruling
    because their resolved intent was not resting in the pre-run settled
    book (`dropped`), the settled intents' resolved levels refreshed under
    the trail policy (`levels`), whether the dual-entry guard suppressed
    an entry (`guard_active`), how long the recompute took, and whether
    `P_other` had to run at all (`p_other_ran`)."""
    bar_index: int; forming: T.NormalizedBar; fills: list[ProbeFill]; deferred: list[ProbeFill]; retracted: list[ProbeFill]
    levels: dict[str, tuple[float | None, float | None, float | None]]; guard_active: bool; recompute_ms: int; p_other_ran: bool
    dropped: list[ProbeFill] = dataclasses.field(default_factory=list)


def last_bar_fills(r: RunResult, n: int) -> list[ProbeFill]:
    """The `ProbeFill`s for bar `n` of run `r` explained by its CLOSED
    trades: entries whose `entry_bar_index == n` and exits whose
    `exit_bar_index == n`. `open_at_end` rows (report-only, spec §0) are
    excluded. Does NOT cover a fill that opens or reverses into a still-open
    position, or one that merely reduces one -- those never appear as a
    closed trade at all; see `_delta_fill` for that half (spec §4 review
    finding 1 / plan defect, mirroring `ledger.py`'s `SettleResult.entry_fills`)."""
    out = []
    for t in r.trades:
        if t.open_at_end:
            continue
        if t.entry_bar_index == n:
            out.append(ProbeFill(t.entry_id, "ENTRY", t.is_long, t.qty, t.entry_price, t.entry_bar_index, t.exit_bar_index))
        if t.exit_bar_index == n:
            out.append(ProbeFill(t.exit_id, "EXIT", t.is_long, t.qty, t.exit_price, t.entry_bar_index, t.exit_bar_index))
    return out


def _departed(book: dict[str, Intent], pending_orders: list) -> list[Intent]:
    """The settled-book intents that are NO LONGER resting in a probe
    run's own pending-order mirror (m4) -- the probe's half of the ONE
    attribution rule (`classify.delta_candidates`): the ledger side reads
    the same departure out of `book_diff`'s `CANCELLED` keys, and the two
    must agree or the probe TRIGGERs an id the settlement never books.

    Keys are rebuilt from the raw mirror rows with `intent_key_for` (the
    same keying `settled_book` uses), which reads no handle accessor and
    so is safe on any run's result, not just the handle's last."""
    resting = {intent_key_for(po, int(po["created_position_cycle_seq"])).s for po in pending_orders}
    return [it for k, it in book.items() if k not in resting]


def _resolve_intent(book: dict[str, Intent], pending_orders: list, leg: str, is_long: bool) -> str:
    """The intent a position-delta fill on (`leg`, `is_long`) is attributed
    to: the settled-book order that LEFT the book on that side during this
    probe run (`classify.delta_candidates` over `_departed`), lowest mirror
    `index` only when more than one did; `"?"` when none did.

    N5 (design, v1-documented): this attributes ONE delta to ONE candidate
    -- it does not split a delta across several same-side orders that could
    each plausibly have contributed. Under `pyramiding >= 2`, two same-side
    entries filling on the SAME bar are folded into the ledger's single
    unexplained residual and read as one `_delta_fill` of the combined qty
    attributed to the lower-index departure; the other's own id is never
    surfaced. This mirrors `ledger.py`'s `SettleResult.entry_fills`, which
    makes the identical simplification (`Ledger._result`'s `unexplained`
    residual is also a single synthesized fill, never two) -- fine for the
    corpus fixtures (neither exercises same-bar multi-lot pyramiding),
    flagged here should a later probe fixture exercise it."""
    candidates = delta_candidates(_departed(book, pending_orders), leg, is_long)
    return min(candidates, key=lambda it: it.index).key.order_id if candidates else "?"


def _delta_fill(r: RunResult, prev_position_size: float, forming: T.NormalizedBar, book: dict[str, Intent], n: int) -> ProbeFill | None:
    """The ONE `ProbeFill` needed to explain run `r`'s position delta
    against the ledger's last settlement, on top of whatever
    `last_bar_fills` already reports from `r`'s closed trades (spec §4
    review finding 1, the ruled PLAN DEFECT: the engine's trade report
    lists closed trades only, so an entry that opens or adds to a
    still-open position -- or an exit that merely reduces one -- never
    shows up as a closed trade at all). Mirrors `ledger.py`'s
    `SettleResult.entry_fills` construction in `Ledger._result`
    (`position_delta`/the `unexplained` residual), including its N1 fix:
    ENTRY vs EXIT is decided by DIRECTION (does the unexplained delta move
    toward `r`'s own final position?), not by comparing magnitudes -- a
    magnitude compare misjudges a same-magnitude reversal (e.g. -1 -> +1)
    as an EXIT, silently dropping the reversal's opening half (confirmed
    against the sma fixture's bar-2001 reversal from the review).

    Returns `None` when the delta is fully explained by closed trades.

    `intent` is resolved by `_resolve_intent` -- the order that left the
    PRE-run settled `book` (the book as of the ledger's last real
    settlement) during THIS run, restricted to kind ENTRY/MARKET/RAW_ORDER
    (never EXIT: a closing/reducing intent is exposed to the venue mirror
    under one of those three kinds too -- see `book.py`'s
    `_counts_as_entry` docstring for the RAW_ORDER case), and only for an
    ENTRY-leg fill. An EXIT-leg fill (an unexplained delta that reduces,
    without closing, an open position) always reads `intent="?"` (N2,
    task-4 re-review): the departed candidate on the reducing side would
    be the SAME-SIDE resting entry -- e.g. the `Long` entry -- for a fill
    that REDUCED the long, never the order that actually did the
    reducing; no candidate in `classify.ENTRYISH_KINDS` ever IS the
    reducing order, so returning one of them would mislabel the fill
    instead of honestly admitting the id is unknown -- matching the
    ledger's own `entry_fills`, which leaves `intent=None` for the
    identical case. Practically unreachable on the corpus fixtures today
    (the engine books every reduction as a closed trade, so this branch
    never fires), but kept honest for whichever engine/script combination
    first exercises it."""
    delta = r.position_size - prev_position_size
    sign = lambda t: t.qty if t.is_long else -t.qty
    closed = [t for t in r.trades if not t.open_at_end]
    explained = sum(sign(t) for t in closed if t.entry_bar_index == n) - sum(sign(t) for t in closed if t.exit_bar_index == n)
    unexplained = delta - explained
    if abs(unexplained) <= _EPS:
        return None
    pos = r.position_size
    leg = "ENTRY" if pos != 0.0 and (unexplained > 0) == (pos > 0) else "EXIT"
    is_long = (unexplained > 0) if leg == "ENTRY" else (prev_position_size > 0)
    price = r.position_avg_price if leg == "ENTRY" else forming.c
    intent = _resolve_intent(book, r.pending_orders, leg, is_long) if leg == "ENTRY" else "?"
    return ProbeFill(intent, leg, is_long, abs(unexplained), price, n, -1)


def _keyed_by_sig_ordinal(fills: list[ProbeFill]) -> dict[tuple, ProbeFill]:
    """Keys `fills` by `(sig, ordinal)`, `ordinal` the 0-based occurrence
    count of that `sig` within `fills`, in list order (N3, task-4
    re-review). Plain `sig` alone collapses two fills that share
    `(intent, leg, is_long)` -- e.g. two pyramided legs closed by ONE
    shared exit id on the same bar -- onto a single dict slot, silently
    dropping one fill's identity and (in the auto/other matching this
    feeds) pairing the survivor against an arbitrary counterpart instead
    of its actual match. Ordinal position isn't a guaranteed 1:1
    correspondence across two intrabar paths or two ticks, but both sides
    preserve the engine's own trade-report order, so pairing by position
    is deterministic and strictly better than "last write wins"."""
    counts: dict[tuple, int] = defaultdict(int)
    out: dict[tuple, ProbeFill] = {}
    for f in fills:
        key = (f.sig, counts[f.sig]); counts[f.sig] += 1
        out[key] = f
    return out


def _drop_unresting_entries(fills: list[ProbeFill], resting_ids: set[str]) -> tuple[list[ProbeFill], list[ProbeFill]]:
    """The `created_now` ruling (spec §4 review, replacing the old dead
    `created_bar == n` exclusion): drop, and report separately, any
    ENTRY-leg fill whose `intent` id is not resting in the PRE-run settled
    book (`"?"` included) -- an entry fill can only legitimately come from
    an order that was already resting before this bar; one that was not is
    either a stale/short-circuited read or (per the ruling's engine-facts
    analysis) an order created on the forming bar itself, which
    `set_probe_suppress_tail_logic` makes unreachable under the probe
    (kept as defense in depth regardless). EXIT legs are exempt: the
    engine synthesises some closes (margin call / max-intraday-loss) with
    no order id at all, and a reversal's close carries the OPPOSING
    entry's id -- neither is "not resting" in any meaningful sense this
    check should reject.

    Returns `(kept, dropped)`."""
    kept, dropped = [], []
    for f in fills:
        if f.leg == "ENTRY" and f.intent not in resting_ids:
            dropped.append(f)
        else:
            kept.append(f)
    return kept, dropped


class Probe:
    """The intrabar probe: recomputes a full backtest over the ledger's
    settled bars plus the currently-forming one, at each tick, to see
    whether any order would fill before the bar actually settles (spec §4
    evaluate 1–4)."""

    def __init__(self, handle, spec, ledger, trail_refresh_policy: str = "bar_open_level"):
        self.h, self.spec, self.L, self.policy = handle, spec, ledger, trail_refresh_policy
        self.prev_fills: dict[int, dict[tuple, ProbeFill]] = defaultdict(dict)   # bar_index -> {(sig, ordinal): last-confirmed ProbeFill}
        self.retracted_history: dict[int, list[ProbeFill]] = defaultdict(list)

    def _run(self, bars, path_order: int) -> RunResult:
        return self.h.run_full(bars, self.spec.script_tf,
                               per_run=[("set_probe_suppress_tail_logic", (True,)), ("set_path_order", (path_order,))])

    def _prune_histories(self, n: int) -> None:
        """N7: `prev_fills`/`retracted_history` are bounded to the current
        bar -- only `n` is ever read back, so entries for bars < n are
        dropped as soon as a new bar starts being probed, instead of
        growing without bound for the life of the process."""
        for k in [k for k in self.prev_fills if k < n]:
            del self.prev_fills[k]
        for k in [k for k in self.retracted_history if k < n]:
            del self.retracted_history[k]

    def _journal(self, journal, forming: T.NormalizedBar, now_ms: int, outcome: str, ms: int) -> None:
        if journal is not None:
            journal.append_evaluation({"epoch_hash": self.spec.epoch_hash(), "trigger": "evaluate", "tick_seq_from": None, "tick_seq_to": None,
                                       "forming_json": json.dumps(forming.ohlcv()), "outcome": outcome, "recompute_ms": ms,
                                       "created_ms": now_ms})   # N8: the tick's own now_ms, not just the insert-time default

    def _journal_drops(self, journal, n: int, now_ms: int, dropped: list[ProbeFill]) -> None:
        """N11 (task-4 `created_now` PARTIAL carry, task-6 review finding
        9): one `probe_dropped_entry` incident per ENTRY-leg fill
        `_drop_unresting_entries` dropped this call -- an entry fill whose
        intent wasn't resting in the pre-run settled book is a genuine
        anomaly (a stale/short-circuited read, or an order the probe
        should never have been able to see at all; see that function's
        docstring), worth an audit trail even though it never reaches
        `fills`/`deferred`. No-op when no journal is given (matches every
        other `append_*` call site in this module)."""
        if journal is None:
            return
        for f in dropped:
            journal.append_incident("probe_dropped_entry", {"bar_index": n, "intent": f.intent, "leg": f.leg,
                                                             "is_long": f.is_long, "qty": f.qty, "price": f.price, "now_ms": now_ms})

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
        self._prune_histories(n)
        # N10: pass the bar objects straight through -- run_full() accepts
        # anything exposing .ohlcv() (Task 0 prelim) -- instead of rebuilding
        # an OHLCV-tuple list from self.L.bars on every evaluate() call;
        # self.L.bars is itself a fresh list only when the ledger settles
        # (once per bar), not once per tick.
        bars = self.L.bars + [forming]
        # Use the book captured AT SETTLE TIME (SettleResult.book, ledger
        # fix-2 / the Task 6 prelim ruling) instead of re-reading
        # settled_book(self.h, self.L.last) here: the handle's
        # effective_levels/level_resolved accessors describe only its LAST
        # run (see settled_book's own docstring), and by the time a SECOND
        # evaluate() call on the same bar reaches this line, self.h's last
        # run is already a PRIOR evaluate() call's own probe run, not the
        # settlement that produced self.L.last -- a re-read here would
        # silently resolve stale/wrong-run levels (confirmed live on the
        # bracket fixture's bar 2005: a previously-valid mirror index reads
        # a DIFFERENT order's real stop/limit after just one probe run).
        # self.L.last.book was captured by Ledger._settled_book immediately
        # after the settlement's own run_full(), so it stays correct no
        # matter how many probe runs have happened on the handle since.
        book: dict[str, Intent] = self.L.last.book
        resting_ids = {it.key.order_id for it in book.values()}
        # m9: the book half of the guard is all that is knowable before the
        # run; the dual-entry-path half is P_auto's own report and is
        # folded in below. An aborted run has no report to fold, so its
        # `ProbeResult` carries the book half alone -- it emits no fills
        # either way.
        guard = dual_entry_guard(book, self.L.last.position_size)

        p_auto = self._run(bars, PATH_ORDER_AUTO)
        if p_auto.status != 0:
            ms = int((time.perf_counter() - t0) * 1000)
            self._journal(journal, forming, now_ms, "aborted", ms)
            return ProbeResult(n, forming, [], [], [], {}, guard, ms, False, [])

        guard = dual_entry_guard(book, self.L.last.position_size, p_auto.last_bar_dual_entry_path)

        # M2 fix: capture the intrabar_best refresh's book IMMEDIATELY after
        # P_auto -- while the handle's last run is still P_auto's, not
        # P_other's (which may run below) -- keyed the same way
        # settled_book always keys (the mirror's own created cycle seq).
        probe_book: dict[str, Intent] | None = None
        if self.policy == "intrabar_best":
            probe_book = settled_book(self.h, dataclasses.replace(self.L.last, pending_orders=p_auto.pending_orders, cycle_seq=p_auto.position_cycle_seq))

        # M1 fix: join last_bar_fills' closed-trade fills with the
        # position-delta fill (opens/adds/reversal-opens/partial-reduces
        # that never appear as a closed trade), then apply the created_now
        # ruling to both before anything downstream sees them.
        d_auto = _delta_fill(p_auto, self.L.last.position_size, forming, book, n)
        auto_all = last_bar_fills(p_auto, n) + ([d_auto] if d_auto is not None else [])
        auto_fills, dropped = _drop_unresting_entries(auto_all, resting_ids)
        # N11: journal here, once, immediately after `dropped` is computed
        # -- so it fires on every return path below (a clean result, a
        # P_other abort, or the guard-suppressed path), not just the
        # common case.
        self._journal_drops(journal, n, now_ms, dropped)

        fills, deferred, other_ran = [], [], False
        if auto_fills:
            p_other = self._run(bars, path_order_other(forming)); other_ran = True
            if p_other.status != 0:
                # M3 fix: a P_other abort is an ABORT, not "P_other disagrees
                # with everything" -- journal it and return an empty result
                # (as the P_auto abort path does above), never emit on a
                # half-run, and never touch prev_fills/retracted_history.
                ms = int((time.perf_counter() - t0) * 1000)
                self._journal(journal, forming, now_ms, "aborted", ms)
                # N4 (re-review): still report P_auto's own drops on an
                # abort -- they were already computed above and the caller
                # otherwise loses that signal, even though no fill/deferral
                # is confirmed on a half-run.
                return ProbeResult(n, forming, [], [], [], {}, guard, ms, True, dropped)
            d_other = _delta_fill(p_other, self.L.last.position_size, forming, book, n)
            other_all = last_bar_fills(p_other, n) + ([d_other] if d_other is not None else [])
            other_kept, _ = _drop_unresting_entries(other_all, resting_ids)
            # N3 (re-review): key both sides by (sig, ordinal), not sig
            # alone -- see _keyed_by_sig_ordinal's docstring.
            other_by_sig = _keyed_by_sig_ordinal(other_kept)
            auto_ordinals: dict[tuple, int] = defaultdict(int)
            for f in auto_fills:
                auto_key = (f.sig, auto_ordinals[f.sig]); auto_ordinals[f.sig] += 1
                match = other_by_sig.get(auto_key)
                if match is not None:
                    # m4: intersect on (intent, leg, is_long); qty is data --
                    # a disagreement is flagged, not treated as a mismatch.
                    if abs(match.qty - f.qty) > _QTY_EPS:
                        f = dataclasses.replace(f, qty_disagreement=True)
                    fills.append(f)
                else:
                    # path-variant: emitted only when it closes the same cycle (EXIT), deferred when it changes net position
                    (fills if f.leg == "EXIT" else deferred).append(dataclasses.replace(f, path_variant=True))
        if guard:
            deferred += [f for f in fills if f.leg == "ENTRY"]; fills = [f for f in fills if f.leg != "ENTRY"]

        # N7 (cont'd): prev_fills now stores the full ProbeFill per sig (not
        # a bare sig set) so a retraction can carry the original fill's
        # price/bars forward instead of a synthesized NaN-priced stand-in.
        # N3 (cont'd): keyed by (sig, ordinal), same reasoning as the
        # auto/other match above -- two same-sig fills on this bar must not
        # collapse onto one slot here either.
        cur_map = _keyed_by_sig_ordinal(fills)
        prev_map = self.prev_fills[n]
        retracted = [prev_map[s] for s in (prev_map.keys() - cur_map.keys())]
        self.prev_fills[n] = cur_map
        self.retracted_history[n] += retracted

        # level refresh for settled intents (spec §4 evaluate 3)
        levels: dict[str, tuple] = {}
        if self.policy == "intrabar_best" and probe_book is not None:
            for k, it in book.items():
                pit = probe_book.get(k); levels[k] = (pit.stop, pit.limit, pit.activation) if pit else (it.stop, it.limit, it.activation)
        else:
            for k, it in book.items():
                levels[k] = (it.stop, it.limit, it.activation)
        ms = int((time.perf_counter() - t0) * 1000)
        self._journal(journal, forming, now_ms, "ran", ms)
        return ProbeResult(n, forming, fills, deferred, retracted, levels, guard, ms, other_ran, dropped)
