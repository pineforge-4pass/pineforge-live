"""`LiveCore` (spec §4 settle 1-8 / evaluate 1-4, §5.4, §5.5) and the L1
mini-harness. Venue-neutral throughout: every venue name is `"TAPE"`."""
import asyncio, pytest
from pineforge_live import types as T
from pineforge_live.core.live import ActionRequest, LiveCore
from pineforge_live.core.reconcile import DeadBand, ReconcileDecision
from pineforge_live.core.riskguard import RiskLimits, Breaker, n_min_for
from pineforge_live.core.classify import VenueFill
from pineforge_live.epoch import RuntimeConfig
from pineforge_live.adapters.tape import TapeTickSource
from pineforge_live.bars import FormingBarBuilder
from pineforge_live.journal import Journal, StopMarker
from tests.helpers import load_bars, make_handle, corpus_spec

LIMITS = RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)

def _core(test_so, tmp_path, limits=LIMITS):
    spec = corpus_spec(); h = make_handle(test_so, spec)
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = Journal.open(tmp_path / "j.sqlite3", stop_marker=m)
    rc = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000, grace_ms=3_000, open_wait_ms=2_000, risk_limits={})
    c = LiveCore(h, spec, j, m, rc, limits, DeadBand(0.001, 0.001, 5.0), [Breaker("orphan", 0.01, 500, n_min_for(0.01), 5)])
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
    """Perfect venue: every settle-emitted/probe fill is echoed back as a venue fill → all CONFIRMED, no STOP, G1 holds."""
    c, bars, j = core
    out = c.seed(bars[:2000]); assert out.settle is not None and c.stop.level == T.StopLevel.NONE
    pending_actions: list = []
    for i in range(2000, 2060):
        bar = bars[i]; fb = FormingBarBuilder(c.spec.script_tf)
        for t in ticks_for(bar, c.spec.script_tf):
            fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
            pending_actions += ev.actions
        # a perfect venue fills every requested action at its hint on this bar
        venue = [VenueFill(a.intent, "ENTRY" if a.kind in ("TRIGGER", "MARKET_AT_OPEN") and not a.reduce_only else "EXIT", a.side, a.qty,
                           a.price_hint or bar.c, i, T.FillCause.OURS, f"cid{k}", a.kind == "TRIGGER") for k, a in enumerate(pending_actions)]
        real = c.ledger.last.position_size
        out = c.settle(bar, venue_fills=venue, in_flight=set(), mirrored=set(), real_position=real, now_ms=bar.ts_open + 900_000)
        pending_actions = [a for a in out.actions if a.kind in ("MARKET_AT_OPEN", "SYNTHETIC_CLOSE")]
        assert out.stop is None, (i, out.stop)
        assert not [x for x in out.reconcile.corrections if x.kind == "MARKET_CORRECT"], (i, out.reconcile)
    assert c.stop.level == T.StopLevel.NONE
    assert j.last_settlement(c.spec.epoch_hash())["bar_index"] == 2059

def test_g1_break_raises_hard_hold(core, monkeypatch):
    c, bars, j = core; c.seed(bars[:2000])
    j._exec("UPDATE settlements SET broker_state_hash=? WHERE bar_index=1999", (f"{7:016x}",))
    out = c.settle(bars[2000], [], set(), set(), 0.0, 0)
    assert out.stop is not None and out.stop[0] == T.StopLevel.HARD and out.stop[1] == T.StopDisposition.HOLD
    assert c.stop.level == T.StopLevel.HARD and c.marker.exists()

def test_stop_refuses_triggers(core):
    c, bars, j = core; c.seed(bars[:2000])
    c.stop.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "test")
    fb = FormingBarBuilder(c.spec.script_tf)
    for t in ticks_for(bars[2000], c.spec.script_tf):
        fb.push(t); ev = c.evaluate(fb.forming(), now_ms=t.ts)
        assert all(a.reduce_only for a in ev.actions)


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
    with an empty venue, against a basis on the OPPOSITE side."""
    from pineforge_live.core.classify import FillClass
    c, bars, j = core
    c.seed(bars[:2000])
    c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    out = c.settle(bars[2001], [], set(), set(), -3.0, 0, our_signed_fills=-3.0)
    assert out.settle.position_size == 1.0
    assert [x.cls for x in out.classified].count(FillClass.MISSED) == 2
    assert not [x for x in out.reconcile.corrections if x.kind == "MARKET_CORRECT"]
    assert out.reconcile.counters.get("skipped_position_mismatch") == 1

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
    emitted for bar n+1 suppresses the probe's own TRIGGER for the same
    (intent, leg) on that bar, and a TRIGGER is emitted at most once per
    (intent, leg) per bar however many ticks evaluate() sees."""
    c, bars, j = core
    c.seed(bars[:2000])
    out = c.settle(bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert {(a.intent, a.reduce_only) for a in out.actions if a.kind == "MARKET_AT_OPEN"} == {("L", True), ("L", False)}
    fb = FormingBarBuilder(c.spec.script_tf); emitted = []
    for t in ticks_for(bars[2001], c.spec.script_tf):
        fb.push(t); emitted += c.evaluate(fb.forming(), now_ms=t.ts).actions
    assert [a for a in emitted if a.intent == "L"] == []

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

def test_stale_account_snapshot_is_forwarded_not_escalated(core):
    """A polled account position is not transactionally consistent with the
    fill stream: when the reported snapshot is short by exactly this
    settlement's own OURS fills, LiveCore forwards it (journaling
    `account_snapshot_stale`) instead of escalating -- spec §5.4 wants the
    snapshot event-time AFTER the last fill. Bar 2001 is the corpus
    reversal, so the two MARKET legs settle(2000) asked for both fill."""
    c, bars, j = core
    c.seed(bars[:2000])
    legs = [a for a in c.settle(bars[2000], [], set(), set(), -1.0, 0).actions if a.kind == "MARKET_AT_OPEN"]
    out = c.settle(bars[2001], _echo(legs, bars[2001], 2001), set(), set(), -1.0, 0)
    assert out.stop is None and out.settle.position_size == 1.0 and c.our_signed_fills == 1.0
    stale = [x for x in out.incidents if x["kind"] == "account_snapshot_stale"]
    assert stale and stale[0]["reported"] == -1.0 and stale[0]["forwarded"] == 1.0 and stale[0]["basis"] == 1.0

def test_account_disagreement_beyond_this_bars_fills_still_escalates(core):
    """The tolerance above is narrow: a gap the settlement's own fills do
    not explain is still `STOP(FLAT_ONLY)` with cause `account_mismatch`."""
    c, bars, j = core
    c.seed(bars[:2000])
    legs = [a for a in c.settle(bars[2000], [], set(), set(), -1.0, 0).actions if a.kind == "MARKET_AT_OPEN"]
    out = c.settle(bars[2001], _echo(legs, bars[2001], 2001), set(), set(), 9.0, 0)
    assert out.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "account_mismatch")
    assert not [x for x in out.incidents if x["kind"] == "account_snapshot_stale"]
