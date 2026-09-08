import pytest
from pineforge_live import types as T
from pineforge_live.core import riskguard as RG
from pineforge_live.journal import Journal, JournalFault, StopMarker, StopMarkerPresent

def test_wilson_and_n_min():
    assert RG.n_min_for(0.01) == 381    # z²(1−θ)/θ = 3.8416*99 = 380.304 → ceil 381, exact
    assert RG.ub95(0, 381) < 0.01 and RG.ub95(0, 100) > 0.01
    assert 0.0 < RG.ub95(5, 500) < 0.03

def test_breaker_table_self_test():
    ok = RG.BreakerTable([RG.Breaker("orphan", 0.01, 500, RG.n_min_for(0.01), 5)])
    ok.self_test()
    bad = RG.BreakerTable([RG.Breaker("bad", 0.01, 500, 50, 5)])
    with pytest.raises(RuntimeError):
        bad.self_test()

def test_breaker_table_self_test_rejects_window_smaller_than_n_min():
    # n9: window_n < n_min means breached() can never gate n >= n_min -- the
    # breaker sits in alert() forever no matter how bad the rate gets.
    unreachable = RG.BreakerTable([RG.Breaker("orphan", 0.01, 300, RG.n_min_for(0.01), 5)])  # window 300 < n_min 381
    with pytest.raises(RuntimeError):
        unreachable.self_test()

def test_rate_window_alert_only_below_n_min():
    b = RG.Breaker("x", 0.05, 200, 80, 5); w = RG.RateWindow(200)
    for _ in range(10): w.observe(True)
    assert w.alert(b) and not w.breached(b)          # n=10 < n_min
    for _ in range(90): w.observe(False)
    assert w.breached(b)                              # 10/100 = 0.10 > 0.05 with n ≥ n_min

def test_rate_window_running_count_tracks_eviction():
    # n9: x is a running count kept in sync by observe(), not a per-call
    # rescan -- verify it survives the window sliding past True observations.
    w = RG.RateWindow(3)
    for hit in [True, True, False]:
        w.observe(hit)
    assert w.n == 3 and w.x == 2
    w.observe(True)    # evicts the oldest True, adds a True -> net unchanged
    assert w.n == 3 and w.x == 2
    w.observe(False)   # evicts a True, adds a False -> x drops
    assert w.n == 3 and w.x == 1
    w.observe(False)   # evicts a False, adds a False -> unchanged
    assert w.n == 3 and w.x == 1
    w.observe(False)   # evicts the last True, adds a False -> x hits 0
    assert w.n == 3 and w.x == 0

def _open(tmp_path, name="j.sqlite3", stop_marker=None):
    return Journal.open(tmp_path / name, stop_marker=stop_marker)

def test_stop_controller_order_and_monotonic(tmp_path):
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = _open(tmp_path, stop_marker=m)
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

def test_raise_stop_survives_journal_fault_in_finally():
    """F1 pin: a JournalFault from append_stop AFTER the marker landed must
    still leave the controller's in-memory STOP set (and the exception must
    still propagate) -- the durable marker is real evidence of an emergency
    STOP and memory must agree with it regardless of what the journal did."""
    class FaultingJournal:
        def append_stop(self, level, disposition, cause):
            raise JournalFault("simulated disk-full on append_stop")
    class FakeMarker:
        def __init__(self):
            self._payload = None
        def write(self, level, disposition, cause):
            self._payload = {"level": level, "disposition": disposition, "cause": cause}
        def exists(self):
            return self._payload is not None
        def clear(self):
            self._payload = None

    j, m = FaultingJournal(), FakeMarker()
    sc = RG.StopController(j, m)
    with pytest.raises(JournalFault):
        sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.FLATTEN, "emergency")
    assert m.exists()                                  # marker landed before the fault
    assert sc.level == T.StopLevel.HARD and sc.disposition == T.StopDisposition.FLATTEN   # memory still set
    assert not sc.permits("entry", increases_exposure=True, reduce_only=False)            # and it actually STOPs

