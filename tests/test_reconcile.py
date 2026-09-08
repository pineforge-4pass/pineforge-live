from pineforge_live import types as T
from pineforge_live.core import classify as C
from pineforge_live.core import reconcile as R

def cfg(**kw):
    d = dict(max_missed_age_bars=1, max_missed_entry_distance_bps=30.0, budget_notional=10_000.0, mirror_early_daily_cap=3, adopt_ledger_position=False)
    d.update(kw); return R.ReconcileConfig(**d)
def cf(cls, qty=1.0, leg="ENTRY", is_long=True):
    e = C.EmulatedFill("L", leg, is_long, qty, 100.0, 10)
    return C.ClassifiedFill(cls, e, None, qty, "")
def inp(classified, **kw):
    d = dict(bar_index=10, classified=classified, ledger_position=1.0, real_position=0.0, our_signed_fills=0.0, price=100.0, quiescent=True,
             in_flight=set(), stop_level=T.StopLevel.NONE, missed_age_bars=0, missed_distance_bps=5.0, cfg=cfg(), dead_band=R.DeadBand(0.001, 0.001, 5.0), mirror_early_today=0)
    d.update(kw); return R.ReconcileInput(**d)

def test_dead_band():
    assert R.DeadBand(0.001, 0.002, 5.0).qty(100.0) == 0.05 and R.DeadBand(0.1, 0.001, 5.0).qty(100.0) == 0.1

def test_missed_within_bound_corrects_once():
    d = R.reconcile(inp([cf(C.FillClass.MISSED)]))
    assert len(d.corrections) == 1 and d.corrections[0].kind == "MARKET_CORRECT" and d.corrections[0].side == T.Side.BUY and d.stop is None

def test_missed_beyond_bound_skips_cycle():
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], missed_age_bars=2))
    assert d.corrections == [] and d.skipped_cycle
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], missed_distance_bps=50.0))
    assert d.skipped_cycle

def test_flat_only_refuses_top_up_but_allows_trim():
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, qty=1.0)], real_position=0.5, ledger_position=1.0, stop_level=T.StopLevel.FLAT_ONLY))
    assert d.corrections == [] and abs(d.residual_qty - 0.5) < 1e-9
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, qty=1.0)], real_position=1.5, ledger_position=1.0, stop_level=T.StopLevel.FLAT_ONLY))
    assert d.corrections[0].kind == "REDUCE_ONLY_TRIM" and abs(d.corrections[0].qty - 0.5) < 1e-9

def test_stop_causes():
    assert R.reconcile(inp([cf(C.FillClass.TRIGGER_REVERSED)])).stop[0] == T.StopLevel.FLAT_ONLY
    assert R.reconcile(inp([cf(C.FillClass.UNATTRIBUTED_VENUE)])).stop == (T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")
    assert R.reconcile(inp([cf(C.FillClass.RETRACTED)], real_position=2.0)).stop[0] == T.StopLevel.FLAT_ONLY
    d = R.reconcile(inp([cf(C.FillClass.MIRROR_EARLY)], mirror_early_today=3))
    assert d.stop[0] == T.StopLevel.FLAT_ONLY
    assert R.reconcile(inp([cf(C.FillClass.MIRROR_EARLY)], mirror_early_today=0)).stop is None

def test_not_quiescent_skips():
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], quiescent=False))
    assert d.corrections == [] and d.counters.get("skipped_not_quiescent") == 1


def test_missed_correction_needs_a_strict_position_subset():
    # Task 8 carry (Task 5 review finding 11): a MISSED correction is
    # emitted only if the real position is a STRICT SUBSET of the ledger
    # position on that side -- real == ledger means nothing is actually
    # missing (the "?"-intent spurious MISSED case: an unresolved intent
    # can turn a correctly-filled entry into a MISSED + a CONFIRMED-with-
    # note venue fill, and the reconciler must not "top up" an already-
    # correct position).
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], real_position=1.0, ledger_position=1.0))
    assert d.corrections == [] and d.counters.get("skipped_position_mismatch") == 1
    # real already at/beyond the ledger on the same side -- also not a
    # "missing fill" a MARKET_CORRECT should patch.
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], real_position=1.5, ledger_position=1.0))
    assert d.corrections == [] and d.counters.get("skipped_position_mismatch") == 1
    # real on the OPPOSITE side of the ledger -- also refused.
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], real_position=-0.5, ledger_position=1.0))
    assert d.corrections == [] and d.counters.get("skipped_position_mismatch") == 1
    # a genuine strict subset (same side, smaller magnitude) still corrects.
    d = R.reconcile(inp([cf(C.FillClass.MISSED)], real_position=0.3, ledger_position=1.0))
    assert len(d.corrections) == 1 and d.counters.get("skipped_position_mismatch") is None
