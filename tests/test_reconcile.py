from pineforge_live import types as T
from pineforge_live.core import classify as C
from pineforge_live.core import reconcile as R

def cfg(**kw):
    d = dict(max_missed_age_bars=1, max_missed_entry_distance_bps=30.0, budget_notional=10_000.0, mirror_early_daily_cap=3, adopt_ledger_position=False)
    d.update(kw); return R.ReconcileConfig(**d)
def cf(cls, qty=1.0, leg="ENTRY", is_long=True, intent="L"):
    e = C.EmulatedFill(intent, leg, is_long, qty, 100.0, 10)
    return C.ClassifiedFill(cls, e, None, qty, "")
def inp(classified, **kw):
    # `our_signed_fills` defaults to `real_position` when the caller
    # doesn't override it -- L8 made `our_signed_fills` the PRIMARY
    # correction basis (real_position is now the secondary account-
    # mismatch check), so a test that only cares about `real_position`
    # shouldn't have to separately restate it to avoid an incidental
    # account_mismatch escalation; a test that specifically exercises the
    # L8 secondary check passes both explicitly.
    d = dict(bar_index=10, classified=classified, ledger_position=1.0, real_position=0.0, our_signed_fills=None, price=100.0, quiescent=True,
             in_flight=set(), stop_level=T.StopLevel.NONE, missed_age_bars=0, missed_distance_bps=5.0, cfg=cfg(), dead_band=R.DeadBand(0.001, 0.001, 5.0), mirror_early_today=0)
    d.update(kw)
    if d["our_signed_fills"] is None:
        d["our_signed_fills"] = d["real_position"]
    return R.ReconcileInput(**d)

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


# ---- Task 6 review fix-wave pins (H1/H2/M3/M4/M5/L6/L7/L8) ----

def test_missed_exit_ledger_flat_real_open_corrects_reduce_only():
    """H1: a genuine missed EXIT -- the ledger closed (now flat) but the
    venue never got the close -- must CORRECT, not refuse (the inverted
    gate previously refused this, the one case a missed-exit correction
    exists to fix)."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, leg="EXIT", is_long=True, intent="XL")], ledger_position=0.0, real_position=1.0))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "MARKET_CORRECT" and c.side == T.Side.SELL and abs(c.qty - 1.0) < 1e-9 and c.intent == "XL"

def test_both_flat_missed_exit_emits_nothing():
    """H1: the both-flat case (venue already flat too, e.g. an earlier
    liquidation) is spurious, not a missed exit -- must NOT open a
    position (the pre-fix bug: it read as a MARKET_CORRECT SELL, opening
    a short out of nothing)."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, leg="EXIT", is_long=True, intent="XL")], ledger_position=0.0, real_position=0.0))
    assert d.corrections == [] and d.counters.get("skipped_position_mismatch") == 1

def test_qty_divergent_flat_ledger_excess_trims_even_under_flat_only():
    """H2: a flat ledger with ANY residual real position is an excess
    (needs a trim), never a "top-up" -- `ledger_position >= 0` used to
    treat a flat ledger as long, so an excess SHORT under a flat ledger
    was refused as a top-up instead of trimmed. Must trim even under
    FLAT_ONLY (reduce-only is never gated)."""
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, leg="EXIT", is_long=False, intent="XS")],
                        ledger_position=0.0, real_position=-0.5, stop_level=T.StopLevel.FLAT_ONLY))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "REDUCE_ONLY_TRIM" and c.side == T.Side.BUY and abs(c.qty - 0.5) < 1e-9

def test_retracted_and_missed_escalates_and_suppresses_the_correction():
    """M3: a decision that escalates FLAT_ONLY from one classified fill
    must gate a MARKET_CORRECT another classified fill in the SAME
    decision would otherwise emit -- the prior version only gated against
    the level walking in (`inp.stop_level`), so this decision's own
    RETRACTED escalation never touched its own MISSED correction."""
    d = R.reconcile(inp([cf(C.FillClass.RETRACTED), cf(C.FillClass.MISSED, qty=1.0)],
                        real_position=0.3, ledger_position=1.0))
    assert d.stop[0] == T.StopLevel.FLAT_ONLY
    assert all(c.kind != "MARKET_CORRECT" for c in d.corrections)
    assert d.skipped_cycle

