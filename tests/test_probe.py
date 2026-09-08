import asyncio
import dataclasses
import pytest
from pineforge_live import types as T
from pineforge_live.core.book import settled_book
from pineforge_live.core.ledger import Ledger
from pineforge_live.core.probe import Probe, path_order_other
from pineforge_live.engine.handle import PATH_ORDER_HIGH_FIRST, PATH_ORDER_LOW_FIRST
from pineforge_live.adapters.tape import TapeTickSource
from pineforge_live.bars import FormingBarBuilder
from tests.helpers import load_bars, make_handle, corpus_spec, corpus_spec_bracket, open_journal


def test_path_order_other_is_the_opposite_of_auto():
    b = T.NormalizedBar(0, 100, 105, 90, 99, 1, 1)     # |H-O|=5 < |O-L|=10 -> auto high-first -> other = LOW_FIRST
    assert path_order_other(b) == PATH_ORDER_LOW_FIRST
    b2 = T.NormalizedBar(0, 100, 110, 95, 99, 1, 1)    # auto low-first -> other = HIGH_FIRST
    assert path_order_other(b2) == PATH_ORDER_HIGH_FIRST
    # N9: the tie case. Engine AUTO (engine_path_resolve.cpp:36,
    # bar_path_uses_high_first) is `|H-O| < |O-L|` -- a strict `<`, so a tie
    # (|H-O| == |O-L|) evaluates False -> the engine ties to LOW_FIRST.
    # `path_order_other` must therefore return the opposite: HIGH_FIRST.
    b3 = T.NormalizedBar(0, 100, 110, 90, 99, 1, 1)    # |H-O|=10 == |O-L|=10 -> auto ties low-first -> other = HIGH_FIRST
    assert path_order_other(b3) == PATH_ORDER_HIGH_FIRST


# (so_fixture_name, spec_factory) -- request.getfixturevalue(name) below fetches
# only the fixture the running param needs, so the OTHER probe's .so being
# unbuilt never skips a param that doesn't need it.
_PROBES = [("test_so", corpus_spec), ("test_so_bracket", corpus_spec_bracket)]


@pytest.fixture(params=_PROBES, ids=["sma", "bracket"])
def env(request, test_feed, tmp_path):
    so_fixture_name, spec_factory = request.param
    so = request.getfixturevalue(so_fixture_name)
    spec = spec_factory()
    h = make_handle(so, spec); j, _ = open_journal(tmp_path); j.append_epoch(spec.epoch_hash(), "{}")
    bars = load_bars(test_feed, 2400); L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    return spec, h, j, bars, L


def _resolve_entry_fill_intent(ef: dict, book) -> str:
    """Test-local mirror of `probe._resolve_intent`, applied to one of
    `SettleResult.entry_fills`' plain dicts (the ledger itself leaves
    `intent` unresolved -- `None` -- since it has no book access; the probe
    resolves it via the SAME rule against the pre-bar settled book, so this
    lets the L1 pin compare probe fills and settled entry_fills like with
    like)."""
    candidates = [it for it in book.values() if it.kind in ("ENTRY", "MARKET", "RAW_ORDER") and it.is_long == ef["is_long"]]
    return min(candidates, key=lambda it: it.index).key.order_id if candidates else "?"


