"""`LiveCore` (spec §4 settle 1-8 / evaluate 1-4, §5.4, §5.5) and the L1
mini-harness. Venue-neutral throughout: every venue name is `"TAPE"`."""
import asyncio, pytest
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
    emitted for bar n+1 replaces the probe's own TRIGGER for the same
    (intent, leg) on that bar with ONE superseding MARKET_AT_OPEN carrying
    the engine's open-price qty (F6, `reason="open_requote"`), and that is
    emitted at most once per (intent, leg) per bar however many ticks
    evaluate() sees."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert {(a.intent, a.reduce_only) for a in out.actions if a.kind == "MARKET_AT_OPEN"} == {("L", True), ("L", False)}
    fb = FormingBarBuilder(c.spec.script_tf); emitted = []
    for t in ticks_for(bars[2001], c.spec.script_tf):
        fb.push(t); emitted += c.evaluate(fb.forming(), now_ms=t.ts).actions
    assert [a.kind for a in emitted if a.intent == "L"] == ["MARKET_AT_OPEN", "MARKET_AT_OPEN"]
    assert {(a.reduce_only, a.reason, a.target_bar_index) for a in emitted if a.intent == "L"} == {
        (True, "open_requote", 2001), (False, "open_requote", 2001)}

def test_cancel_stale_cycle_never_fires_for_an_intent_that_filled(core):
    """`book_diff` reads CANCELLED for an order that FILLED as well as one
    that was cancelled (see `IntentState`); LiveCore disambiguates with the
    bar's own emulated fills, so a filled MARKET never produces a
    CANCEL_STALE_CYCLE."""
    c, bars, j = core
    c.seed(bars[:2000])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    out = c.settle(bars[2001], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    from pineforge_live.core.book import IntentState
    assert out.book_diff == {"L|MARKET||54": IntentState.CANCELLED}
    assert [a for a in out.actions if a.kind == "CANCEL_STALE_CYCLE"] == []

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
    assert [(a.kind, a.intent, a.qty, a.reduce_only, a.reason, a.target_bar_index) for a in emitted] == [
        ("MARKET_AT_OPEN", "L", 2.5, False, "open_requote", 2001)]

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
