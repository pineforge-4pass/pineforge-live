"""`LiveCore` (spec §4 settle 1-8 / evaluate 1-4, §5.4, §5.5) and the L1
mini-harness. Venue-neutral throughout: every venue name is `"TAPE"`."""
import asyncio, json, pytest
from pineforge_live import types as T
from pineforge_live.core.live import ActionRequest, LiveCore
from pineforge_live.core.reconcile import DeadBand, ReconcileDecision
from pineforge_live.core.riskguard import RiskLimits, Breaker, n_min_for
from pineforge_live.core.classify import ClassifiedFill, EmulatedFill, FillClass, VenueFill
from pineforge_live.core.probe import ProbeFill, ProbeResult
from pineforge_live.epoch import RuntimeConfig
from pineforge_live.adapters.tape import TapeTickSource
from pineforge_live.bars import FormingBarBuilder
from pineforge_live.journal import Journal, StopMarker
from tests.helpers import load_bars, make_handle, corpus_spec

LIMITS = RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)

def _core(test_so, tmp_path, limits=LIMITS, breakers=None):
    spec = corpus_spec(); h = make_handle(test_so, spec)
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = Journal.open(tmp_path / "j.sqlite3", stop_marker=m)
    rc = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000, grace_ms=3_000, open_wait_ms=2_000, risk_limits={})
    c = LiveCore(h, spec, j, m, rc, limits, DeadBand(0.001, 0.001, 5.0),
                 breakers if breakers is not None else [Breaker("missed", 0.01, 500, n_min_for(0.01), 5)])
    return c, j

@pytest.fixture
def core(test_so, test_feed, tmp_path):
    c, j = _core(test_so, tmp_path)
    return c, load_bars(test_feed, 2300), j

def ticks_for(bar, tf):
    src = TapeTickSource([bar], tf, policy="path4", seed=1)
    async def go():
        return [e.tick async for e in src.subscribe(T.InstrumentId("TAPE", T.MarketType.PERP, "ETHUSDT"), 0) if isinstance(e, T.Tick)]
    return asyncio.run(go())

def test_l1_full_cadence_with_perfect_venue(core):
    """Perfect venue: every settle-emitted/probe fill is echoed back as a
    venue fill → all CONFIRMED, no incident, no STOP, G1 holds.

    The venue model is the STRICT reading of spec §5.4 (ruling 6): the
    account snapshot is event-time AFTER the bar's fills (`the ledger
    position the settlement starts from + Σ signed OURS fills this venue
    just echoed`), never the pre-settle ledger position one bar behind the
    fills it is handed alongside. And it honours `ActionRequest`'s
    supersede contract: a later request for the same `(intent, leg,
    target_bar_index)` -- the open-price requote `evaluate()` emits for a
    MARKET leg the preceding settlement asked for in advance -- REPLACES
    the earlier one, so the venue fills the last request per key, not
    both."""
    c, bars, j = core
    out = c.seed(bars[:2000]); assert out.settle is not None and c.stop.level == T.StopLevel.NONE
    pending_actions: list = []
    for i in range(2000, 2060):
        bar = bars[i]; fb = FormingBarBuilder(c.spec.script_tf)
        for t in ticks_for(bar, c.spec.script_tf):
            fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
            pending_actions += ev.actions
        # a perfect venue fills every (superseded-to-last) requested action at its hint on this bar
        by_key: dict[tuple, ActionRequest] = {}
        for a in pending_actions:
            by_key[(a.intent, "EXIT" if a.reduce_only else "ENTRY", a.target_bar_index)] = a
        # n5: ONE venue model, the same one `scripts/l1_harness.py`'s
        # `echo` implements -- a MARKET_AT_OPEN fills at the bar's OPEN
        # (spec §4 settle 6: the open IS the price), a TRIGGER at the
        # probe's own hint, anything else at the close. Pricing a
        # MARKET_AT_OPEN at the close here was harmless only while
        # ENTRY_SLIP was unreachable; with m1 measuring the slip per
        # matched pair it would fabricate one out of the bar's range.
        venue = [VenueFill(a.intent, leg, a.side, a.qty,
                           bar.o if a.kind == "MARKET_AT_OPEN" else (a.price_hint if a.price_hint is not None else bar.c),
                           i, T.FillCause.OURS, f"cid{k}", a.kind == "TRIGGER")
                 for k, ((_, leg, _), a) in enumerate(by_key.items())]
        real = c.ledger.last.position_size + sum((v.qty if v.side is T.Side.BUY else -v.qty)
                                                 for v in venue if v.cause is T.FillCause.OURS)
        out = c.settle(bar, venue_fills=venue, in_flight=set(), mirrored=set(), real_position=real, now_ms=bar.ts_open + 900_000)
        pending_actions = [a for a in out.actions if a.kind in ("MARKET_AT_OPEN", "SYNTHETIC_CLOSE")]
        assert out.stop is None, (i, out.stop)
        assert not [x for x in out.reconcile.corrections if x.kind == "MARKET_CORRECT"], (i, out.reconcile)
        # F8: "all CONFIRMED" is the claim -- a MISSED the reconciler
        # silently skips (`skipped_position_mismatch`) passes the two
        # assertions above but is not a clean cadence.
        assert all(x.cls is FillClass.CONFIRMED for x in out.classified), (i, [x.cls for x in out.classified])
        assert not out.incidents, (i, out.incidents)
    assert c.stop.level == T.StopLevel.NONE
    assert j.last_settlement(c.spec.epoch_hash())["bar_index"] == 2059

def test_g1_break_raises_hard_hold(core):
    c, bars, j = core; c.seed(bars[:2000])
    j._exec("UPDATE settlements SET broker_state_hash=? WHERE bar_index=1999", (f"{7:016x}",))
    out = c.settle(bars[2000], [], set(), set(), 0.0, 0)
    assert out.stop is not None and out.stop[0] == T.StopLevel.HARD and out.stop[1] == T.StopDisposition.HOLD
    assert c.stop.level == T.StopLevel.HARD and c.marker.exists()