def test_probe_fills_subset_of_settlement_over_50_bars(env, request):
    """L1 probe ≡ recompute at k=4 points: every probe fill is either a settlement fill of that bar or retracted.

    "Settlement fill" now covers BOTH halves of a bar's settlement (review
    finding 1's fix): the closed-trade fills (`new_opened`/`new_closed`,
    same-bar round trips) AND the ledger's `entry_fills` (opens/adds/
    reversal-opens/partial-reduces that never appear as a closed trade),
    resolved to an intent via the SAME pre-bar book the probe itself uses
    -- so the pin is no longer blind to opening entries, and on the
    bracket fixture (ATR stop/target: `strategy.entry`/`strategy.exit`)
    the window includes bar 2005's 0 -> +1 open (finding 6)."""
    spec, h, j, bars, L = env
    is_bracket = "bracket" in request.node.callspec.id
    P = Probe(h, spec, L, trail_refresh_policy="bar_open_level")
    total_probe, total_settle, retracts, entry_probe_fills = 0, 0, 0, 0
    for i in range(2000, 2050):
        bar = bars[i]
        src = TapeTickSource([bar], spec.script_tf, policy="path4", seed=1); fb = FormingBarBuilder(spec.script_tf)
        probe_fills_this_bar = set()

        async def ticks():
            return [e.tick async for e in src.subscribe(T.InstrumentId("TAPE", T.MarketType.PERP, "ETHUSDT"), 0) if isinstance(e, T.Tick)]

        for t in asyncio.run(ticks()):
            fb.push(t); r = P.evaluate(fb.forming(), now_ms=t.ts, journal=j)
            assert r.bar_index == i
            probe_fills_this_bar |= {(f.intent, f.leg) for f in r.fills}
            entry_probe_fills += sum(1 for f in r.fills if f.leg == "ENTRY")
            retracts += len(r.retracted)
        book_before = settled_book(h, L.last)   # the pre-bar book, same input the probe resolved against
        s = L.settle(bar, now_ms=bar.ts_open + 900_000)
        settled = {(k.entry_id, "ENTRY") for k in s.new_opened} | {(k.exit_id, "EXIT") for k in s.new_closed}
        settled |= {(_resolve_entry_fill_intent(ef, book_before), ef["leg"]) for ef in s.entry_fills}
        total_probe += len(probe_fills_this_bar); total_settle += len(settled)
        # a probe fill that the settlement did not book must have been retracted by a later evaluate
        for f in probe_fills_this_bar - settled:
            assert any(x.intent == f[0] and x.leg == f[1] for x in P.retracted_history[i]), (i, f)
    assert total_settle >= 1, "the window must contain at least one settled fill to be a meaningful test"
    if is_bracket:
        assert entry_probe_fills >= 1, \
            "expected >=1 ENTRY probe fill in 2000-2049 on the bracket fixture (bar 2005's 0->+1 open, finding 6)"
    assert j.rows("evaluations", "outcome=?", ("ran",))


def _assert_matches_pre_probe_book(levels: dict, pre_probe_book: dict) -> None:
    assert set(levels.keys()) == set(pre_probe_book.keys())
    for key, it in pre_probe_book.items():
        assert levels[key] == (it.stop, it.limit, it.activation)


