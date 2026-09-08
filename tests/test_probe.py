import asyncio
import dataclasses
from types import SimpleNamespace
import pytest
from pineforge_live import types as T
from pineforge_live.core.book import book_diff, settled_book
from pineforge_live.core.classify import emulated_from_settle
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


def _settled_fill_ids(s, book_before) -> set:
    """The `(intent, leg)` pairs the SETTLEMENT itself books for its bar --
    `classify.emulated_from_settle` over the settlement's own book diff,
    i.e. the ledger side of the ONE attribution rule (m4). The probe
    resolves its own delta fills by the same rule against the same
    departure signal, so the L1 pin compares like with like instead of
    re-deriving the ledger's ids with a test-local copy of a rule that can
    drift from both."""
    return {(e.intent, e.leg) for e in emulated_from_settle(s, book_diff(book_before, s.book), book_before)}


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
        settled = _settled_fill_ids(s, book_before)
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


def test_dropped_entry_journals_an_incident(monkeypatch):
    """N11 pin (task-4 `created_now` PARTIAL carry, task-6 review finding
    9): each ENTRY-leg fill `_drop_unresting_entries` drops -- its intent
    was not resting in the pre-run settled book -- must journal a
    `probe_dropped_entry` incident when a journal is given. No engine
    needed: `Probe._run` is monkeypatched to return one canned `RunResult`
    with NO closed trades, whose position delta (0.0 -> 1.0) `_delta_fill`
    explains as one ENTRY delta fill -- `_resolve_intent` on the (empty)
    pre-run book resolves it to `"?"`, which is never resting, so
    `_drop_unresting_entries` drops it; `auto_fills` is then empty, so the
    flow never needs a P_other run."""
    n = 20

    def run_result() -> RunResult:
        return RunResult(status=0, trades=[], net_profit=0.0, script_bars_processed=n + 1, broker_state_hash=[],
                         position_size=1.0, position_avg_price=100.0, position_cycle_seq=0,
                         trail_best_price=float("nan"), current_equity=0.0, last_bar_dual_entry_path=-1, pending_orders=[])

    fake_ledger = SimpleNamespace(last=SimpleNamespace(book={}, position_size=0.0), n=n, bars=[])
    fake_spec = SimpleNamespace(epoch_hash=lambda: "epoch")
    forming = T.NormalizedBar(0, 100.0, 105.0, 95.0, 100.0, 1.0, 1, is_forming=True)

    incidents = []
    fake_journal = SimpleNamespace(append_incident=lambda kind, detail: incidents.append((kind, detail)),
                                   append_evaluation=lambda row: None)

    P = Probe(handle=None, spec=fake_spec, ledger=fake_ledger, trail_refresh_policy="bar_open_level")
    monkeypatch.setattr(P, "_run", lambda bars, path_order: run_result())

    r = P.evaluate(forming, now_ms=123, journal=fake_journal)
    assert r.fills == [] and len(r.dropped) == 1 and r.dropped[0].intent == "?" and r.dropped[0].leg == "ENTRY"
    assert incidents == [("probe_dropped_entry", {"bar_index": n, "intent": "?", "leg": "ENTRY",
                                                   "is_long": True, "qty": 1.0, "price": 100.0, "now_ms": 123})]

    # no journal given -- must not raise, and obviously nothing recorded.
    P2 = Probe(handle=None, spec=fake_spec, ledger=fake_ledger, trail_refresh_policy="bar_open_level")
    monkeypatch.setattr(P2, "_run", lambda bars, path_order: run_result())
    r2 = P2.evaluate(forming, now_ms=123, journal=None)
    assert len(r2.dropped) == 1


def test_intrabar_best_levels_pin_at_bracket_bar_2044(test_so_bracket, test_feed, tmp_path):
    """M2 pin (task-4 PARTIAL carry, task-6 review finding 9): the
    `intrabar_best` trail policy's `probe_book` capture (immediately after
    P_auto, before P_other runs -- see `evaluate()`'s M2 comment) on the
    bracket fixture at bar 2044, where the bracket's EXIT leg ("XL")
    fills intrabar and P_other confirms it (tick 3 of 4, `path4` policy,
    empirically verified against the real engine): every settled intent
    key must still be present in `levels`, and XL's own refreshed
    (stop, limit, activation) must read the ATR bracket's real numbers,
    not the bar-open-level's stale ones (both policies happen to agree at
    this exact bar/tick, per the task-6 review, but only `intrabar_best`
    is pinned here -- `bar_open_level` already has its own pin at
    2005/2006 in `test_book_captured_before_probe_run`)."""
    spec = corpus_spec_bracket(); h = make_handle(test_so_bracket, spec); j, _ = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    bars = load_bars(test_feed, 2050)
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    for i in range(2000, 2044):
        L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)

    P = Probe(h, spec, L, trail_refresh_policy="intrabar_best")
    bar = bars[2044]
    src = TapeTickSource([bar], spec.script_tf, policy="path4", seed=1)
    fb = FormingBarBuilder(spec.script_tf)

    async def ticks():
        return [e.tick async for e in src.subscribe(T.InstrumentId("TAPE", T.MarketType.PERP, "ETHUSDT"), 0) if isinstance(e, T.Tick)]

    r = None
    for t in asyncio.run(ticks()):
        fb.push(t)
        r = P.evaluate(fb.forming(), now_ms=t.ts, journal=j)
        if any(f.intent == "XL" and f.leg == "EXIT" for f in r.fills):
            break

    assert r is not None and r.p_other_ran
    assert [(f.intent, f.leg) for f in r.fills if f.intent == "XL"] == [("XL", "EXIT")]
    assert set(r.levels.keys()) == set(L.last.book.keys())
    xl_key = next(k for k in r.levels if k.startswith("XL|"))
    stop, limit, activation = r.levels[xl_key]
    assert stop == pytest.approx(169.1312, abs=1e-4)
    assert limit == pytest.approx(170.9484, abs=1e-4)
    assert activation is None