def test_stop_refuses_triggers(core):
    """F3: `evaluate()` puts every TRIGGER through `StopController.permits`,
    so under FLAT_ONLY an exposure-increasing probe fill is refused with an
    `action_refused_by_stop` incident and never reaches `out.actions`.

    Bar 2001 (the corpus reversal), not 2000: the probe emits NO fills at
    all on bar 2000, so the original "every emitted action is reduce-only"
    assertion was vacuously true over an empty list and an implementation
    that never consulted `permits` in `evaluate()` passed it. The
    settlement of bar 2000 runs FIRST and under the same STOP, so its
    exposure-increasing MARKET_AT_OPEN leg is refused there and is NOT in
    `pending_market` -- which is what leaves the probe's own ENTRY-leg
    fill for the STOP gate to refuse here."""
    c, bars, j = core; c.seed(bars[:2000])
    c.stop.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "test")
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    fb = FormingBarBuilder(c.spec.script_tf); seen_fills = 0; refused = []
    for t in ticks_for(bars[2001], c.spec.script_tf):
        fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
        seen_fills += len(ev.probe.fills)
        refused += [x for x in ev.incidents if x["kind"] == "action_refused_by_stop"]
        assert all(a.reduce_only for a in ev.actions), [a.kind for a in ev.actions if not a.reduce_only]
    assert seen_fills > 0
    assert [(x["action"], x["intent"]) for x in refused] == [("TRIGGER", "L")]


# --- controller rulings -------------------------------------------------------

def test_our_signed_fills_is_derived_from_ours_venue_fills(core):
    """Ruling 1: with `our_signed_fills` not supplied, LiveCore accumulates
    the signed qty of the OURS venue fills (+BUY/-SELL) it is handed,
    anchored on the position `seed()` adopted and reset when the ledger
    settles flat. A non-OURS fill never counts (it is the venue's, not
    ours); an explicitly-passed value wins outright."""
    c, bars, j = core
    c.seed(bars[:2000])
    assert c.our_signed_fills == c.ledger.last.position_size == -1.0   # adoption anchor
    ours = VenueFill("L", "ENTRY", T.Side.BUY, 1.0, 100.0, 2000, T.FillCause.OURS, "cid0")
    theirs = VenueFill(None, None, T.Side.BUY, 4.0, 100.0, 2000, T.FillCause.LIQUIDATION, None)
    c.settle(bars[2000], [ours, theirs], set(), set(), -1.0, 0)
    assert c.our_signed_fills == 0.0                                   # -1 + 1 BUY; the liquidation is not ours
    c.settle(bars[2001], [], set(), set(), 0.0, 0, our_signed_fills=7.0)
    assert c.our_signed_fills == 0.0                                   # an explicit basis never mutates the accumulator

def test_missed_age_bars_grows_then_drops(core, monkeypatch):
    """Ruling 2: `missed_age_bars` is `bar_index - the first bar the same
    (intent, leg) was classified MISSED`, the max across the currently
    MISSED fills, and the entry is dropped the moment that (intent, leg)
    stops being MISSED."""
    import pineforge_live.core.live as live
    seen: list = []
    real_reconcile = live.reconcile
    def spy(inp):
        seen.append(inp)
        return real_reconcile(inp)
    monkeypatch.setattr(live, "reconcile", spy)
    c, bars, j = core
    c.seed(bars[:2000])
    # bar 2001 is the corpus reversal: the ledger fills, the venue does not.
    for i in (2000, 2001, 2002, 2003):
        c.settle(bars[i], [], set(), set(), c.ledger.last.position_size, 0, our_signed_fills=c.ledger.last.position_size)
    ages = [inp.missed_age_bars for inp in seen]
    assert ages[0] == 0                       # bar 2000: nothing emulated, nothing MISSED
    assert ages[1] == 0                       # bar 2001: first bar these fills read MISSED
    assert ages[2] == 0 and ages[3] == 0      # they do not recur, so the entries are dropped again
    assert c.missed_since == {}

def test_missed_without_a_strict_subset_never_market_corrects(core):
    """Ruling 3: a MISSED classification whose position basis is NOT a
    strict subset of the ledger position must never produce a
    `MARKET_CORRECT` -- the reconciler enforces it and LiveCore must not
    route around it. Real fixture: bar 2001's reversal fills on the ledger
    with an empty venue, against a basis on the OPPOSITE side.

    N6: that same input is X13's `unreconcilable_sides` -- the ledger and
    our own fills disagree about which way the position even points -- so
    the pin states the STOP the reconciler raises for it too, not just the
    correction it declines to make."""
    c, bars, j = core
    c.seed(bars[:2000])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    out = c.settle(bars[2001], [], set(), set(), -3.0, 0, our_signed_fills=-3.0)
    assert out.settle.position_size == 1.0
    assert [x.cls for x in out.classified].count(FillClass.MISSED) == 2
    assert not [x for x in out.reconcile.corrections if x.kind == "MARKET_CORRECT"]
    assert out.reconcile.counters.get("skipped_position_mismatch") == 1
    assert out.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.HOLD, "unreconcilable_sides")

def test_stop_refuses_settle_actions_with_an_incident(test_so, test_feed, tmp_path):
    """Ruling 4: an action refused by the STOP gate is journaled as an
    `action_refused_by_stop` incident, never silently dropped -- here the
    settled book's MARKET entry under `STOP(HARD, HOLD)`."""
    c, j = _core(test_so, tmp_path)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    c.stop.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "test")
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert out.actions == []
    kinds = [x["kind"] for x in out.incidents]
    assert kinds.count("action_refused_by_stop") >= 1
    assert [r for r in j.rows("incidents", "kind=?", ("action_refused_by_stop",))]

def test_risk_guard_violation_refuses_the_action_with_an_incident(test_so, test_feed, tmp_path):
    """Ruling 4 (second half): a `RiskGuard.check_*` violation refuses the
    action and journals an incident rather than raising or shipping it."""
    limits = RiskLimits(1e6, 1e12, 1e-9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)
    c, j = _core(test_so, tmp_path, limits=limits)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert out.actions == []
    assert [x for x in out.incidents if x["kind"] == "risk_refused" and x["cause"] == "max_order_notional"]

def test_settled_market_entry_becomes_a_close_and_an_open_leg(core):
    """spec §4 settle 6 + the engine's `close_opposite_then_enter` rule
    (`strategy_pending_order_fill_qty` doxygen): a settled MARKET entry
    whose side opposes the live position is submitted as TWO legs -- a
    reduce-only close of the live position, then the engine's own opened
    qty -- so both halves of the ledger's own reversal have a venue
    counterpart."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    mkt = [a for a in out.actions if a.kind == "MARKET_AT_OPEN"]
    assert [(a.intent, a.side, a.qty, a.reduce_only, a.target_bar_index) for a in mkt] == [
        ("L", T.Side.BUY, 1.0, True, 2001), ("L", T.Side.BUY, 1.0, False, 2001)]

def test_a_settled_market_is_requested_once_not_again_by_the_probe(core):
    """`[r4]` one open action per intent: the MARKET_AT_OPEN settle(n)
    emitted for bar n+1 stands as the advance, and the probe's own fill of
    the same (intent, leg) on that bar is NOT a second request.

    m5/F6: on this fixture (partition 1, fixed qty) the engine's qty at
    the open equals the settle-time close proxy, so the first evaluate of
    bar 2001 confirms both legs and emits NOTHING -- no TRIGGER (the same
    fill, seen from the other side of the bar boundary) and no requote
    (there is no size to fix). A requote is for a real qty difference and
    a withdraw for a leg the open did not confirm; neither applies here,
    and an unconditional requote cost an order op per settled MARKET leg
    per bar for nothing."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    advance = {(a.intent, a.reduce_only, a.qty) for a in out.actions if a.kind == "MARKET_AT_OPEN"}
    assert advance == {("L", True, 1.0), ("L", False, 1.0)}
    fb = FormingBarBuilder(c.spec.script_tf); emitted = []
    for t in ticks_for(bars[2001], c.spec.script_tf):
        fb.push(t); emitted += c.evaluate(fb.forming(), now_ms=t.ts).actions
    assert emitted == []
    assert c.pending_market == {}          # both keys were confirmed and consumed

