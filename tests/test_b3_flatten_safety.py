"""B3 preliminary ruling: venue-position flattens bypass correction budgets."""
from dataclasses import replace

import pytest

from pineforge_live import types as T
from pineforge_live.core import live
from pineforge_live.core.classify import ClassifiedFill, EmulatedFill, FillClass
from pineforge_live.core.live import LiveCore
from pineforge_live.core.reconcile import DeadBand
from pineforge_live.core.riskguard import RiskLimits
from pineforge_live.epoch import RuntimeConfig
from tests.helpers import corpus_spec, load_bars, make_handle, open_journal


LIMITS = RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 1, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)


@pytest.fixture
def seeded_core(test_so, test_feed, tmp_path):
    bars = load_bars(test_feed, 2001)

    def build(*, max_order_notional=LIMITS.max_order_notional, cap_exhausted=False):
        spec = corpus_spec()
        journal, marker = open_journal(tmp_path)
        config = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000,
                               grace_ms=3_000, open_wait_ms=2_000, risk_limits={})
        limits = replace(LIMITS, max_order_notional=max_order_notional)
        core = LiveCore(make_handle(test_so, spec), spec, journal, marker, config,
                        limits, DeadBand(0.001, 0.001, 5.0), [])
        core.seed(bars[:2000])
        core._roll_day(bars[2000])
        core.reconciles_today = limits.max_daily_reconciles if cap_exhausted else 0
        return core, bars[2000]

    return build


@pytest.mark.parametrize("cause", [FillClass.TRIGGER_REVERSED, FillClass.ENTRY_SLIP])
@pytest.mark.parametrize("real_position", [-5.0, 5.0])
def test_reconciler_flatten_bypasses_exhausted_cap_and_oversize_notional(
        seeded_core, monkeypatch, cause, real_position):
    core, bar = seeded_core(max_order_notional=100.0, cap_exhausted=True)
    monkeypatch.setattr(live, "classify_bar", lambda *a, **kw: [ClassifiedFill(cause, None, None, 0.0, "")])

    out = core.settle(bar, [], set(), set(), real_position, 0, our_signed_fills=real_position)

    assert abs(real_position) * bar.c > core.limits.max_order_notional
    assert out.reconcile.counters["flattened"] == 1
    assert [(a.kind, a.cls, a.side, a.qty, a.reduce_only) for a in out.actions if a.kind == "FLATTEN"] == [
        ("FLATTEN", "FLATTEN", T.Side.BUY if real_position < 0 else T.Side.SELL, abs(real_position), True)
    ]
    assert core.reconciles_today == core.limits.max_daily_reconciles
    assert not core._hard_flat_issued  # the reconciler's action does not consume the STOP's one-shot
    assert not [i for i in out.incidents if i["kind"] in {"max_daily_reconciles", "cycle_skipped"}]
    assert not [i for i in out.incidents if i["kind"] == "risk_refused" and i["action"] == "FLATTEN"]


@pytest.mark.parametrize("gate", ["daily_cap", "order_notional"])
@pytest.mark.parametrize("reduce_only", [False, True])
def test_ordinary_corrections_keep_daily_and_notional_gates(seeded_core, monkeypatch, gate, reduce_only):
    core, bar = seeded_core(max_order_notional=100.0 if gate == "order_notional" else 1e9,
                            cap_exhausted=gate == "daily_cap")
    # Keep the reconciler's exposure-increasing budget open to exercise
    # LiveCore's own per-order gate on both ENTRY and EXIT repairs.
    core.rcfg = replace(core.rcfg, budget_notional=1e9)
    fill = EmulatedFill("XS" if reduce_only else "S", "EXIT" if reduce_only else "ENTRY",
                        False, 1.0, bar.c, 2000)
    monkeypatch.setattr(live, "classify_bar", lambda *a, **kw: [ClassifiedFill(FillClass.MISSED, fill, None, 1.0, "")])
    position = -2.0 if reduce_only else 0.0  # the engine's settled target is -1
    before = core.reconciles_today

    out = core.settle(bar, [], set(), set(), position, 0, our_signed_fills=position)

    assert [(c.kind, c.qty, c.reduce_only) for c in out.reconcile.corrections] == [("MARKET_CORRECT", 1.0, reduce_only)]
    assert not [a for a in out.actions if a.kind == "CORRECTION"]
    assert core.reconciles_today == before
    if gate == "daily_cap":
        assert [i for i in out.incidents if i["kind"] == "max_daily_reconciles" and i["action"] == "CORRECTION"]
        assert [i for i in out.incidents if i["kind"] == "cycle_skipped"]
    else:
        assert [i for i in out.incidents if i["kind"] == "risk_refused"
                and i["action"] == "CORRECTION" and i["cause"] == "max_order_notional"]


def test_reconciler_flatten_still_obeys_hard_hold(seeded_core, monkeypatch):
    core, bar = seeded_core(max_order_notional=100.0, cap_exhausted=True)
    core.stop.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD, "hold for operator")
    monkeypatch.setattr(live, "classify_bar", lambda *a, **kw: [
        ClassifiedFill(FillClass.TRIGGER_REVERSED, None, None, 0.0, "")
    ])

    out = core.settle(bar, [], set(), set(), -5.0, 0, our_signed_fills=-5.0)

    assert out.reconcile.counters["flattened"] == 1
    assert not [a for a in out.actions if a.kind == "FLATTEN"]
    assert [i for i in out.incidents if i["kind"] == "action_refused_by_stop" and i["action"] == "FLATTEN"]
    assert core.stop.level is T.StopLevel.HARD and core.stop.disposition is T.StopDisposition.HOLD
