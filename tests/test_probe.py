import asyncio
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


def test_probe_fills_subset_of_settlement_over_50_bars(env):
    """L1 probe ≡ recompute at k=4 points: every probe fill is either a settlement fill of that bar or retracted."""
    spec, h, j, bars, L = env
    P = Probe(h, spec, L, trail_refresh_policy="bar_open_level")
    total_probe, total_settle, retracts = 0, 0, 0
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
            retracts += len(r.retracted)
        s = L.settle(bar, now_ms=bar.ts_open + 900_000)
        settled = {(k.entry_id, "ENTRY") for k in s.new_opened} | {(k.exit_id, "EXIT") for k in s.new_closed}
        total_probe += len(probe_fills_this_bar); total_settle += len(settled)
        # a probe fill that the settlement did not book must have been retracted by a later evaluate
        for f in probe_fills_this_bar - settled:
            assert any(x.intent == f[0] and x.leg == f[1] for x in P.retracted_history[i]), (i, f)
    assert total_settle >= 1, "the window must contain at least one settled fill to be a meaningful test"
    assert j.rows("evaluations", "outcome=?", ("ran",))


def test_book_captured_before_probe_run(test_so, test_feed, tmp_path):
    """settled_book(self.h, self.L.last) must run BEFORE any probe run_full
    (EngineHandle's accessors reflect only the last run) -- and evaluate()
    must keep returning levels keyed by the settled intents even after two
    probe runs (P_auto + P_other) have since replaced the handle's live
    strategy."""
    spec = corpus_spec(); h = make_handle(test_so, spec); j, _ = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    bars = load_bars(test_feed, 3000)
    L = Ledger(h, spec, j, "rc"); s = L.seed(bars)
    pre_probe_book = settled_book(h, s)
    assert pre_probe_book, "expected >=1 settled intent to exercise the level-refresh path"

    P = Probe(h, spec, L, trail_refresh_policy="bar_open_level")
    forming = T.NormalizedBar(bars[-1].ts_open + 900_000, bars[-1].c, bars[-1].c, bars[-1].c, bars[-1].c, 0.0, 0, is_forming=True)
    r = P.evaluate(forming, now_ms=forming.ts_open)  # runs P_auto (and P_other if it fills anything)

    # The book evaluate() captured BEFORE its probe run(s) must still match
    # the settled intents from the last real settlement -- proving the
    # ordering hazard the docstring warns about was avoided.
    assert set(r.levels.keys()) == set(pre_probe_book.keys())
    for key, it in pre_probe_book.items():
        assert r.levels[key] == (it.stop, it.limit, it.activation)

    # A second evaluate() (a second pair of probe runs on the SAME handle)
    # must still agree: settled_book(self.h, self.L.last) is recaptured
    # fresh at the top of every evaluate() call, so it is never stale.
    r2 = P.evaluate(forming, now_ms=forming.ts_open + 1)
    assert set(r2.levels.keys()) == set(pre_probe_book.keys())