def test_cancel_stale_cycle_reads_venue_truth_not_ledger_truth(core, test_so, test_feed, tmp_path):
    """`book_diff` reads CANCELLED for an order that FILLED as well as one
    that was cancelled (see `IntentState`), so the bar's own fills have to
    disambiguate -- and m3: only a fill the VENUE reported proves the venue
    is no longer holding the order.

    NEW-1: both arms settle with `mirrored={"L"}`, or the N3 "is the venue
    even holding it" gate suppresses the cancel before the fill check is
    consulted and the disambiguation is unpinned (verified: with the fill
    check deleted, an unmirrored pin passes either way).

    The mirrored-and-venue-filled arm is the original claim (a filled
    order is never chased). The mirrored-and-NOT-venue-filled arm is m3:
    the ledger filled the order, the venue did not (a MISSED being
    repaired by a MARKET_CORRECT), and its resting `closePosition` order
    is still live at the venue -- exactly the order that must be cancelled
    before it fires on the next cycle's position."""
    from pineforge_live.core.book import IntentState
    c, bars, j = core
    c.seed(bars[:2000])
    legs = [a for a in c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0).actions
            if a.kind == "MARKET_AT_OPEN"]
    venue = _echo(legs, bars[2001], 2001)
    out = c.settle(bars[2001], venue, set(), {"L"}, 1.0, 0, our_signed_fills=1.0)
    assert out.book_diff == {"L|MARKET||54": IntentState.CANCELLED}
    assert [a for a in out.actions if a.kind == "CANCEL_STALE_CYCLE"] == []

    unfilled = tmp_path / "unfilled"; unfilled.mkdir()
    c2, _j2 = _core(test_so, unfilled)
    c2.seed(bars[:2000])
    c2.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    out2 = c2.settle(bars[2001], [], set(), {"L"}, -1.0, 0, our_signed_fills=-1.0)
    assert [(a.kind, a.intent) for a in out2.actions if a.kind == "CANCEL_STALE_CYCLE"] == [("CANCEL_STALE_CYCLE", "L")]

def test_seed_divergence_raises_hard_hold(core):
    """A `LedgerDivergence` out of `seed()` (here a trades-digest mismatch
    on the restart path) is STOP(HARD, HOLD), and the marker is set."""
    c, bars, j = core
    out = c.seed(bars[:2000], expected_trades_sha256="0" * 64)
    assert out.settle is None and out.stop[0] == T.StopLevel.HARD and out.stop[1] == T.StopDisposition.HOLD
    assert c.stop.level == T.StopLevel.HARD and c.marker.exists()

def test_bars_divergence_is_flat_only_with_an_incident(core):
    """spec §4.1: a revised settled bar is STOP(FLAT_ONLY) + a
    `bars_divergence` incident, not a HARD stop."""
    import dataclasses
    c, bars, j = core
    c.seed(bars[:2000])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    revised = dataclasses.replace(bars[2000], c=bars[2000].c + 5.0)
    out = c.settle(revised, [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert out.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "bars_divergence")
    assert [x for x in out.incidents if x["kind"] == "bars_divergence"]
    assert c.stop.level == T.StopLevel.FLAT_ONLY

def test_action_request_and_core_output_shapes(core):
    """The facade's own contract: `ActionRequest` is frozen and carries the
    nine fields Plan B3 reads; `CoreOutput` always carries the book."""
    import dataclasses
    c, bars, j = core
    assert [f.name for f in dataclasses.fields(ActionRequest)] == [
        "kind", "intent", "side", "qty", "price_hint", "reduce_only", "cls", "reason", "target_bar_index"]
    out = c.seed(bars[:2000])
    assert out.book is c.book and out.actions == [] and out.reconcile is None
    assert isinstance(ReconcileDecision(), ReconcileDecision)


def _echo(legs, bar, bar_index):
    """A perfect venue's fills for a list of MARKET legs (venue `"TAPE"`)."""
    return [VenueFill(a.intent, "EXIT" if a.reduce_only else "ENTRY", a.side, a.qty, bar.c, bar_index,
                      T.FillCause.OURS, f"cid{k}") for k, a in enumerate(legs)]

def test_an_account_snapshot_that_disagrees_with_our_fills_escalates(core):
    """Ruling 6 (F2): `real_position` is taken AS GIVEN -- there is no
    "forward the snapshot by this bar's own fills" tolerance in LiveCore.
    A caller's snapshot that disagrees with our own fill basis beyond the
    dead-band is a real disagreement and stays one: `STOP(FLAT_ONLY)` with
    cause `account_mismatch`. Spec §5.4 puts the snapshot event-time AFTER
    the last fill, so a caller handing over a stale one is a caller bug to
    surface, not a core tolerance to absorb it with."""
    c, bars, j = core
    c.seed(bars[:2000])
    legs = [a for a in c.settle(bars[2000], [], set(), set(), -1.0, 0).actions if a.kind == "MARKET_AT_OPEN"]
    out = c.settle(bars[2001], _echo(legs, bars[2001], 2001), set(), set(), 9.0, 0)
    assert out.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "account_mismatch")
    assert not [x for x in out.incidents if x["kind"] == "account_snapshot_stale"]
    assert not hasattr(c, "_account_position")


# --- Task 8 review fix wave (F1, F4-F7, F9, N5, Task 7 carry) ------------------

def _probe_result(bar_index, forming, fills):
    """A stubbed `Probe.evaluate` outcome -- the corpus fixture is
    partition 1 (fixed qty), so a probe whose sizing/identity differs from
    the settlement's own has to be stood up rather than found."""
    return ProbeResult(bar_index, forming, fills, [], [], {}, False, 1, False)