def test_raise_stop_survives_marker_fault_too():
    """The same finally covers a marker-write fault (the §6 disk-full cause
    routed to STOP(HARD, HOLD)) -- memory must not stay NONE either."""
    class FaultingMarker:
        def write(self, level, disposition, cause):
            raise JournalFault("simulated ENOSPC on marker write")
    class NoopJournal:
        def append_stop(self, level, disposition, cause):
            pass

    sc = RG.StopController(NoopJournal(), FaultingMarker())
    with pytest.raises(JournalFault):
        sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "disk full")
    assert sc.level == T.StopLevel.HARD and sc.disposition == T.StopDisposition.HOLD
    assert not sc.permits("entry", increases_exposure=True, reduce_only=False)

def test_clear_drains_every_open_row_and_restart_recovers_to_none(tmp_path):
    """F2 pin: raise ×3 (three open `stops` rows) → clear() must drain ALL
    of them, not just the latest -- otherwise restore() resurrects an older
    escalation after the operator cleared. Also n5: the real restart path
    refuses via StopMarkerPresent while the marker is set; only after the
    controller clears it does reopening with the marker succeed."""
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = _open(tmp_path, stop_marker=m)
    sc = RG.StopController(j, m)
    sc.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "a")
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "b")
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.FLATTEN, "c")
    assert len(j.rows("stops", "cleared_ms IS NULL", ())) == 3

    assert sc.clear("operator") is True
    assert sc.level == T.StopLevel.NONE and not m.exists()
    assert j.rows("stops", "cleared_ms IS NULL", ()) == []   # every open row drained, not just the latest
    assert sc.clear("operator again") is False                # nothing left open -- a no-op, not a fault
    j.close()

    # n5: the real restart path -- reopening WITH the marker while it is
    # still set must refuse via StopMarkerPresent (the marker was cleared
    # above, so this now succeeds, proving the path is exercised for real
    # rather than sidestepped with stop_marker=None).
    j2 = _open(tmp_path, stop_marker=m)
    sc2 = RG.StopController(j2, m)
    sc2.restore()
    assert sc2.level == T.StopLevel.NONE   # nothing open to restore -- all three rows were cleared
    j2.close()

def test_restart_with_set_marker_refuses_then_clears_through_controller(tmp_path):
    """n5 pin: with the marker still SET after a raise, reopening WITH it
    must raise StopMarkerPresent (the real restart path) -- the operator's
    recovery tool opens without the marker check, restores, and clears
    through the controller before a normal reopen can succeed."""
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = _open(tmp_path, stop_marker=m)
    sc = RG.StopController(j, m)
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.FLATTEN, "operator manual")
    j.close()

    with pytest.raises(StopMarkerPresent):
        _open(tmp_path, stop_marker=m)

    # operator recovery: open without the marker gate, restore, clear.
    j2 = _open(tmp_path, stop_marker=None)
    sc2 = RG.StopController(j2, m)
    sc2.restore()
    assert sc2.level == T.StopLevel.HARD and sc2.disposition == T.StopDisposition.FLATTEN
    assert sc2.clear("operator") and sc2.level == T.StopLevel.NONE and not m.exists()
    j2.close()

    j3 = _open(tmp_path, stop_marker=m)   # now succeeds -- marker is cleared
    j3.close()

def test_restore_ties_resolve_to_stronger_not_last_row(tmp_path):
    """m4 pin: restore() must pick the `stronger()` open row, not whichever
    one it happens to read last. Insert rows directly through the journal
    (bypassing raise_stop's own monotonic guard) in an order where the
    strongest row is NOT the last one written."""
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = _open(tmp_path, stop_marker=m)
    j.append_stop(T.StopLevel.HARD.value, T.StopDisposition.FLATTEN.value, "strongest, written first")
    j.append_stop(T.StopLevel.FLAT_ONLY.value, T.StopDisposition.NONE.value, "weaker")
    j.append_stop(T.StopLevel.HARD.value, T.StopDisposition.HOLD.value, "weaker than FLATTEN, written last")
    j.close()

    j2 = _open(tmp_path, stop_marker=None)
    sc2 = RG.StopController(j2, m)
    sc2.restore()
    assert sc2.level == T.StopLevel.HARD and sc2.disposition == T.StopDisposition.FLATTEN   # not HOLD, the last row
    j2.close()

