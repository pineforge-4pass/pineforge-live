import asyncio
import dataclasses
from types import SimpleNamespace
import pytest
from pineforge_live import types as T
from pineforge_live.core.book import settled_book
from pineforge_live.core.ledger import Ledger
from pineforge_live.core.probe import Probe, path_order_other
from pineforge_live.engine.handle import PATH_ORDER_HIGH_FIRST, PATH_ORDER_LOW_FIRST
from pineforge_live.engine.report import RunResult, TradeRow
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
        # The pre-bar book, same input the probe resolved against -- read
        # from L.last.book (captured at settle time), NOT a fresh
        # settled_book(h, L.last) call: the handle's last run by this point
        # is one of THIS bar's own probe evaluate() calls above, not the
        # settlement that produced L.last, so a re-read here would now
        # raise (settled_book's restored strict contract, Task 6 prelim).
        book_before = L.last.book
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
    strategy) must fail loudly (Task 6 prelim: L5 restores the strict
    `RuntimeError`, since `book` is now only ever built inside the
    settle/seed accessor window) rather than silently returning a
    plausible-but-wrong Intent -- verified empirically (pre-fix) to
    manifest as the STOP/LIMIT VALUES THEMSELVES reading wrong (the fill
    at 2005 changes the mirror's index assignment, so a stale index reads
    a different order's levels entirely), not merely `level_resolved`.
    `r.levels`/`r2.levels`/`r3.levels` (evaluate()'s own internal book,
    now sourced from `self.L.last.book` -- captured once, at settle time,
    and therefore IDENTICAL across every evaluate() call on the same bar)
    must still match the pre-probe book exactly, on both fixtures'
    (stop, limit, activation)."""
    spec = corpus_spec_bracket(); h = make_handle(test_so_bracket, spec); j, _ = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    bars = load_bars(test_feed, 2010)
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2005])
    pre_probe_book = settled_book(h, L.last)

    P = Probe(h, spec, L, trail_refresh_policy="bar_open_level")
    forming = dataclasses.replace(bars[2005], is_forming=True)
    r = P.evaluate(forming, now_ms=forming.ts_open)  # runs P_auto (and P_other if it fills anything)
    _assert_matches_pre_probe_book(r.levels, pre_probe_book)

    # Discriminating check (finding 5, now L5's restored raise): a
    # wrong-order read (through the handle's now-stale live strategy, left
    # over from the probe run(s) above) must raise RuntimeError -- if it
    # doesn't, this bar has stopped exercising the hazard and the pin is
    # vacuous again.
    with pytest.raises(RuntimeError):
        settled_book(h, L.last)

    # Fixed by this prelim (previously a documented concern): a SECOND
    # evaluate() call on this SAME forming bar (2005, where P_auto's run
    # just filled the entry and restructured the mirror's pending-order
    # set) must STILL reproduce pre_probe_book -- Probe.evaluate() now
    # reads `self.L.last.book` (captured once, at settle time) instead of
    # recomputing settled_book(self.h, self.L.last) at the top of every
    # call, so it no longer matters how many probe runs have since
    # replaced the handle's live strategy.
    r2 = P.evaluate(forming, now_ms=forming.ts_open + 1)
    _assert_matches_pre_probe_book(r2.levels, pre_probe_book)

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


def test_same_sig_fills_pair_by_ordinal_not_collapse(monkeypatch):
    """N3 pin (task-4 re-review): two fills sharing (intent, leg, is_long)
    on the same bar -- e.g. two pyramided legs closed together by ONE
    shared exit id -- must pair with their P_other counterpart by ORDINAL
    position, not collapse onto a single `{sig: ProbeFill}` dict slot
    (which drops one fill's identity and compares an arbitrary qty pair).
    No engine needed: `Probe._run` is monkeypatched to return two canned,
    synthetic `RunResult`s (a P_auto and a P_other) whose trades both
    close bar `n` under the shared exit id "XL" -- P_other confirms the
    FIRST leg's qty exactly (1.0) and disagrees on the SECOND's (2.5 vs
    2.0). Keyed by sig alone, `other_by_sig` (a plain dict comprehension)
    collapses to ONLY the second (qty 2.5) row -- last write wins -- so
    the first auto fill would spuriously read `qty_disagreement=True`
    (1.0 vs 2.5) while its correct, exact-matching counterpart is lost."""
    n = 10

    def trow(qty: float, entry_bar: int) -> TradeRow:
        return TradeRow(entry_time=0, exit_time=0, entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0,
                        is_long=True, qty=qty, commission=0.0, entry_bar_index=entry_bar, exit_bar_index=n,
                        open_at_end=False, entry_id=f"E{entry_bar}", exit_id="XL", exit_comment="", close_cause=0)

    def run_result(trades: list[TradeRow], position_size: float) -> RunResult:
        return RunResult(status=0, trades=trades, net_profit=0.0, script_bars_processed=n + 1, broker_state_hash=[],
                         position_size=position_size, position_avg_price=float("nan"), position_cycle_seq=0,
                         trail_best_price=float("nan"), current_equity=0.0, last_bar_dual_entry_path=-1, pending_orders=[])

    prev_position_size = 3.0
    # Auto: two legs sum to 3.0 (matches the ledger's last position exactly
    # -> position_size 0.0, no _delta_fill residual). Other: the SAME two
    # legs but the second reads 2.5 instead of 2.0 -- position_size is
    # adjusted to -0.5 so THAT run's own residual is also fully explained
    # (isolating the sig-collapse bug from _delta_fill's unrelated logic).
    canned = [run_result([trow(1.0, 5), trow(2.0, 6)], position_size=0.0),
              run_result([trow(1.0, 5), trow(2.5, 6)], position_size=-0.5)]
    calls = {"n": 0}
    def fake_run(bars, path_order):
        calls["n"] += 1
        return canned[calls["n"] - 1]

    fake_ledger = SimpleNamespace(last=SimpleNamespace(book={}, position_size=prev_position_size), n=n, bars=[])
    forming = T.NormalizedBar(0, 100.0, 105.0, 95.0, 100.0, 1.0, 1, is_forming=True)

    P = Probe(handle=None, spec=None, ledger=fake_ledger, trail_refresh_policy="bar_open_level")
    monkeypatch.setattr(P, "_run", fake_run)

    r = P.evaluate(forming, now_ms=0, journal=None)
    assert len(r.fills) == 2
    by_qty = sorted(r.fills, key=lambda f: f.qty)
    assert by_qty[0].qty == pytest.approx(1.0) and not by_qty[0].qty_disagreement
    assert by_qty[1].qty == pytest.approx(2.0) and by_qty[1].qty_disagreement