def test_a_missed_exit_correction_is_reduce_only_and_ships_under_flat_only(core, monkeypatch):
    """F1: a MISSED EXIT -- the venue still holds what the ledger closed --
    is corrected by a REDUCE-ONLY `MARKET_CORRECT` (the reconciler runs
    that branch with `increases=False` precisely so it is not gated), so
    LiveCore must take `CorrectionRequest.reduce_only` as given rather than
    re-derive it from `kind`. Re-derived, it read exposure-increasing,
    `permits()` refused it under FLAT_ONLY -- the one order FLAT_ONLY
    exists to allow (spec §5.5) -- and B3 was told it was not reduce-only.

    Ledger +1 at bar 2001 against our own fills / the account at +2: the
    ledger's close never reached the venue."""
    import pineforge_live.core.live as live
    c, bars, j = core
    c.seed(bars[:2000])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    miss = ClassifiedFill(FillClass.MISSED, EmulatedFill("XL", "EXIT", True, 1.0, bars[2001].c, 2001), None, 1.0, "")
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [miss])
    c.stop.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "test")
    out = c.settle(bars[2001], [], set(), set(), 2.0, 0, our_signed_fills=2.0)
    assert [x.kind for x in out.reconcile.corrections] == ["MARKET_CORRECT"]
    corr = [a for a in out.actions if a.kind == "CORRECTION"]
    assert [(a.intent, a.side, a.qty, a.reduce_only) for a in corr] == [("XL", T.Side.SELL, 1.0, True)]
    assert not [x for x in out.incidents if x["kind"] == "action_refused_by_stop" and x["action"] == "CORRECTION"]

def test_missed_bounds_ages_one_pair_across_settles_and_drops_it(core):
    """F4: `_missed_bounds` directly -- the committed cadence pin never
    sees a non-zero age (the fixture's MISSED pair does not recur), so a
    `_missed_bounds` returning a constant 0 passed it. The SAME (intent,
    leg) MISSED at bar 10 then bar 11 ages to 1; absent at bar 12 the
    entry is dropped and the age falls back to 0."""
    c, bars, j = core
    def missed(fill_bar=10):
        return ClassifiedFill(FillClass.MISSED, EmulatedFill("L", "ENTRY", True, 1.0, 100.0, fill_bar), None, 1.0, "")
    assert c._missed_bounds([missed()], 10, 100.0)[0] == 0
    assert c.missed_since == {("L", "ENTRY"): 10}
    assert c._missed_bounds([missed()], 11, 100.0)[0] == 1
    assert c._missed_bounds([], 12, 100.0)[0] == 0
    assert c.missed_since == {}

def test_our_fills_carry_an_uncorrected_venue_residual_past_a_flat_ledger(core, monkeypatch):
    """F5: the derived basis is reset at ledger-flat only when the venue
    agrees it is flat. A settlement that leaves the LEDGER flat while a
    MISSED EXIT (just corrected) leaves the VENUE holding must carry the
    residual: resetting to 0 there drove the next bar's basis to `-qty`
    against a real position of 0, and the correction that repaired the
    venue tripped a false `account_mismatch` STOP one bar later."""
    import pineforge_live.core.live as live
    c, bars, j = core
    c.seed(bars[:2000])                                   # adoption anchor: -1.0
    real_settle = c.ledger.settle
    def flat_settle(bar, now_ms):
        s = real_settle(bar, now_ms); s.position_size = 0.0; return s   # the corpus script is never flat
    monkeypatch.setattr(c.ledger, "settle", flat_settle)
    miss = ClassifiedFill(FillClass.MISSED, EmulatedFill("XL", "EXIT", True, 1.0, bars[2000].c, 2000), None, 1.0, "")
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [miss])
    ours = [VenueFill("L", "ENTRY", T.Side.BUY, 2.0, bars[2000].c, 2000, T.FillCause.OURS, "cid0")]
    out = c.settle(bars[2000], ours, set(), set(), 1.0, 0)
    assert [(x.kind, x.side, x.reduce_only) for x in out.reconcile.corrections] == [("MARKET_CORRECT", T.Side.SELL, True)]
    assert c.our_signed_fills == 1.0            # the venue is NOT flat: the residual is carried
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [])
    correction_fill = [VenueFill("XL", "EXIT", T.Side.SELL, 1.0, bars[2001].c, 2001, T.FillCause.OURS, "cid1")]
    out = c.settle(bars[2001], correction_fill, set(), set(), 0.0, 0)
    assert out.stop is None and out.reconcile.counters.get("account_mismatch") is None
    assert c.our_signed_fills == 0.0            # now both are flat: the per-cycle meaning is re-established

def test_a_settled_market_is_requoted_at_the_open_price_qty(core, monkeypatch):
    """F6: spec §4 settle 6 places the engine's qty "at the next
    `evaluate()` with the open known". The settle-time `MARKET_AT_OPEN` is
    priced off the settle's CLOSE (the only price it has), so the first
    `evaluate()` of the target bar re-quotes it with the open-price qty as
    a SUPERSEDING request rather than being de-duplicated away -- for
    partition-3 (`AT_FILL` default) sizing the two genuinely differ, and
    B3 cannot re-derive the qty afterwards (the handle's accessors
    describe its LAST run)."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    advance = [a for a in out.actions if a.kind == "MARKET_AT_OPEN" and not a.reduce_only]
    assert len(advance) == 1 and advance[0].qty == 1.0 and advance[0].target_bar_index == 2001
    pf = ProbeFill("L", "ENTRY", True, 2.5, bars[2001].o, 2001, 2001)      # open-price sizing != the close proxy
    monkeypatch.setattr(c.probe, "evaluate",
                        lambda forming, now_ms, journal=None: _probe_result(2001, forming, [pf]))
    fb = FormingBarBuilder(c.spec.script_tf); emitted = []
    for t in ticks_for(bars[2001], c.spec.script_tf):
        fb.push(t); emitted += c.evaluate(fb.forming(), now_ms=t.ts).actions
    # The stub reports the ENTRY leg only, so the reduce-only advance for
    # the same intent is a leg the open did not confirm -- m5 withdraws it
    # with a `qty=0` supersede rather than leaving an order standing at the
    # venue that the ledger will never book.
    assert [(a.kind, a.intent, a.qty, a.reduce_only, a.reason, a.target_bar_index) for a in emitted] == [
        ("MARKET_AT_OPEN", "L", 2.5, False, "open_requote", 2001),
        ("MARKET_AT_OPEN", "L", 0.0, True, "withdraw", 2001)]

def test_the_settled_market_legs_size_the_close_to_the_live_position(core):
    """F7 (first half): on the corpus reversal targeted at bar 2001, the
    reduce-only close leg is the WHOLE live position and the open leg is
    the engine's own opened qty (`strategy_pending_order_fill_qty`), read
    inside the settle window."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    it = next(x for x in out.book.values() if x.is_market)
    rc, engine_qty, close_only, _partition = c.h.probe_fill_qty(it.index, bars[2000].c)
    assert rc == 0 and not close_only
    legs = [a for a in out.actions if a.kind == "MARKET_AT_OPEN"]
    close = [a for a in legs if a.reduce_only]; opened = [a for a in legs if not a.reduce_only]
    assert [a.qty for a in close] == [abs(out.settle.position_size)]
    assert [a.qty for a in opened] == [engine_qty]

