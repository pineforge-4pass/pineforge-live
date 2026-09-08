"""`LiveCore`: the venue-neutral facade Plan B3's drivers (and the L1
harness) call -- spec §4 settle 1-8 and evaluate 1-4, §5.4's reconciler and
§5.5's STOP/RiskGuard, composed over Tasks 2-7.

Nothing here talks to a venue, a clock or a socket: `settle()` and
`evaluate()` are called WITH the bar/tick and the venue's own reported
fills/positions, and hand back `ActionRequest`s the execution layer turns
into orders. That keeps the whole core testable against a tape (the L1
harness in `tests/test_live_core.py`) and keeps every venue name out of it.

Engine-handle contract: `Ledger.settle()` captures its own settled book
inside the settle window (`SettleResult.book`) because
`handle.effective_levels`/`level_resolved`/`probe_fill_qty` describe the
handle's LAST `run_full()` only. `LiveCore.settle()` therefore reads
`s.book` rather than calling `settled_book()` again, and makes its
`probe_fill_qty` reads while the settle's own run is still the handle's
last one -- before any `evaluate()` probe run can invalidate them.
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass, field
from pineforge_live import types as T
from pineforge_live.epoch import RuntimeConfig
from .book import Intent, IntentState, book_diff
from .classify import ClassifiedFill, FillClass, VenueFill, _side_for_leg, classify_bar, emulated_from_settle
from .ledger import BarsDivergence, Ledger, LedgerDivergence, RecomputeAborted, SettleResult
from .probe import Probe, ProbeResult
from .reconcile import DeadBand, ReconcileConfig, ReconcileDecision, ReconcileInput, reconcile
from .riskguard import Breaker, BreakerTable, RateWindow, RiskGuard, RiskLimits, RiskViolation, StopController

DAY_MS = 86_400_000


@dataclass(frozen=True)
class ActionRequest:
    """One order the execution layer (Plan B3) should place.

    `intent` is the PINE ORDER ID (`Intent.key.order_id` /
    `EmulatedFill.intent` / `ProbeFill.intent`), never an `IntentKey.s`:
    it is the identity `classify.classify_bar` matches a venue fill back
    against (`VenueFill.intent`), so the round trip only closes if the
    same id space is used on the way out. The intent KEY for a book op
    (which cycle's order to cancel/replace) is in `CoreOutput.book` /
    `CoreOutput.book_diff`, keyed by `IntentKey.s`.

    `qty` is always positive; `side` carries the direction.
    `price_hint` is the probe's own fill price for a TRIGGER and `None`
    for a MARKET_AT_OPEN (spec §4 settle 6: "no price fallback" -- the
    open is the price, and B3 waits `open_wait_ms` for it).
    `target_bar_index` is the bar the action is journaled against, which
    is what a venue fill's `target_bar_index` must equal to be
    identity-matched at that bar's settlement.

    SUPERSEDE CONTRACT: a later request for the same `(intent, leg,
    target_bar_index)` REPLACES the earlier one -- the executor submits
    (or amends to) the last one and the venue must fill it once, not
    once per request. The one producer of such a pair is spec §4 settle
    6's MARKET leg: `settle(n)` asks for it in advance, priced off the
    only price it has (bar n's close), and the first `evaluate()` of bar
    n+1 re-quotes it with the engine's qty at the OPEN
    (`reason="open_requote"`), which is the qty the spec actually
    specifies. `leg` here is `"EXIT"` when `reduce_only` else `"ENTRY"`,
    the same pairing `classify.classify_bar` matches venue fills on.
    """
    kind: str; intent: str | None; side: T.Side; qty: float; price_hint: float | None
    reduce_only: bool; cls: str; reason: str; target_bar_index: int


@dataclass
class CoreOutput:
    """One `seed()`/`settle()`/`evaluate()` call's whole outcome.

    `classified` is this settlement's `ClassifiedFill`s (empty for
    `seed`/`evaluate`) -- the reconciler's own input, surfaced so B3 can
    journal the fill classification and so a test can assert on it
    without re-deriving it. `incidents` are the dicts LiveCore ALSO
    journaled via `Journal.append_incident` (they are a return value for
    the caller's convenience, not the durable record). `stop` is the
    `(level, disposition, cause)` this call raised (or attempted to raise
    -- `StopController.raise_stop` is monotonic, so a weaker escalation
    while already STOPped is recorded here but does not lower the
    controller)."""
    settle: SettleResult | None = None
    probe: ProbeResult | None = None
    actions: list[ActionRequest] = field(default_factory=list)
    stop: tuple | None = None
    incidents: list[dict] = field(default_factory=list)
    reconcile: ReconcileDecision | None = None
    book: dict[str, Intent] = field(default_factory=dict)
    book_diff: dict[str, IntentState] = field(default_factory=dict)
    classified: list[ClassifiedFill] = field(default_factory=list)


def signed_qty(venue_fills: list[VenueFill]) -> float:
    """The signed qty of the fills WE are responsible for in one batch:
    `+` for BUY, `-` for SELL, `FillCause.OURS` only (a liquidation/ADL/
    manual fill is the venue's own action, never part of our basis).

    Public because a driver (and `scripts/l1_harness.py`'s perfect venue)
    has to move its account position by exactly the quantity `settle()`
    derives its basis from -- two spellings of "the signed qty of our
    fills" is precisely the drift `real_position` exists to detect."""
    return sum((v.qty if v.side is T.Side.BUY else -v.qty) for v in venue_fills if v.cause is T.FillCause.OURS)


#: Pre-rename spelling, kept so an in-flight caller (or a monkeypatching
#: test) that reached for the private name still resolves.
_signed = signed_qty


class LiveCore:
    """Composes the ledger, probe, classifier, reconciler, RiskGuard and
    STOP controller into the two entry points a driver calls.

    Cadence: `seed(history)` once, then per script-TF bar
    `evaluate(forming, now)` on every coalesced tick and `settle(bar,
    ...)` on the confirmed kline. The per-bar RiskGuard budget is reset by
    `settle()` only -- `evaluate()` spends from the budget the preceding
    settlement opened, so a bar's whole tick stream shares one
    `max_fill_actions_per_bar` allowance (spec §4's "one TRIGGER per
    intent per cycle; `max_fill_actions_per_bar` ≤ P's fill count").
    """

    def __init__(self, handle, spec, journal, marker, runtime_config: RuntimeConfig, limits: RiskLimits,
                 dead_band: DeadBand, breakers: list[Breaker], trail_refresh_policy: str = "bar_open_level",
                 reconcile_cfg: ReconcileConfig | None = None):
        self.h, self.spec, self.j, self.marker, self.rc = handle, spec, journal, marker, runtime_config
        self.ledger = Ledger(handle, spec, journal, runtime_config.hash())
        self.probe = Probe(handle, spec, self.ledger, trail_refresh_policy)
        self.limits, self.dead_band = limits, dead_band
        self.guard = RiskGuard(limits, dead_band)
        self.stop = StopController(journal, marker, limits.hard_stop_max_hold_ms)
        self.stop.restore()
        # G3 (spec §1): self-tested at construction so a breaker that could
        # never fire (UB_95(0, n_min) >= theta, or window_n < n_min) is a
        # startup error, not a silent no-op. Each breaker owns a RateWindow
        # keyed by its own name; `settle()` observes one sample per bar --
        # "did the reconciler's counter of that name fire this bar".
        self.g3 = BreakerTable(breakers); self.g3.self_test()
        self.g3_windows: dict[str, RateWindow] = {b.name: RateWindow(b.window_n) for b in breakers}
        self.rcfg = reconcile_cfg or ReconcileConfig(1, 30.0, limits.max_order_notional, 3, False)
        self.book: dict[str, Intent] = {}
        self.mirror_early_today = 0
        self._mirror_early_day: int | None = None
        # spec §4 settle 6: the MARKET legs this settlement asked for at the
        # NEXT bar's open, keyed by (intent id, leg). `evaluate()` on that
        # bar suppresses its own TRIGGER for the same (intent, leg) -- they
        # are the same fill, and `[r4]` allows one open action per intent.
        self.pending_market: dict[tuple[str, str], ActionRequest] = {}
        # (intent id, leg) already TRIGGERed on the bar being evaluated --
        # the probe re-reports a confirmed fill on every tick of the bar.
        self._triggered_bar: int | None = None
        self._triggered: set[tuple[str, str]] = set()
        self._our_fills = 0.0
        self.missed_since: dict[tuple[str, str], int] = {}
        journal.append_epoch(spec.epoch_hash(), "{}")
        journal.append_runtime_config(runtime_config.hash(), "{}")

    # --- state a caller may read -------------------------------------------------
    @property
    def our_signed_fills(self) -> float:
        """The v1 fallback basis (spec §5.4's PRIMARY comparison): the
        cumulative signed qty of the fills WE are responsible for, `+` for
        BUY and `-` for SELL, over every `FillCause.OURS` venue fill
        `settle()` has been handed since the ledger was last flat.

        Anchored at `seed()` on the ledger position the seed adopted: a
        campaign that starts mid-position (every corpus fixture does --
        the always-in-market reversal scripts are never flat) has no
        "since flat" boundary to count from, and starting at 0 would make
        the basis disagree with both the ledger and the account by the
        adopted size for the life of the process. Reset to 0.0 whenever a
        settlement leaves the ledger flat AND this accumulator agrees the
        venue is flat too (within the dead-band), which re-establishes the
        exact per-cycle meaning the moment both sides do go flat. A ledger
        that settles flat over an uncorrected venue residual (a MISSED EXIT
        the same settlement is correcting) keeps carrying it: resetting
        there would read the correction's own fill, one bar later, as a
        position out of nowhere.

        Plan B3 passes the exchange-derived value to
        `settle(..., our_signed_fills=...)` instead; an explicit value is
        used for that call only and never mutates this accumulator (the
        two are different estimates of the same quantity, and mixing them
        would corrupt the fallback for whichever call comes next)."""
        return self._our_fills

    # --- helpers -----------------------------------------------------------------
    def _incident(self, out: CoreOutput, kind: str, **detail) -> None:
        """Journals an incident AND reports it on `out.incidents` -- the
        journal row is the durable record, the returned dict is what a
        driver/test reads without querying sqlite."""
        row = {"kind": kind, **detail}
        out.incidents.append(row)
        self.j.append_incident(kind, detail)

    def _raise(self, out: CoreOutput, level: T.StopLevel, disp: T.StopDisposition, cause: str) -> None:
        self.stop.raise_stop(level, disp, cause)
        out.stop = (level, disp, cause)

    def _permitted(self, out: CoreOutput, a: ActionRequest, position: float, price: float) -> bool:
        """The two gates every request passes before it reaches
        `out.actions` (ruling 4 / spec §5.5): the STOP controller's own
        `permits()`, then the RiskGuard's `check_*` budgets. A refusal is
        an incident (`action_refused_by_stop` / `risk_refused`), never a
        silent drop -- an order the core wanted and did not send is
        exactly the thing an operator must be able to find afterwards.

        `action_kind` mapping: a cancel is always permitted; a FLATTEN is
        the `hard_flat` reduce-only pass (allowed under `HARD` only with
        disposition `FLATTEN`); everything else is `entry` when it
        increases exposure and `reduce` when it does not. A SYNTHETIC
        close is deliberately a plain `reduce`, NOT `static_exit`: under
        `HARD` the spec allows only the dead-man and re-established
        protective exits, and an engine-side margin-call close is neither.
        """
        kind = "cancel" if a.kind == "CANCEL_STALE_CYCLE" else ("hard_flat" if a.kind == "FLATTEN" else
                                                                ("entry" if not a.reduce_only else "reduce"))
        if not self.stop.permits(kind, increases_exposure=not a.reduce_only, reduce_only=a.reduce_only):
            self._incident(out, "action_refused_by_stop", action=a.kind, intent=a.intent, qty=a.qty,
                           level=self.stop.level.value, disposition=self.stop.disposition.value)
            return False
        if a.qty > 0.0:
            cause = self.guard.check_order_notional(a.qty, price)
            if cause is None and not a.reduce_only:
                # Conservative bound: an exposure-increasing action can at
                # most add its whole qty to the side already held.
                cause = self.guard.check_position(abs(position) + a.qty, price)
            if cause is not None:
                self._incident(out, "risk_refused", action=a.kind, intent=a.intent, qty=a.qty, cause=cause)
                return False
        return True

    def _market_legs(self, out: CoreOutput, s: SettleResult, price: float) -> list[ActionRequest]:
        """spec §4 settle 6: every MARKET order resting in the settled book
        fills at the NEXT bar's open, with the ENGINE's own qty
        (`strategy_pending_order_fill_qty`) -- the runtime never
        re-implements the sizing rules.

        That accessor answers "the contracts the entry kernel would OPEN",
        which is NOT the whole venue transaction when the order reverses a
        live position: its doxygen names the engine's own branch
        `close_opposite_then_enter` and tells a consumer to compare the
        returned qty with the live position. So an order whose side
        opposes the settled position is submitted as TWO legs -- a
        reduce-only close of the live position, then the opened qty --
        which is also exactly how the ledger reports it back at the next
        settlement (a closed-trade EXIT leg plus a position-delta ENTRY
        leg), so both halves have a venue counterpart to match against.
        `close_only=1` means the kernel opens nothing: only the close leg
        is submitted, bounded by the live position.

        Read here, inside the settle window, because `probe_fill_qty`
        describes the handle's LAST run -- which is this settlement's own
        recompute until the next `evaluate()` probe runs.
        """
        legs: list[ActionRequest] = []
        pos = s.position_size
        band = self.dead_band.qty(price)
        for it in self.book.values():
            if not it.is_market:
                continue
            rc, qty, close_only, partition = self.h.probe_fill_qty(it.index, price)
            if rc != 0 or math.isnan(qty):
                self._incident(out, "market_qty_unavailable", intent=it.key.order_id, index=it.index, rc=rc)
                continue
            opposite = pos != 0.0 and it.is_long != (pos > 0)
            if close_only:
                # `close_only=1` is the kernel reporting that it opens
                # NOTHING: for the two MARKET reversal kernels `qty` is then
                # the UNOPENED REMAINDER (<= kQtyEpsilon, engine_fills.cpp
                # `close_opposite_then_enter`), never the size it closes --
                # the ABI exposes no closed qty at all, and the kernel closes
                # `min(tx, live)`, i.e. the whole live position for a full
                # transaction. Sizing this leg `min(|qty|, |pos|)` therefore
                # produced a sub-dead-band leg that was dropped, the venue
                # never closed, and the ledger's own close read MISSED a bar
                # later. A partial transaction surfaces as QTY_DIVERGENT at
                # the next settlement, which the reconciler trims.
                close_qty, open_qty = (abs(pos) if opposite else 0.0), 0.0
            else:
                close_qty, open_qty = (abs(pos) if opposite else 0.0), qty
            side = T.Side.BUY if it.is_long else T.Side.SELL
            reason = f"settled MARKET order (partition {partition}, close_only {int(bool(close_only))})"
            if close_qty > band:
                legs.append(ActionRequest("MARKET_AT_OPEN", it.key.order_id, side, close_qty, None, True,
                                          "MARKET", reason, s.bar_index + 1))
            if open_qty > band:
                legs.append(ActionRequest("MARKET_AT_OPEN", it.key.order_id, side, open_qty, None, False,
                                          "MARKET", reason, s.bar_index + 1))
        return legs

    def _missed_bounds(self, classified: list[ClassifiedFill], bar_index: int, price: float) -> tuple[int, float]:
        """The `(missed_age_bars, missed_distance_bps)` pair the reconciler
        bounds a MISSED correction with (spec `[r4]`).

        Age is tracked ACROSS settlements per `(intent, leg)`: the first
        bar that pair read MISSED is remembered, its age is
        `bar_index - that bar`, and the entry is dropped the moment the
        pair stops being MISSED (it filled, went IN_FLIGHT, or was
        corrected) so a later recurrence starts its own clock. The value
        passed is the MAXIMUM age over the currently-MISSED pairs (0 when
        none), i.e. the oldest thing still missing -- the reconciler
        itself takes `max(this, bar_index - fill.bar_index)`, so passing
        the oldest can only make the bound stricter, never laxer.

        Distance is measured against the SAME fill the reconciler will
        pick as its representative -- the first MISSED ENTRY if there is
        one, else the first MISSED EXIT -- rather than an arbitrary fill,
        so the bps figure describes the correction actually under
        consideration.

        Note that a non-zero age requires the SAME `(intent, leg)` to read
        MISSED on CONSECUTIVE settlements: `emulated_from_settle` only ever
        emits bar-n fills, so a single uncorrected MISSED does not keep
        ageing by itself -- readers of ruling 2 otherwise expect a growth
        the cadence cannot produce."""
        missed = [c for c in classified if c.cls == FillClass.MISSED and c.emulated is not None]
        keys = {(c.emulated.intent, c.emulated.leg) for c in missed}
        for k in [k for k in self.missed_since if k not in keys]:
            del self.missed_since[k]
        for k in keys:
            self.missed_since.setdefault(k, bar_index)
        age = max((bar_index - first for first in self.missed_since.values()), default=0)
        rep = next((c for c in missed if c.emulated.leg == "ENTRY"), None) or (missed[0] if missed else None)
        dist = abs(price - rep.emulated.price) / price * 1e4 if rep is not None and price > 0 else 0.0
        return age, dist

    def _observe_breakers(self, counters: dict[str, int]) -> list[tuple[str, bool, bool]]:
        """One G3 sample per settlement per breaker: a breaker's `name` IS
        the reconciler counter it watches, so the sample is "did that
        counter fire this bar". Returns `(name, breached, alerting)`
        triples -- `breached` is the decidable rate breach (the window has
        reached `n_min`), `alerting` the pre-decidable warning (`alert()`:
        something has fired but the window is still too short for a rate
        to mean anything). The caller escalates on the first and journals
        an incident on the second."""
        out = []
        for b in self.g3.breakers:
            w = self.g3_windows[b.name]
            w.observe(counters.get(b.name, 0) > 0)
            out.append((b.name, w.breached(b), w.alert(b)))
        return out

    # --- lifecycle ---------------------------------------------------------------
    def seed(self, history, expected_trades_sha256: str | None = None) -> CoreOutput:
        """Seed the ledger from `history` (spec §4 settle 1, the restart
        path). A `LedgerDivergence` -- a trades digest that does not
        reproduce, or a journal conflict against a previous incarnation --
        is STOP(HARD, HOLD) per spec §4.1: the recompute disagrees with a
        durable record, which is never something the runtime may trade
        through."""
        out = CoreOutput()
        try:
            s = self.ledger.seed(history, expected_trades_sha256)
        except LedgerDivergence as e:
            self._raise(out, T.StopLevel.HARD, T.StopDisposition.HOLD, f"seed:{e.cause}")
            self._incident(out, "ledger_divergence", cause=e.cause, detail=str(e.detail))
            return out
        self.book = s.book
        self._our_fills = s.position_size
        out.settle, out.book = s, self.book
        return out

    def settle(self, bar: T.NormalizedBar, venue_fills: list[VenueFill], in_flight: set[str], mirrored: set[str],
               real_position: float, now_ms: int, *, our_signed_fills: float | None = None) -> CoreOutput:
        """spec §4 settle 1-8 for one confirmed script-TF bar.

        Order of operations: recompute the ledger (1-3) -> settled book and
        its diff (5) -> classify this bar's ledger fills against
        `venue_fills` (4) -> reconcile (7) -> raise whatever STOP the
        reconciliation demands -> only THEN build the bar's action requests
        (6) and filter every one of them through the STOP/RiskGuard gate ->
        journal the reconcile row (8, the settlement row itself is the
        ledger's). Building actions only after the escalation is what stops
        a settlement from shipping an exposure-increasing order it has
        already decided to STOP over.

        `real_position` is the venue account's own reported position (spec
        §5.4's SECONDARY check); `our_signed_fills` is the PRIMARY basis
        and is derived when not supplied -- see `our_signed_fills`.
        `in_flight`/`mirrored` are sets of Pine order ids: intents with a
        non-terminal action (never classified MISSED, `[r4]`) and intents
        currently mirrored as resting venue orders (§5.2).

        A `LedgerGap` (a non-contiguous or still-forming bar) is NOT caught
        here: it means the caller handed the core the wrong bar, and gap
        carry-forward is the bar layer's job (spec §2), not a STOP. It
        propagates so the driver fixes its own feed rather than the core
        silently settling a hole.
        """
        out = CoreOutput()
        self.guard.begin_bar()
        try:
            s = self.ledger.settle(bar, now_ms)
        except BarsDivergence as e:
            # spec §4.1: a revised settled bar is a feed problem, not a
            # divergent recompute -- FLAT_ONLY, not HARD. (The ledger has
            # already journaled its own bars_divergence incident.)
            self._incident(out, "bars_divergence", detail=str(e))
            self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "bars_divergence")
            return out
        except LedgerDivergence as e:
            self._raise(out, T.StopLevel.HARD, T.StopDisposition.HOLD, f"g1:{e.cause}")
            self._incident(out, "ledger_divergence", cause=e.cause, detail=str(e.detail))
            return out
        except RecomputeAborted:
            # Nothing was journaled and the ledger is untouched: the same
            # bar can be re-settled, so this is an incident, not a STOP.
            self._incident(out, "recompute_aborted", ts_open=bar.ts_open)
            return out
        out.settle = s

        prev_book = self.book
        self.book = s.book
        out.book, out.book_diff = self.book, book_diff(prev_book, self.book)
        emulated = emulated_from_settle(s, out.book_diff, prev_book)
        price = bar.c
        band = self.dead_band.qty(price)

        basis = self._advance_our_fills(signed_qty(venue_fills)) if our_signed_fills is None else our_signed_fills
        classified = classify_bar(emulated, venue_fills,
                                  in_flight_intents=in_flight, mirrored_intents=mirrored, dead_band_qty=band,
                                  ledger_position=s.position_size, real_position=real_position,
                                  entry_slip_bps=0.0, max_entry_slip_bps=self.rcfg.max_missed_entry_distance_bps)
        out.classified = classified

        age, dist = self._missed_bounds(classified, s.bar_index, price)
        self._roll_mirror_early_day(bar)
        dec = reconcile(ReconcileInput(s.bar_index, classified, s.position_size, real_position, basis, price,
                                       quiescent=not in_flight, in_flight=in_flight, stop_level=self.stop.level,
                                       missed_age_bars=age, missed_distance_bps=dist, cfg=self.rcfg,
                                       dead_band=self.dead_band, mirror_early_today=self.mirror_early_today))
        self.mirror_early_today += dec.counters.get("mirror_early", 0)
        out.reconcile = dec
        if dec.stop is not None:
            self._raise(out, *dec.stop)
        if dec.skipped_cycle:
            self._incident(out, "cycle_skipped", bar_index=s.bar_index)
        for name, breached, alerting in self._observe_breakers(dec.counters):
            if breached:
                self._incident(out, "g3_breached", breaker=name)
                self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, f"g3:{name}")
            elif alerting:
                self._incident(out, "g3_alert", breaker=name)

        for a in self._requests(out, s, dec, classified, price, prev_book, in_flight, mirrored):
            if self._permitted(out, a, s.position_size, price):
                out.actions.append(a)
        # The MARKET legs asked for at the next bar's open: that bar's
        # `evaluate()` re-quotes them at the open rather than TRIGGERing
        # the same fill a second time (see `evaluate`).
        self.pending_market = {(a.intent, "EXIT" if a.reduce_only else "ENTRY"): a
                               for a in out.actions if a.kind == "MARKET_AT_OPEN" and a.intent is not None}
        # The per-cycle reset (see `our_signed_fills`) needs BOTH sides
        # flat. A settlement that leaves the LEDGER flat while an
        # uncorrected venue residual stands -- a MISSED EXIT this same
        # settlement just issued a `MARKET_CORRECT` for -- must carry it:
        # resetting to 0 there drove the next bar's basis to `-qty` (the
        # correction's own OURS fill) against a real position of 0, i.e. a
        # false `account_mismatch` STOP one bar after a correct repair.
        if s.position_size == 0.0 and our_signed_fills is None and abs(self._our_fills) <= band:
            self._our_fills = 0.0
        self.j.append_reconcile({"epoch_hash": self.spec.epoch_hash(), "bar_index": s.bar_index,
                                 "cause": ",".join(sorted(dec.counters)) or "clean",
                                 "detail_json": json.dumps(dec.counters, sort_keys=True)})
        return out

    def _advance_our_fills(self, ours_delta: float) -> float:
        self._our_fills += ours_delta
        return self._our_fills

    def _roll_mirror_early_day(self, bar: T.NormalizedBar) -> None:
        """`mirror_early_daily_cap` is a PER-DAY count (spec §5.4), so the
        tally resets on the UTC-day boundary of the settled bar's own open
        rather than growing for the life of the process."""
        day = bar.ts_open // DAY_MS
        if self._mirror_early_day != day:
            self._mirror_early_day, self.mirror_early_today = day, 0

    def _requests(self, out: CoreOutput, s: SettleResult, dec: ReconcileDecision,
                  classified: list[ClassifiedFill], price: float, prev_book: dict[str, Intent],
                  in_flight: set[str], mirrored: set[str]) -> list[ActionRequest]:
        """This settlement's candidate actions, in submission order:
        engine-forced closes first, then the reconciler's corrections, then
        the next open's MARKET legs, then stale-cycle cancels.

        A correction's `reduce_only` is the RECONCILER's own (F1): a
        `MARKET_CORRECT` is exposure-increasing for a missed ENTRY and
        reduce-only for a missed EXIT, and only `reconcile` knows which
        branch it took -- re-deriving the flag from `corr.kind` here
        labelled the missed-EXIT repair exposure-increasing, so `permits()`
        refused under FLAT_ONLY the one order FLAT_ONLY exists to allow
        (spec §5.4/§5.5) and B3 was told it was not reduce-only."""
        reqs: list[ActionRequest] = []
        for c in classified:
            if c.cls == FillClass.SYNTHETIC and c.emulated is not None:
                e = c.emulated
                reqs.append(ActionRequest("SYNTHETIC_CLOSE", e.intent, _side_for_leg(e.is_long, "EXIT"), e.qty, None,
                                          True, "SYNTHETIC", "engine-side close (margin call / intraday cap)", s.bar_index))
        for corr in dec.corrections:
            kind = "FLATTEN" if corr.kind == "FLATTEN" else "CORRECTION"
            reqs.append(ActionRequest(kind, corr.intent, corr.side, corr.qty, None, corr.reduce_only, corr.kind,
                                      corr.reason, s.bar_index))
        reqs += self._market_legs(out, s, price)
        # spec §5.2 stale-cycle cancels. `book_diff` reads CANCELLED for an
        # order that FILLED as well as one that genuinely left the book
        # (see `IntentState`); the bar's own emulated fills disambiguate, so
        # a filled order is never chased with a cancel.
        filled = {e.intent for c in classified if (e := c.emulated) is not None}
        placed = in_flight | mirrored
        for k, state in out.book_diff.items():
            if state != IntentState.CANCELLED:
                continue
            gone = prev_book.get(k)   # a CANCELLED key is gone from the CURRENT book by construction
            if gone is None or gone.key.order_id in filled:
                continue
            order_id = gone.key.order_id
            # N3: there is nothing to cancel unless the venue is actually
            # holding the order -- one we mirrored as a resting order
            # (§5.2) or have a non-terminal action for. Every OTHER book
            # departure is a ledger-side cycle rotation the venue never saw.
            if order_id not in placed:
                continue
            reqs.append(ActionRequest("CANCEL_STALE_CYCLE", order_id, T.Side.BUY, 0.0, None, False, "CANCEL",
                                      f"intent {k} left the settled book unfilled", s.bar_index))
        return reqs

    # --- intrabar ----------------------------------------------------------------
    def evaluate(self, forming: T.NormalizedBar, now_ms: int) -> CoreOutput:
        """spec §4 evaluate 1-4 for one coalesced tick: probe, then turn
        the confirmed intrabar fills into `TRIGGER` MARKETs with the
        ENGINE's qty.

        One `[r4]` de-duplication lives here, because the probe answers
        "what would fill if the bar settled now" afresh on every tick and
        so re-reports the same confirmed fill each time: a `(intent, leg)`
        produces at most ONE request per bar.

        A `(intent, leg)` the preceding settlement already asked for as a
        `MARKET_AT_OPEN` (spec §4 settle 6 -- the same fill, seen from the
        other side of the bar boundary) is not TRIGGERed again, but it is
        not dropped either: this is the "next `evaluate()` with the open
        known" the spec sizes that order at, so the first evaluate of the
        target bar re-quotes it as a SUPERSEDING `MARKET_AT_OPEN` carrying
        the ENGINE's qty at the open (`reason="open_requote"`; see
        `ActionRequest`'s supersede contract). The settle-time request
        stands as the advance notice B3 needs to have an order in place at
        the open; the requote is what fixes its SIZE, which for partition-3
        (`AT_FILL` default) sizing is priced at the open and cannot be
        re-derived by B3 afterwards (the handle's accessors describe its
        LAST run).

        The per-bar fill-action budget is NOT reset here (`settle()` owns
        `begin_bar()`), so it spans the whole bar's tick stream; a
        `RiskViolation` from it is a genuine anomaly (the probe is
        emitting more distinct fills in one bar than the epoch's budget
        allows) and escalates STOP(FLAT_ONLY), unlike a `check_*` budget
        refusal, which only refuses the one action.
        """
        out = CoreOutput()
        pr = self.probe.evaluate(forming, now_ms, journal=self.j)
        out.probe = pr
        out.book = self.book
        if self._triggered_bar != pr.bar_index:
            self._triggered_bar, self._triggered = pr.bar_index, set()
        position = self.ledger.last.position_size if self.ledger.last is not None else 0.0
        price = forming.c
        for f in pr.fills:
            key = (f.intent, f.leg)
            if key in self._triggered:
                continue
            if f.intent == "?":
                # N5: classify's M4 ambiguity -- the ledger could not pin
                # this delta fill to one candidate order. A `"?"` order id
                # identity-matches no venue fill, so submitting under it
                # would create an order the next settlement cannot classify.
                self._triggered.add(key)
                self._incident(out, "ambiguous_trigger_intent", leg=f.leg, qty=f.qty, bar_index=pr.bar_index)
                continue
            pending = self.pending_market.get(key)
            requote = pending is not None and pending.target_bar_index == pr.bar_index
            reduce_only = pending.reduce_only if requote else f.leg == "EXIT"
            a = ActionRequest("MARKET_AT_OPEN" if requote else "TRIGGER", f.intent,
                              _side_for_leg(f.is_long, f.leg), f.qty, None if requote else f.price, reduce_only,
                              "MARKET" if requote else "TRIGGER",
                              "open_requote" if requote else "probe fill confirmed on both intrabar paths",
                              pr.bar_index)
            if not self._permitted(out, a, position, price):
                self._triggered.add(key)
                continue
            try:
                self.guard.count_fill_action()
            except RiskViolation as e:
                # F9: mark the key so the exhausted budget is reported ONCE
                # per key per bar -- the probe re-reports the same confirmed
                # fill on every remaining tick of the bar, and each one used
                # to re-count, re-journal and re-`_raise` it.
                self._triggered.add(key)
                self._incident(out, "risk_violation", detail=str(e), bar_index=pr.bar_index)
                self._raise(out, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, str(e))
                break
            self._triggered.add(key)
            out.actions.append(a)
        for f in pr.retracted:
            self._incident(out, "probe_retract", intent=f.intent, leg=f.leg, bar_index=pr.bar_index)
        return out