def test_trigger_reversed_and_missed_emits_exactly_one_flatten_no_correction():
    """M3 + L6: TRIGGER_REVERSED escalates FLAT_ONLY and wants a FLATTEN;
    a MISSED fill in the same decision must NOT also ship a
    MARKET_CORRECT (gated by this decision's own escalation), and exactly
    one FLATTEN is emitted (not one per TRIGGER_REVERSED/ENTRY_SLIP
    fill)."""
    d = R.reconcile(inp([cf(C.FillClass.TRIGGER_REVERSED), cf(C.FillClass.ENTRY_SLIP), cf(C.FillClass.MISSED, qty=1.0)],
                        real_position=0.5, ledger_position=1.0))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "FLATTEN" and c.side == T.Side.SELL and abs(c.qty - 0.5) < 1e-9

def test_missed_ambiguous_entry_qty_clamped_to_shortfall():
    """M4: the correction qty is the SHORTFALL (`ledger - real`), not the
    ambiguous fill's own `e.qty` outright -- a `"?"`-intent MISSED fill
    (M4 ambiguity) carries the WHOLE delta's qty even when only PART of it
    is actually missing (one leg of a partially-confirmed pyramid), so
    clamping to `e.qty` over-corrects."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, qty=2.0, intent="?")], real_position=1.0, ledger_position=2.0))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "MARKET_CORRECT" and c.side == T.Side.BUY and abs(c.qty - 1.0) < 1e-9

def test_two_qty_divergent_fills_emit_one_top_up():
    """M5: two QTY_DIVERGENT classified fills (e.g. the M4 ambiguity split
    across two candidates) describe the SAME aggregate shortfall -- must
    correct with ONE top-up sized to the position delta, not one per
    fill."""
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, qty=0.5, intent="A"), cf(C.FillClass.QTY_DIVERGENT, qty=0.5, intent="B")],
                        real_position=1.0, ledger_position=2.0))
    top_ups = [c for c in d.corrections if c.kind == "TOP_UP"]
    assert len(top_ups) == 1 and abs(top_ups[0].qty - 1.0) < 1e-9 and top_ups[0].side == T.Side.BUY

def test_top_up_over_budget_refused():
    """M5: TOP_UP is now budget-checked exactly like a MISSED entry
    (previously unchecked)."""
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, qty=199.0)], real_position=1.0, ledger_position=200.0,
                        cfg=cfg(budget_notional=10_000.0)))
    assert d.corrections == [] and d.counters.get("refused_budget") == 1
    assert abs(d.residual_qty - 199.0) < 1e-9

def test_account_mismatch_escalates_and_skips_all_corrections():
    """L8: `our_signed_fills` is the PRIMARY correction basis; when the
    account's `real_position` disagrees with it beyond the dead-band, the
    reconciler can't trust its own fill-tracking this call -- escalate
    FLAT_ONLY (cause account_mismatch) and skip every MISSED/QTY_DIVERGENT
    correction, even one that would otherwise clearly fire."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, qty=1.0)], our_signed_fills=0.0, real_position=5.0, ledger_position=1.0))
    assert d.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "account_mismatch")
    assert d.corrections == []

def test_malformed_missed_and_qty_divergent_counted_not_crashed():
    """N10: a classified MISSED/QTY_DIVERGENT fill with `emulated=None`
    (shouldn't happen per classify.py's own contract, but the reconciler
    must not crash on it) is counted as `malformed`, not silently
    dropped."""
    malformed_missed = C.ClassifiedFill(C.FillClass.MISSED, None, None, 1.0, "")
    malformed_qty = C.ClassifiedFill(C.FillClass.QTY_DIVERGENT, None, None, 1.0, "")
    d = R.reconcile(inp([malformed_missed, malformed_qty]))
    assert d.corrections == [] and d.counters.get("malformed") == 2

def test_escalate_keeps_first_cause_on_equal_level_and_disposition():
    """N10 (Q11e): two escalations to the SAME (level, disposition) in one
    decision keep the FIRST cause, not whichever ran last."""
    d = R.reconcile(inp([cf(C.FillClass.RETRACTED), cf(C.FillClass.TRIGGER_REVERSED)], real_position=0.0, ledger_position=0.0))
    assert d.stop[0] == T.StopLevel.FLAT_ONLY
    assert d.stop[2] == "RETRACTED: real ≠ ledger beyond dead-band"
