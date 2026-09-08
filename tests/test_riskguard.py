import pytest
from pineforge_live import types as T
from pineforge_live.core import riskguard as RG
from pineforge_live.journal import Journal, StopMarker

def test_wilson_and_n_min():
    assert RG.n_min_for(0.01) == 381 or RG.n_min_for(0.01) == 380    # z²(1−θ)/θ = 3.8416*99 = 380.3 → ceil 381
    assert RG.ub95(0, 381) < 0.01 and RG.ub95(0, 100) > 0.01
    assert 0.0 < RG.ub95(5, 500) < 0.03

def test_breaker_table_self_test():
    ok = RG.BreakerTable([RG.Breaker("orphan", 0.01, 500, RG.n_min_for(0.01), 5)])
    ok.self_test()
    bad = RG.BreakerTable([RG.Breaker("bad", 0.01, 500, 50, 5)])
    with pytest.raises(RuntimeError):
        bad.self_test()

def test_rate_window_alert_only_below_n_min():
    b = RG.Breaker("x", 0.05, 200, 80, 5); w = RG.RateWindow(200)
    for _ in range(10): w.observe(True)
    assert w.alert(b) and not w.breached(b)          # n=10 < n_min
    for _ in range(90): w.observe(False)
    assert w.breached(b)                              # 10/100 = 0.10 > 0.05 with n ≥ n_min

def test_stop_controller_order_and_monotonic(tmp_path):
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = Journal.open(tmp_path / "j.sqlite3", stop_marker=m)
    sc = RG.StopController(j, m)
    sc.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "test")
    assert m.exists() and j.rows("stops", "1=1", ())[-1]["level"] == "FLAT_ONLY" and sc.level == T.StopLevel.FLAT_ONLY
    assert sc.permits("cancel", increases_exposure=False, reduce_only=False)
    assert not sc.permits("entry", increases_exposure=True, reduce_only=False)
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "worse")
    sc.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "never lowers")
    assert sc.level == T.StopLevel.HARD and sc.disposition == T.StopDisposition.HOLD
    assert not sc.permits("mirror_exit", increases_exposure=False, reduce_only=True) and sc.permits("dead_man", False, True)
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.FLATTEN, "flatten beats hold"); assert sc.disposition == T.StopDisposition.FLATTEN
    j.close()
    j2 = Journal.open(tmp_path / "j.sqlite3", stop_marker=None); sc2 = RG.StopController(j2, m); sc2.restore()
    assert sc2.level == T.StopLevel.HARD
    assert sc2.clear("operator") and sc2.level == T.StopLevel.NONE and not m.exists(); j2.close()

def test_riskguard_budgets_and_horizon():
    g = RG.RiskGuard(RG.RiskLimits(max_abs_position=2.0, max_notional=1e6, max_order_notional=1e5, max_fill_actions_per_bar=2, max_book_ops_per_bar=3,
                                   max_daily_realized_loss=1e3, max_daily_reconciles=5, stale_feed_ms=5000, stale_eval_ms=5000, bar_mismatch_streak=3,
                                   disagree_twice=2, unexplained_divergence_pct=2.0, liquidation_distance_pct_min=1.0, recompute_ms_p99_max=500))
    assert g.check_position(2.5, 100.0) == "max_abs_position" and g.check_position(1.0, 100.0) is None
    g.begin_bar(); g.count_fill_action(); g.count_fill_action()
    with pytest.raises(RG.RiskViolation):
        g.count_fill_action()
    assert g.horizon(70, 100) == "ok" and g.horizon(80, 100) == "alert" and g.horizon(100, 100) == "rotate"