def test_stronger_rank_table():
    N, HOLD, FLATTEN = T.StopDisposition.NONE, T.StopDisposition.HOLD, T.StopDisposition.FLATTEN
    NONE, FLAT_ONLY, HARD = T.StopLevel.NONE, T.StopLevel.FLAT_ONLY, T.StopLevel.HARD
    assert RG.stronger((FLAT_ONLY, N), (NONE, N))
    assert RG.stronger((HARD, HOLD), (FLAT_ONLY, N))
    assert RG.stronger((HARD, FLATTEN), (HARD, HOLD))
    assert RG.stronger((HARD, HOLD), (HARD, N))
    assert not RG.stronger((HARD, HOLD), (HARD, FLATTEN))
    assert not RG.stronger((NONE, N), (NONE, N))   # equal pair is not strictly stronger than itself

def test_permits_hard_flat_is_disposition_aware(tmp_path):
    """m3/F3 pin: hard_flat is a reduce-only pass ONLY under HARD/FLATTEN,
    never under plain HARD/HOLD (spec §5.5(c): "under FLATTEN")."""
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = _open(tmp_path, stop_marker=m)
    sc = RG.StopController(j, m)
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "manual")
    assert not sc.permits("hard_flat", increases_exposure=False, reduce_only=True)     # refused under HOLD
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.FLATTEN, "escalate")
    assert sc.permits("hard_flat", increases_exposure=False, reduce_only=True)         # allowed under FLATTEN
    assert not sc.permits("hard_flat", increases_exposure=False, reduce_only=False)    # must still be reduce-only
    j.close()

def test_hold_expired(tmp_path):
    """n6 pin: hold_expired() is disabled at hard_stop_max_hold_ms=0, tracks
    raised_ms only while HARD/HOLD, and clears it on a disposition change or
    clear()."""
    m = StopMarker(tmp_path / "j.stop"); m.prepare(); j = _open(tmp_path, stop_marker=m)
    sc = RG.StopController(j, m, hard_stop_max_hold_ms=1000)
    assert not sc.hold_expired(0)     # never raised yet
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "manual")
    t0 = sc.raised_ms
    assert t0 is not None
    assert not sc.hold_expired(t0 + 999)
    assert sc.hold_expired(t0 + 1000)
    sc.raise_stop(T.StopLevel.HARD, T.StopDisposition.FLATTEN, "escalate")
    assert sc.raised_ms is None and not sc.hold_expired(t0 + 10_000)   # off HOLD -- never expires
    j.close()

    disabled = RG.StopController(Journal.open(tmp_path / "j2.sqlite3"), StopMarker(tmp_path / "j2.stop"))
    disabled.m.prepare()
    disabled.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "manual")
    assert not disabled.hold_expired(disabled.raised_ms + 10_000_000)   # hard_stop_max_hold_ms=0 -- disabled
    disabled.j.close()

def test_riskguard_budgets_and_horizon():
    g = RG.RiskGuard(RG.RiskLimits(max_abs_position=2.0, max_notional=1e6, max_order_notional=1e5, max_fill_actions_per_bar=2, max_book_ops_per_bar=3,
                                   max_daily_realized_loss=1e3, max_daily_reconciles=5, stale_feed_ms=5000, stale_eval_ms=5000, bar_mismatch_streak=3,
                                   disagree_twice=2, unexplained_divergence_pct=2.0, liquidation_distance_pct_min=1.0, recompute_ms_p99_max=500))
    assert g.check_position(2.5, 100.0) == "max_abs_position" and g.check_position(1.0, 100.0) is None
    g.begin_bar(); g.count_fill_action(); g.count_fill_action()
    with pytest.raises(RG.RiskViolation):
        g.count_fill_action()
    assert g.horizon(70, 100) == "ok" and g.horizon(80, 100) == "alert" and g.horizon(100, 100) == "rotate"

def test_riskguard_checks_take_abs():
    """n7 pin: the sign of `new_abs_position`/`qty` must not be trusted --
    a negative value (a bookkeeping bug upstream) must still be checked
    against the limit rather than silently passing."""
    g = RG.RiskGuard(RG.RiskLimits(max_abs_position=2.0, max_notional=1e6, max_order_notional=1e5, max_fill_actions_per_bar=2, max_book_ops_per_bar=3,
                                   max_daily_realized_loss=1e3, max_daily_reconciles=5, stale_feed_ms=5000, stale_eval_ms=5000, bar_mismatch_streak=3,
                                   disagree_twice=2, unexplained_divergence_pct=2.0, liquidation_distance_pct_min=1.0, recompute_ms_p99_max=500))
    assert g.check_position(-3.0, 100.0) == "max_abs_position"
    assert g.check_order_notional(-2, 6e4) == "max_order_notional"