def test_a_close_only_market_closes_the_whole_live_position(core, monkeypatch):
    """F7 (second half): for the two MARKET reversal kernels the engine's
    `qty` under `close_only=1` is the UNOPENED REMAINDER (<= kQtyEpsilon,
    `engine_fills.cpp` `close_opposite_then_enter`), not the closed size --
    the ABI exposes no closed qty at all. Sizing the close leg
    `min(|qty|, |pos|)` therefore produced a sub-dead-band leg that was
    dropped: the venue never closed, and the ledger's own close read MISSED
    a bar later. Under `close_only` the close leg is `abs(position)`."""
    c, bars, j = core
    c.seed(bars[:2000])
    monkeypatch.setattr(c.h, "probe_fill_qty", lambda index, price: (0, 0.0, True, 3))
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    legs = [a for a in out.actions if a.kind == "MARKET_AT_OPEN"]
    assert [(a.intent, a.side, a.qty, a.reduce_only) for a in legs] == [
        ("L", T.Side.BUY, abs(out.settle.position_size), True)]
    assert "close_only 1" in legs[0].reason and "partition 3" in legs[0].reason

def test_a_risk_violation_is_journaled_once_per_key_per_bar(test_so, test_feed, tmp_path, monkeypatch):
    """F9: the `RiskViolation` path marks the `(intent, leg)` triggered
    before it breaks, so the budget is reported once per key per bar. It
    did not, so every later tick of the same bar re-counted the same fill,
    re-journalled `risk_violation` and re-called `_raise`."""
    limits = RiskLimits(1e6, 1e12, 1e9, 0, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)
    c, j = _core(test_so, tmp_path, limits=limits)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    pf = ProbeFill("XL", "EXIT", True, 1.0, bars[2001].c, 2000, 2001)      # reduce-only: `permits` is not the refusal here
    monkeypatch.setattr(c.probe, "evaluate",
                        lambda forming, now_ms, journal=None: _probe_result(2001, forming, [pf]))
    fb = FormingBarBuilder(c.spec.script_tf); incidents = []
    ticks = ticks_for(bars[2001], c.spec.script_tf)
    assert len(ticks) > 1
    for t in ticks:
        fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
        incidents += ev.incidents
        assert ev.actions == []
    assert [x["kind"] for x in incidents] == ["risk_violation"]
    assert len([r for r in j.rows("incidents", "kind=?", ("risk_violation",))]) == 1

def test_an_ambiguous_probe_intent_is_an_incident_not_a_trigger(core, monkeypatch):
    """N5: a `_delta_fill` whose intent attribution could not be pinned
    carries `intent="?"` (classify's M4 ambiguity). That id identity-matches
    no venue fill, so submitting a TRIGGER under it would create an order
    the next settlement cannot classify -- it is an incident instead."""
    c, bars, j = core
    c.seed(bars[:2000])
    pf = ProbeFill("?", "EXIT", True, 1.0, bars[2001].c, 2000, 2001)
    monkeypatch.setattr(c.probe, "evaluate",
                        lambda forming, now_ms, journal=None: _probe_result(2001, forming, [pf]))
    fb = FormingBarBuilder(c.spec.script_tf)
    t = ticks_for(bars[2001], c.spec.script_tf)[0]
    fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
    assert ev.actions == []
    assert [(x["kind"], x["leg"]) for x in ev.incidents] == [("ambiguous_trigger_intent", "EXIT")]

def test_cancel_stale_cycle_only_targets_an_order_the_venue_knows(test_so, test_feed, tmp_path, monkeypatch):
    """N3: `CANCEL_STALE_CYCLE` is for an order the venue actually holds --
    one we mirrored as a resting order (§5.2) or have a non-terminal
    action for. An intent that left the settled book having never been
    placed there is nothing to cancel; the SAME book departure with the id
    mirrored still is. (`classify_bar` is stubbed empty so the departure is
    not already disambiguated by the bar's own fills.)"""
    import pineforge_live.core.live as live
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [])
    bars = load_bars(test_feed, 2300)

    def departure(mirrored):
        d = tmp_path / ("mirrored" if mirrored else "unknown"); d.mkdir()
        c, _j = _core(test_so, d)
        c.seed(bars[:2000])
        c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
        held = {"L"} if mirrored else set()
        out = c.settle(bars[2001], [], set(), held, -1.0, 0, our_signed_fills=-1.0)
        assert list(out.book_diff) == ["L|MARKET||54"]
        return [(a.kind, a.intent) for a in out.actions if a.kind == "CANCEL_STALE_CYCLE"]

    assert departure(mirrored=False) == []
    assert departure(mirrored=True) == [("CANCEL_STALE_CYCLE", "L")]

def test_the_stop_controllers_hold_bound_comes_from_risk_limits(test_so, tmp_path):
    """Task 7 re-review N2 (carry): `RiskLimits` is the single source for
    `hard_stop_max_hold_ms` -- LiveCore hands it to the `StopController` at
    construction rather than leaving the controller on its "disabled"
    default, so `hold_expired()` can actually become True."""
    limits = RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000,
                        hard_stop_max_hold_ms=60_000)
    c, j = _core(test_so, tmp_path, limits=limits)
    assert c.stop.hard_stop_max_hold_ms == 60_000
    c.stop.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "test")
    assert not c.stop.hold_expired(c.stop.raised_ms + 59_999)
    assert c.stop.hold_expired(c.stop.raised_ms + 60_000)


# --- final wave: m2 end to end -------------------------------------------------