def test_book_captured_before_probe_run(test_so_bracket, test_feed, tmp_path):
    """settled_book(self.h, self.L.last) must run BEFORE any probe run_full
    (EngineHandle's accessors reflect only the last run) -- and evaluate()
    must keep returning levels keyed by the settled intents even after two
    probe runs (P_auto + P_other) have since replaced the handle's live
    strategy.

    Finding 5 (test, m5): the ORIGINAL version of this test used the sma
    fixture, whose sole intent is a MARKET order with `(stop, limit,
    activation) == (None, None, None)` under BOTH the right and the wrong
    ordering, so a deliberately wrong-order read never differed from the
    correct one in any way this test could see, and the test could not
    fail on the hazard it names. Pinned here on the bracket fixture (ATR
    stop/target: real numeric stop/limit levels) at bars 2005/2006 -- 2005
    is where the position opens (0 -> +1, finding 6: the probe's ENTRY
    fill for this bar is also exercised here) and 2006 is where the
    resulting bracket actually rests with resolved (non-degenerate)
    levels.

    At bar 2005 this test independently reproduces the review's own
    verification method: a wrong-order `settled_book` read (called AFTER
    evaluate()'s probe run(s), through the handle's by-then-stale live
    strategy) must diverge from the correct pre-probe Intent objects --
    verified empirically to manifest as the STOP/LIMIT VALUES THEMSELVES
    reading wrong (the fill at 2005 changes the mirror's index assignment,
    so a stale index reads a different order's levels entirely -- a more
    severe case of the same hazard than a bare `level_resolved` flip), not
    merely `level_resolved`, so the assertion checks full Intent
    inequality rather than pinning one specific field. `r.levels`/
    `r2.levels` (evaluate()'s own internal book, captured the RIGHT way)
    must still match the pre-probe book exactly, on both fixtures'
    (stop, limit, activation) AND (independently, via direct `Intent`
    comparison below) `level_resolved`."""
    spec = corpus_spec_bracket(); h = make_handle(test_so_bracket, spec); j, _ = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    bars = load_bars(test_feed, 2010)
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2005])
    pre_probe_book = settled_book(h, L.last)

    P = Probe(h, spec, L, trail_refresh_policy="bar_open_level")
    forming = dataclasses.replace(bars[2005], is_forming=True)
    r = P.evaluate(forming, now_ms=forming.ts_open)  # runs P_auto (and P_other if it fills anything)
    _assert_matches_pre_probe_book(r.levels, pre_probe_book)

    # Discriminating check (finding 5): the wrong-order read (through the
    # handle's now-stale live strategy, left over from the probe run(s)
    # above) must actually diverge from the correct pre-probe book on full
    # Intent equality (level_resolved included) -- if it doesn't, this bar
    # has stopped exercising the hazard and the pin is vacuous again.
    wrong_order_book = settled_book(h, L.last)
    assert wrong_order_book.keys() == pre_probe_book.keys()
    assert wrong_order_book != pre_probe_book, "fixture/bar no longer exercises the book-staleness hazard"

    # NOTE (concern, not a finding this fix wave covers): a second
    # evaluate() call on this SAME forming bar (2005, where P_auto's run
    # just filled the entry and restructured the mirror's pending-order
    # set) does NOT reproduce pre_probe_book -- settled_book(self.h,
    # self.L.last) is recomputed fresh at the top of every evaluate() call,
    # but by the second call self.h's LAST run is already the first call's
    # own probe run, not the settlement that produced self.L.last, so the
    # indices it reads are for a DIFFERENT (probe) run's mirror. This is
    # exactly the steady-state staleness book.py's own docstring documents
    # ("after a single Probe.evaluate(), a previously-valid mirror index
    # reads ... unresolved ... this is the STEADY STATE for a probe issuing
    # multiple evaluate() calls per bar") -- degrading gracefully to
    # None/unresolved on the sma fixture (never observably wrong there,
    # which is exactly why the ORIGINAL version of this test, and its
    # "second evaluate() must still agree" claim, never caught it) but, on
    # THIS fixture/bar (a real fill restructuring the order count),
    # observably reading a DIFFERENT order's real stop/limit values under
    # the stale index -- not merely degrading to None. Confirmed this is
    # about the fill, not "any second call": bar 2006 below (no fill) DOES
    # keep agreeing across repeated evaluate() calls. Flagged in the fix
    # report as a pre-existing concern (present before and after this fix
    # wave, on both HEAD~1 and HEAD) rather than fixed here -- it is not
    # one of M1-M3/m4-m5/n7-n10/created_now, and a fix belongs with
    # `SettleResult.book` (ledger.py's own in-flight capture-once-after-
    # the-producing-run field), out of this fix wave's probe.py/
    # tests/test_probe.py-only scope.

    # Now settle bar 2005 for real (the position opens) and re-pin at bar
    # 2006, where the ATR bracket's own stop/target leg is resting with
    # real (non-degenerate, level_resolved=True) levels -- a correctness
    # check on genuinely meaningful values, complementing 2005's
    # discrimination check.
    L.settle(bars[2005], now_ms=bars[2005].ts_open + 900_000)
    pre_probe_book_2006 = settled_book(h, L.last)
    assert pre_probe_book_2006, "expected >=1 resting bracket intent after the position opens at bar 2005"
    assert any(it.level_resolved and it.stop is not None for it in pre_probe_book_2006.values()), \
        "expected a real resolved stop/limit level at bar 2006 -- the non-degenerate case finding 5 needs"

    forming_2006 = dataclasses.replace(bars[2006], is_forming=True)
    r3 = P.evaluate(forming_2006, now_ms=forming_2006.ts_open)
    _assert_matches_pre_probe_book(r3.levels, pre_probe_book_2006)

    # No fill happens at 2006 (see the L1 pin / finding 6), so the mirror's
    # pending-order set is unchanged across the probe's own run(s) here --
    # unlike 2005 above, a second evaluate() call on this bar DOES still
    # agree, positively confirming the staleness above is fill-triggered.
    r4 = P.evaluate(forming_2006, now_ms=forming_2006.ts_open + 1)
    _assert_matches_pre_probe_book(r4.levels, pre_probe_book_2006)