def test_delta_fill_attributes_the_order_that_left_the_book_not_the_lowest_index(monkeypatch):
    """m4 (ONE attribution rule): with two same-side priced entries resting
    -- a breakout stop `L1` at mirror index 0 and a pullback limit `L2` at
    index 1, the shape a Pine script produces routinely -- and only `L2`
    filling, the probe must attribute the position delta to `L2`, the row
    that LEFT the book on that side (`classify.delta_candidates` over the
    probe run's own pending-order mirror), exactly as
    `classify.emulated_from_settle` does at the settlement.

    Pre-fix the probe took the LOWEST-INDEX resting candidate (`L1`), so
    it TRIGGERed `L1` while the settlement emulated `L2`: MISSED `L2` +
    CONFIRMED-with-note `L1`, `probe_not_settled` in the harness, and a
    `skipped_position_mismatch` every such bar."""
    from pineforge_live.core.book import Intent, content_hash
    from pineforge_live.core.ids import IntentKey
    n = 30

    def it(oid, index):
        key = IntentKey(oid, "ENTRY", "", 0)
        return key.s, Intent(key, index, True, "ENTRY", "", 100.0, None, None, True, 0, 1.0, None, False, False,
                             content_hash(100.0, None, None, True, 1.0))

    book = dict([it("L1", 0), it("L2", 1)])
    # The mirror after the run: L1 still resting, L2 gone (it filled).
    resting_l1 = [{"id": "L1", "type": 1, "from_entry": "", "created_position_cycle_seq": 0, "index": 0}]

    def run_result() -> RunResult:
        return RunResult(status=0, trades=[], net_profit=0.0, script_bars_processed=n + 1, broker_state_hash=[],
                         position_size=1.0, position_avg_price=100.0, position_cycle_seq=0,
                         trail_best_price=float("nan"), current_equity=0.0, last_bar_dual_entry_path=-1,
                         pending_orders=resting_l1)

    fake_ledger = SimpleNamespace(last=SimpleNamespace(book=book, position_size=0.0), n=n, bars=[])
    fake_spec = SimpleNamespace(epoch_hash=lambda: "epoch")
    forming = T.NormalizedBar(0, 100.0, 105.0, 95.0, 100.0, 1.0, 1, is_forming=True)
    P = Probe(handle=None, spec=fake_spec, ledger=fake_ledger, trail_refresh_policy="bar_open_level")
    monkeypatch.setattr(P, "_run", lambda bars, path_order: run_result())

    r = P.evaluate(forming, now_ms=0, journal=None)
    assert [(f.intent, f.leg) for f in r.fills] == [("L2", "ENTRY")]
    assert r.dropped == []


def test_the_dual_entry_guard_reads_the_engines_own_path_report(monkeypatch):
    """m9: `ProbeResult.guard_active` (and the ENTRY-leg deferral it
    drives) comes from `RunResult.last_bar_dual_entry_path`, not from a
    book re-derivation -- two opposite pure-stop entries merely RESTING no
    longer suppress an intrabar entry, and the engine reporting a resolved
    dual-entry path does suppress it whatever the book holds."""
    from pineforge_live.core.book import Intent, content_hash
    from pineforge_live.core.ids import IntentKey
    n = 31

    def it(oid, is_long, index):
        key = IntentKey(oid, "ENTRY", "", 0)
        return key.s, Intent(key, index, is_long, "ENTRY", "", 105.0 if is_long else 95.0, None, None, True, 0,
                             1.0, None, False, False, content_hash(None, None, None, is_long, 1.0))

    book = dict([it("L", True, 0), it("S", False, 1)])
    resting = [{"id": "L", "type": 1, "from_entry": "", "created_position_cycle_seq": 0, "index": 0},
               {"id": "S", "type": 1, "from_entry": "", "created_position_cycle_seq": 0, "index": 1}]

    def run_result(path: int) -> RunResult:
        # L filled (it leaves the mirror); S stays resting.
        return RunResult(status=0, trades=[], net_profit=0.0, script_bars_processed=n + 1, broker_state_hash=[],
                         position_size=1.0, position_avg_price=100.0, position_cycle_seq=0,
                         trail_best_price=float("nan"), current_equity=0.0, last_bar_dual_entry_path=path,
                         pending_orders=[resting[1]])

    forming = T.NormalizedBar(0, 100.0, 105.0, 95.0, 100.0, 1.0, 1, is_forming=True)
    fake_spec = SimpleNamespace(epoch_hash=lambda: "epoch")

    def probe_for(path):
        fake_ledger = SimpleNamespace(last=SimpleNamespace(book=book, position_size=0.0), n=n, bars=[])
        P = Probe(handle=None, spec=fake_spec, ledger=fake_ledger, trail_refresh_policy="bar_open_level")
        monkeypatch.setattr(P, "_run", lambda bars, path_order, _p=path: run_result(_p))
        return P.evaluate(forming, now_ms=0, journal=None)

    for none_code in (0, -1):   # 0 = the engine's own "None", -1 = its NULL-handle return
        free = probe_for(none_code)
        assert not free.guard_active and [(f.intent, f.leg) for f in free.fills] == [("L", "ENTRY")]
    guarded = probe_for(1)
    assert guarded.guard_active and guarded.fills == [] and [(f.intent, f.leg) for f in guarded.deferred] == [("L", "ENTRY")]