def test_a_g3_breaker_alerts_then_breaches_on_the_missed_counter(test_so, test_feed, tmp_path):
    """m2, end to end: the whole `g3_alert` / `g3_breached` path had zero
    coverage because the only breaker ever configured (`orphan`) named a
    counter nothing bumps. With the breaker watching `missed`, bar 2001's
    reversal (the ledger fills, the empty venue does not) samples True:
    below `n_min` that is an `alert` incident, and once the window reaches
    `n_min` the breach escalates `STOP(FLAT_ONLY, "g3:missed")`.

    theta 0.5 / n_min 4 is the smallest window `self_test` accepts
    (`UB_95(0, 4) < 0.5`) and `x_max = 0` makes one sample decisive; the
    corpus cadence itself is far cleaner than that -- the harness runs its
    `missed` breaker at the spec's own 1%. The basis is held at 0 so bar
    2001's MISSED entry is correctable and the reconciler raises no STOP
    of its own to confound the G3 one."""
    c, j = _core(test_so, tmp_path, breakers=[Breaker("missed", 0.5, 4, 4, 0)])
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    seen = []
    for i in range(2000, 2004):
        out = c.settle(bars[i], [], set(), set(), 0.0, 0, our_signed_fills=0.0)
        seen.append(([x["kind"] for x in out.incidents], out.stop))
    assert [k for k, _ in seen] == [[], ["g3_alert"], ["g3_alert"], ["g3_breached"]], seen
    assert seen[1][1] is None and seen[2][1] is None                     # an alert is not a STOP
    assert seen[3][1] == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "g3:missed")
    assert c.stop.level == T.StopLevel.FLAT_ONLY and c.stop.cause == "g3:missed"
    assert [r for r in j.rows("incidents", "kind=?", ("g3_breached",))]


# --- final wave: M1, M3, M4, m5-m11, n8, n9, n12, NEW-2..4 --------------------

def test_an_idempotent_re_delivery_settles_nothing_twice(core):
    """M1: `Ledger.settle` returns the LAST settlement unchanged for a
    byte-identical re-delivery of the bar it already settled (spec §4.8) --
    check mode's REST catch-up delivers one routinely. LiveCore has to
    notice: re-running the settlement over the SAME `SettleResult` emulates
    the bar's fills a second time against whatever `venue_fills` the driver
    passes (normally none, since they were consumed), classifies them
    MISSED, reconciles again -- a duplicate `MARKET_CORRECT` is real money
    -- writes a SECOND `reconciles` row for the bar, and resets
    `pending_market` from re-derived legs."""
    c, bars, j = core
    c.seed(bars[:2000])
    first = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    pending_before = dict(c.pending_market)
    assert pending_before and first.classified == []
    again = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert again.settle is first.settle
    assert again.classified == [] and again.actions == [] and again.reconcile is None and again.stop is None
    assert [x["kind"] for x in again.incidents] == ["idempotent_redelivery"]
    assert c.pending_market == pending_before
    rows = j.rows("reconciles", "bar_index=?", (2000,))
    assert len(rows) == 1


def test_a_venue_initiated_fill_produces_one_hard_flat(core):
    """M3, spec §5.5(c): `UNATTRIBUTED_VENUE` escalates `STOP(HARD,
    FLATTEN)` and `permits("hard_flat", ...)` is carefully
    disposition-aware -- but nothing EMITTED the order, so the remaining
    venue position sat under HARD (where no new reduce-only order is
    permitted except this one) indefinitely. It is emitted from the STOP
    state, not by the reconciler: a HARD_FLAT is not a correction toward
    the ledger, it is the disposition acting on venue truth."""
    c, bars, j = core
    c.seed(bars[:2000])
    liq = VenueFill(None, None, T.Side.BUY, 2.0, bars[2000].c, 2000, T.FillCause.LIQUIDATION, None)
    out = c.settle(bars[2000], [liq], set(), set(), 1.0, 0, our_signed_fills=1.0)
    assert out.stop == (T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")
    flat = [a for a in out.actions if a.kind == "FLATTEN"]
    assert [(a.side, a.qty, a.reduce_only, a.cls) for a in flat] == [(T.Side.SELL, 1.0, True, "HARD_FLAT")]
    assert [x for x in out.incidents if x["kind"] == "hard_flat"]
    # once only: the venue is being flattened, not flattened every bar
    again = c.settle(bars[2001], [], set(), set(), 1.0, 0, our_signed_fills=1.0)
    assert [a for a in again.actions if a.kind == "FLATTEN"] == []


def test_a_skipped_missed_is_re_presented_and_ages(core, monkeypatch):
    """M4(a): `emulated_from_settle` emits bar-n fills only and
    `classify_bar` has no memory, so a MISSED the reconciler skipped as
    NOT QUIESCENT simply vanished -- nothing aged it, nothing re-counted
    it, and `[r4]`'s "≤ 1 script bar old" bound could never bind because no
    (intent, leg) ever read MISSED on two consecutive settlements. The
    carry re-presents it, so `missed_since` ages it."""
    import pineforge_live.core.live as live
    c, bars, j = core
    c.seed(bars[:2000])
    miss = ClassifiedFill(FillClass.MISSED, EmulatedFill("L", "ENTRY", True, 1.0, bars[2000].c, 2000), None, 1.0, "")
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [miss])
    out = c.settle(bars[2000], [], {"X"}, set(), -1.0, 0, our_signed_fills=-1.0)
    assert out.reconcile.counters.get("skipped_not_quiescent") == 1
    assert c._carried_missed and c.missed_since == {("L", "ENTRY"): 2000}
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [])
    out = c.settle(bars[2001], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert [x.cls for x in out.classified] == [FillClass.MISSED]      # re-presented, not lost
    assert out.reconcile.counters.get("missed") == 1
    # the same pair now reads MISSED on two consecutive settlements, which
    # is what makes the `[r4]` age bound reachable at all
    assert c._missed_bounds(out.classified, 2001, bars[2001].c)[0] == 1


def test_consecutive_non_quiescent_settles_escalate_at_disagree_twice(core):
    """M4(b), spec §5.4: "else skip (bounded) and count toward
    `disagree_twice`". A driver whose actions never go terminal makes every
    settlement non-quiescent and the reconciler then does NOTHING, bar
    after bar, with only a counter to show for it. `disagree_twice` was
    declared in `RiskLimits` and read nowhere."""
    c, bars, j = core
    c.seed(bars[:2000])
    assert c.limits.disagree_twice == 2
    first = c.settle(bars[2000], [], {"L"}, set(), -1.0, 0, our_signed_fills=-1.0)
    assert first.stop is None and c._not_quiescent_streak == 1
    second = c.settle(bars[2001], [], {"L"}, set(), -1.0, 0, our_signed_fills=-1.0)
    assert second.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "disagree_twice")
    assert [x for x in second.incidents if x["kind"] == "disagree_twice"]


