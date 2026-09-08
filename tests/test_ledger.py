import math, pytest
from pineforge_live.core.ledger import Ledger, LedgerDivergence, BarsDivergence
from pineforge_live import types as T
from tests.helpers import load_bars, make_handle, corpus_spec, open_journal

@pytest.fixture
def env(test_so, test_feed, tmp_path):
    spec = corpus_spec(); h = make_handle(test_so, spec); j, m = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    return spec, h, j, load_bars(test_feed, 2200)

def test_seed_then_settle_g1_holds(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, runtime_config_hash="rc")
    s0 = L.seed(bars[:2000])
    assert s0.bar_index == 1999 and len(s0.hashes) == 2000 and L.n == 2000
    # Controller ruling: with realtime_tail on (corpus_spec's default), the
    # corpus run never produces an open_at_end row -- pinned here so a
    # future engine/config change that starts emitting one is caught.
    assert not any(t.open_at_end for t in s0.trades)
    for i in range(2000, 2010):
        s = L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)
        assert s.bar_index == i and len(s.hashes) == i + 1
        assert not any(t.open_at_end for t in s.trades)
        assert j.last_settlement(spec.epoch_hash())["bar_index"] == i
    row = j.last_settlement(spec.epoch_hash())
    assert row["broker_state_hash"] == L.last.hashes[-1] and row["trades_sha256"] == L.last.trades_sha256
    assert not math.isnan(row["equity"]) and row["position"] == L.last.position_size

def test_seed_mismatch_raises(env):
    spec, h, j, bars = env
    with pytest.raises(LedgerDivergence) as e:
        Ledger(h, spec, j, "rc").seed(bars[:2000], expected_trades_sha256="0" * 64)
    assert e.value.cause == "seed_mismatch"

def test_revised_bar_is_bars_divergence(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000]); L.settle(bars[2000], 0)
    revised = T.NormalizedBar(bars[2000].ts_open, bars[2000].o, bars[2000].h + 1, bars[2000].l, bars[2000].c, bars[2000].v, 0)
    with pytest.raises(BarsDivergence):
        L.settle(revised, 0)
    assert j.rows("incidents", "kind=?", ("bars_divergence",))

def test_g1_hash_mismatch_detected(env, monkeypatch):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    # tamper the journaled hash of bar 1999 (simulates a mutant .so / divergent recompute)
    j._exec("UPDATE settlements SET broker_state_hash=? WHERE bar_index=1999", (f"{123:016x}",))
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "g1_hash"