def test_a_quiescent_settle_resets_the_non_quiescent_streak(core):
    """M4(b), the other half: the bound is on CONSECUTIVE skips."""
    c, bars, j = core
    c.seed(bars[:2000])
    c.settle(bars[2000], [], {"L"}, set(), -1.0, 0, our_signed_fills=-1.0)
    c.settle(bars[2001], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert c._not_quiescent_streak == 0


def test_the_daily_reconcile_cap_refuses_and_skips_the_cycle(test_so, test_feed, tmp_path, monkeypatch):
    """m8, spec §5.5: `max_daily_reconciles` is the low-n guard on
    corrections -- the G3 rate breakers are alert-only below their own
    `n_min` (381 samples at theta = 1%), so a runtime correcting every bar
    reaches no decidable rate for a year. It was declared, core-computable
    and unwired."""
    import pineforge_live.core.live as live
    limits = RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 1, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)
    c, j = _core(test_so, tmp_path, limits=limits)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    def missed_on(bar_index, is_long):
        # the missed entry has to sit on the LEDGER's own side (short at
        # bar 2000, long after the 2001 reversal) or the reconciler's
        # representative filter drops it before the cap is ever consulted
        return [ClassifiedFill(FillClass.MISSED, EmulatedFill("L", "ENTRY", is_long, 1.0, bars[bar_index].c, bar_index),
                               None, 1.0, "")]
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: missed_on(2000, False))
    out = c.settle(bars[2000], [], set(), set(), 0.0, 0, our_signed_fills=0.0)
    assert [a.kind for a in out.actions if a.kind == "CORRECTION"] == ["CORRECTION"] and c.reconciles_today == 1
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: missed_on(2001, True))
    out = c.settle(bars[2001], [], set(), set(), 0.0, 0, our_signed_fills=0.0)
    assert [a for a in out.actions if a.kind == "CORRECTION"] == []
    kinds = [x["kind"] for x in out.incidents]
    assert "max_daily_reconciles" in kinds and "cycle_skipped" in kinds


def test_the_horizon_alerts_once_then_refuses_the_settle(test_so, test_feed, tmp_path):
    """m7, spec §2: `horizon_bars` is the frozen `last_bar_index` the
    epoch's `realtime_tail` pins `pine_last_bar_index()` to, so settling
    past it runs the engine with a `last_bar_index` BELOW the actual last
    bar. "At 80% consumption the runtime alerts and at 100% forces an
    epoch rotation before the next settlement" -- `RiskGuard.horizon` was
    written, pinned, and called by nothing."""
    from tests.helpers import corpus_spec
    spec = corpus_spec(horizon_bars=2002)
    h = make_handle(test_so, spec)
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = Journal.open(tmp_path / "j.sqlite3", stop_marker=m)
    rc = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000, grace_ms=3_000, open_wait_ms=2_000, risk_limits={})
    c = LiveCore(h, spec, j, m, rc, LIMITS, DeadBand(0.001, 0.001, 5.0),
                 [Breaker("missed", 0.01, 500, n_min_for(0.01), 5)])
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    first = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)      # 2001/2002 -- past 80%
    assert [x["kind"] for x in first.incidents] == ["horizon_alert"] and first.stop is None
    second = c.settle(bars[2001], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)     # 2002/2002 -- exhausted
    assert second.settle is None and second.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "horizon")
    assert [x["kind"] for x in second.incidents] == ["horizon_exhausted"]
    assert c.ledger.n == 2001                       # the settle was REFUSED, not run
    assert "horizon_alert" not in [x["kind"] for x in second.incidents]   # one incident per crossing


def test_two_same_bar_triggers_are_bounded_by_their_sum(test_so, test_feed, tmp_path, monkeypatch):
    """m11: `check_position` bounded `abs(settled position) + a.qty`, so
    two same-bar pyramiding TRIGGERs on distinct intents each passed on
    their own while their SUM breached `max_abs_position`."""
    limits = RiskLimits(2.5, 1e12, 1e9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)
    c, j = _core(test_so, tmp_path, limits=limits)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    fills = [ProbeFill("L1", "ENTRY", True, 1.5, bars[2001].c, 2001, 2001),
             ProbeFill("L2", "ENTRY", True, 1.5, bars[2001].c, 2001, 2001)]
    monkeypatch.setattr(c.probe, "evaluate",
                        lambda forming, now_ms, journal=None: _probe_result(2001, forming, fills))
    fb = FormingBarBuilder(c.spec.script_tf)
    t = ticks_for(bars[2001], c.spec.script_tf)[0]
    fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
    assert [a.intent for a in ev.actions] == ["L1"]
    assert [(x["kind"], x["intent"], x["cause"]) for x in ev.incidents if x["kind"] == "risk_refused"] == [
        ("risk_refused", "L2", "max_abs_position")]


def test_a_refused_open_requote_cancels_the_advance(core, monkeypatch):
    """NEW-2: the advance was gated and emitted at `settle(n)` and is
    standing at the venue. A STOP raised between the settlement and the
    first `evaluate()` of bar n+1 refuses the requote -- and journaling
    `action_refused_by_stop` while the advance it supersedes goes on to
    fill is a record that lies. A cancel is always permitted, and under a
    STOP it is the right outcome."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert [a.kind for a in out.actions if a.kind == "MARKET_AT_OPEN"] == ["MARKET_AT_OPEN", "MARKET_AT_OPEN"]
    c.stop.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "test")
    pf = ProbeFill("L", "ENTRY", True, 2.5, bars[2001].o, 2001, 2001)     # differs from the proxy -> a real requote
    monkeypatch.setattr(c.probe, "evaluate",
                        lambda forming, now_ms, journal=None: _probe_result(2001, forming, [pf]))
    fb = FormingBarBuilder(c.spec.script_tf)
    t = ticks_for(bars[2001], c.spec.script_tf)[0]
    fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
    kinds = [(a.kind, a.intent, a.qty, a.reason) for a in ev.actions]
    assert ("CANCEL_STALE_CYCLE", "L", 0.0, "open_requote refused; the settled advance must not stand") in kinds
    assert [x["kind"] for x in ev.incidents if x["kind"] == "action_refused_by_stop"] == ["action_refused_by_stop"]
    assert ("L", "ENTRY") not in c.pending_market




def test_a_requote_does_not_spend_the_fill_action_budget(test_so, test_feed, tmp_path, monkeypatch):
    """NEW-3: a requote AMENDS an order the settlement already counted,
    gated and placed, so counting it again would make a two-leg reversal
    need a budget of 4 where spec §4's invariant is "<= P's fill count".
    At `max_fill_actions_per_bar = 1` the requote used to raise
    `RiskViolation` -> `STOP(FLAT_ONLY)` on the reversal bar. A TRIGGER
    still spends the budget."""
    limits = RiskLimits(1e6, 1e12, 1e9, 1, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)
    c, j = _core(test_so, tmp_path, limits=limits)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    fills = [ProbeFill("L", "EXIT", False, 1.0, bars[2001].o, 2000, 2001),      # the reduce-only advance, re-quoted
             ProbeFill("L", "ENTRY", True, 2.5, bars[2001].o, 2001, 2001)]      # ... and the opened leg
    monkeypatch.setattr(c.probe, "evaluate",
                        lambda forming, now_ms, journal=None: _probe_result(2001, forming, fills))
    fb = FormingBarBuilder(c.spec.script_tf)
    t = ticks_for(bars[2001], c.spec.script_tf)[0]
    fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
    assert [(a.kind, a.reason) for a in ev.actions] == [("MARKET_AT_OPEN", "open_requote")]
    assert ev.stop is None and [x for x in ev.incidents if x["kind"] == "risk_violation"] == []


def test_our_fills_reset_reads_the_venues_own_word_not_the_accumulator(core, monkeypatch):
    """NEW-4: "the venue is flat" is the VENUE's own word
    (`real_position`), not our accumulator's. Guarding the per-cycle reset
    on `|basis|` made the accumulator sticky exactly when it was wrong: a
    liquidation flattens the venue while the accumulator still carries our
    position, so `|basis|` never fell inside the band, the reset never
    fired, and every later bar re-raised `account_mismatch` until an
    operator restart re-seeded."""
    import pineforge_live.core.live as live
    c, bars, j = core
    c.seed(bars[:2000])                                   # adoption anchor: -1.0
    real_settle = c.ledger.settle
    def flat_settle(bar, now_ms):
        s = real_settle(bar, now_ms); s.position_size = 0.0; return s
    monkeypatch.setattr(c.ledger, "settle", flat_settle)
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [])
    liq = VenueFill(None, None, T.Side.BUY, 1.0, bars[2000].c, 2000, T.FillCause.LIQUIDATION, None)
    c.settle(bars[2000], [liq], set(), set(), 0.0, 0)
    assert c.our_signed_fills == 0.0                      # the venue says flat, so the cycle really is over
    out = c.settle(bars[2001], [], set(), set(), 0.0, 0)
    assert out.reconcile.counters.get("account_mismatch") is None


def test_seed_anchors_the_basis_on_the_venue_and_refuses_a_disagreement(test_so, test_feed, tmp_path):
    """m6, spec §6: "cold start with an existing position -> refuse unless
    `--adopt-position`". Anchoring the fallback basis on the RECOMPUTE
    means a restart can reach a STOP the uninterrupted run never raises: a
    legitimate hold-flat state (MIRROR_EARLY -- venue 0, ledger +1, no
    STOP) comes back as basis +1 against a real 0, i.e. `account_mismatch`
    on the very first settlement of a state the run was carrying happily."""
    bars = load_bars(test_feed, 2300)
    c, j = _core(test_so, tmp_path)
    out = c.seed(bars[:2000], real_position=-1.0)         # the venue agrees with the recompute
    assert out.settle is not None and c.our_signed_fills == -1.0 and out.stop is None

    refused_dir = tmp_path / "refused"; refused_dir.mkdir()
    c2, _ = _core(test_so, refused_dir)
    out = c2.seed(bars[:2000], real_position=0.0)         # the venue is flat, the ledger is short
    assert out.settle is None
    assert out.stop == (T.StopLevel.HARD, T.StopDisposition.HOLD, "cold_start_position")
    assert [x for x in out.incidents if x["kind"] == "cold_start_position"]

    adopted_dir = tmp_path / "adopted"; adopted_dir.mkdir()
    c3, _ = _core(test_so, adopted_dir)
    out = c3.seed(bars[:2000], real_position=0.0, adopt_position=True)
    assert out.settle is not None and out.stop is None and c3.our_signed_fills == 0.0


def test_a_synthetic_close_without_an_exit_id_carries_a_stable_intent(core, monkeypatch):
    """n8: a margin-call close books `exit_id == ""`, and B3 hashes the
    intent into the order's client id -- an empty one collides across
    every such close."""
    import pineforge_live.core.live as live
    from pineforge_live.core.classify import CLOSE_CAUSE_MARGIN_CALL
    c, bars, j = core
    c.seed(bars[:2000])
    mc = ClassifiedFill(FillClass.SYNTHETIC,
                        EmulatedFill("", "EXIT", True, 1.0, bars[2000].c, 2000, CLOSE_CAUSE_MARGIN_CALL), None, 1.0, "")
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [mc])
    out = c.settle(bars[2000], [], set(), set(), 1.0, 0, our_signed_fills=1.0)
    syn = [a for a in out.actions if a.kind == "SYNTHETIC_CLOSE"]
    assert [(a.intent, a.reduce_only) for a in syn] == [("__synthetic__", True)]


def test_the_settled_books_transitions_are_journaled(core):
    """n9, spec §6: the `intents` table. The settled book and its diff are
    the core's own product -- nothing else derives them -- and B3 needs the
    durable copy to re-run a journaled STOP's cancel step on restart
    without first recomputing the ledger to find out what was resting."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    rows = {r["intent_key"]: r for r in j.rows("intents", "epoch_hash=?", (c.spec.epoch_hash(),))}
    assert set(rows) == set(out.book_diff)
    for key, state in out.book_diff.items():
        assert rows[key]["state"] == state.value
        payload = json.loads(rows[key]["payload_json"])
        it = out.book.get(key)
        assert (payload == {} if it is None else payload["content_hash"] == it.content_hash)
    # a later transition of the same key REPLACES the row (latest state)
    out2 = c.settle(bars[2001], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    rows2 = {r["intent_key"]: r["state"] for r in j.rows("intents", "epoch_hash=?", (c.spec.epoch_hash(),))}
    for key, state in out2.book_diff.items():
        assert rows2[key] == state.value


def test_the_mirror_early_day_tally_survives_a_restart(test_so, test_feed, tmp_path, monkeypatch):
    """n12: `mirror_early_today` restarted at 0 on every process start, so
    the per-day cap was launderable by bouncing the runtime -- the exact
    thing the cap exists to prevent. It is re-derived from today's
    journaled `reconciles` rows at construction."""
    import pineforge_live.core.live as live
    c, j = _core(test_so, tmp_path)
    bars = load_bars(test_feed, 2300)
    c.seed(bars[:2000])
    early = ClassifiedFill(FillClass.MIRROR_EARLY, None,
                           VenueFill("x", "EXIT", T.Side.SELL, 1.0, bars[2000].c, 2000, T.FillCause.OURS, "c"), 1.0, "")
    monkeypatch.setattr(live, "classify_bar", lambda *a, **k: [early])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert c.mirror_early_today == 1
    reborn = LiveCore(c.h, c.spec, j, c.marker, c.rc, LIMITS, DeadBand(0.001, 0.001, 5.0),
                      [Breaker("missed", 0.01, 500, n_min_for(0.01), 5)])
    assert reborn.mirror_early_today == 1
