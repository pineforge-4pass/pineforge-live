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
    """N10 (Q11e): two escalations to the SAME (level, disposition) keep
    the FIRST cause, not whichever ran last. Driven through `_escalate`
    directly with an EQUAL `(HARD, FLATTEN)` pair -- that is the case the
    N10 fix actually changed (the pre-fix `_escalate` overwrote whenever
    `disp == FLATTEN`, so a HARD/FLATTEN pair kept the LAST cause). The
    two-FLAT_ONLY/NONE route this pin used to take could not fail against
    the pre-fix code (re-review NEW-4)."""
    d = R.ReconcileDecision()
    R._escalate(d, T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")
    R._escalate(d, T.StopLevel.HARD, T.StopDisposition.FLATTEN, "dead-man fired")
    assert d.stop == (T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")

def test_escalation_uses_the_shared_riskguard_rank_table():
    """Prelim (Task 8): `_escalate` no longer carries its own copy of
    §5.5's (level, disposition) lattice -- it calls the single exported
    `riskguard.stronger()`, so the reconciler and `StopController` can
    never drift apart on which of two STOPs wins."""
    from pineforge_live.core.riskguard import stronger
    assert R.stronger is stronger
    assert not hasattr(R, "_STOP_RANK") and not hasattr(R, "_DISP_RANK")

def test_escalation_keeps_the_strongest_regardless_of_fill_order():
    """A weaker escalation arriving AFTER a stronger one never lowers the
    decision's STOP, and a stronger one arriving after a weaker one always
    raises it -- both directions, same shared rank table."""
    hard = (T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")
    d = R.reconcile(inp([cf(C.FillClass.UNATTRIBUTED_VENUE), cf(C.FillClass.RETRACTED)], real_position=2.0))
    assert d.stop == hard
    d = R.reconcile(inp([cf(C.FillClass.RETRACTED), cf(C.FillClass.UNATTRIBUTED_VENUE)], real_position=2.0))
    assert d.stop == hard


# ---- Task 6 re-review pins (NEW-1 netting, NEW-2 side, NEW-3 opposite
# sides, NEW-5 skipped_cycle, X13 unreconcilable sides) ----

def test_flatten_suppresses_a_missed_correction_in_the_same_decision():
    """NEW-1 (X3): a FLATTEN already takes the venue to ZERO, so nothing
    else in the same decision may be sized against the pre-flatten gap. The
    pre-fix code shipped `FLATTEN SELL 1.0` **plus** a reduce-only
    `MARKET_CORRECT SELL 1.0` for the missed EXIT -- both legs reduce-only
    (so the M3 gate never touched them) and the account cross-check passes
    (real == basis) -- driving the account to −1.0: a SHORT opened out of a
    flatten."""
    d = R.reconcile(inp([cf(C.FillClass.TRIGGER_REVERSED, intent="T"), cf(C.FillClass.MISSED, leg="EXIT", is_long=True, intent="XL")],
                        ledger_position=0.0, real_position=1.0, our_signed_fills=1.0))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "FLATTEN" and c.side == T.Side.SELL and abs(c.qty - 1.0) < 1e-9

def test_flatten_suppresses_a_qty_divergent_correction_in_the_same_decision():
    """NEW-1 (X4): same shape on the QTY_DIVERGENT leg -- the pre-fix code
    shipped `FLATTEN SELL 1.5` **plus** `REDUCE_ONLY_TRIM SELL 0.5`,
    leaving the venue at −0.5."""
    d = R.reconcile(inp([cf(C.FillClass.TRIGGER_REVERSED, intent="T"), cf(C.FillClass.QTY_DIVERGENT, qty=0.5, intent="L")],
                        ledger_position=1.0, real_position=1.5, our_signed_fills=1.5))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "FLATTEN" and c.side == T.Side.SELL and abs(c.qty - 1.5) < 1e-9

def test_missed_and_qty_divergent_net_within_one_decision():
    """NEW-1 (X1): the ordinary pyramid shape -- one candidate partially
    filled, the other not, so classify's M4 split yields a MISSED **and** a
    QTY_DIVERGENT describing ONE 1.5 shortfall. "Once per decision" means
    NETTED: the MISSED correction issues 1.0 and the QTY_DIVERGENT pass
    then sees `basis + issued` and tops up only the 0.5 remainder. The
    pre-fix code sized both from the full gap (BUY 1.0 + TOP_UP 1.5 = 2.5
    on a 1.5 shortfall)."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, qty=1.0, intent="L1"), cf(C.FillClass.QTY_DIVERGENT, qty=0.5, intent="L2")],
                        ledger_position=2.0, real_position=0.5, our_signed_fills=0.5))
    assert len(d.corrections) == 2
    mc, top = d.corrections
    assert mc.kind == "MARKET_CORRECT" and mc.side == T.Side.BUY and abs(mc.qty - 1.0) < 1e-9 and mc.intent == "L1"
    assert top.kind == "TOP_UP" and top.side == T.Side.BUY and abs(top.qty - 0.5) < 1e-9 and top.intent == "L2"

def test_missed_exit_and_qty_divergent_net_within_one_decision():
    """NEW-1 (X2): the reducing mirror image -- ledger 0.5, basis 1.5, a
    missed EXIT of 0.5 and a QTY_DIVERGENT over the same 1.0 excess. The
    missed EXIT sells 0.5, the trim then covers only the remaining 0.5
    (pre-fix: SELL 0.5 + TRIM 1.0 = 1.5, flattening a ledger that still
    wants 0.5 long)."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, qty=0.5, leg="EXIT", is_long=True, intent="XL"), cf(C.FillClass.QTY_DIVERGENT, qty=1.0, intent="L")],
                        ledger_position=0.5, real_position=1.5, our_signed_fills=1.5))
    assert len(d.corrections) == 2
    mc, trim = d.corrections
    assert mc.kind == "MARKET_CORRECT" and mc.side == T.Side.SELL and abs(mc.qty - 0.5) < 1e-9 and mc.intent == "XL"
    assert trim.kind == "REDUCE_ONLY_TRIM" and trim.side == T.Side.SELL and abs(trim.qty - 0.5) < 1e-9

def _x6_fills():
    """The X6 one-bar round trip `0 → +1 (L) → 0 (XL) → −1 (S)` with the
    venue idle: `emulated_from_settle` yields the L/XL legs from the closed
    trade and the S delta fill, all three MISSED."""
    return [cf(C.FillClass.MISSED, qty=1.0, leg="ENTRY", is_long=True, intent="L"),
            cf(C.FillClass.MISSED, qty=1.0, leg="EXIT", is_long=True, intent="XL"),
            cf(C.FillClass.MISSED, qty=1.0, leg="ENTRY", is_long=False, intent="S")]

def test_missed_entry_representative_agrees_with_the_ledger_side():
    """NEW-2 (X6): on the `basis == 0` ENTRY path the representative must
    be the first missed entry whose own direction equals the LEDGER's side
    -- the pre-fix code took `missed_entries[0]` unconditionally, so this
    one-bar round trip corrected `BUY 1.0` against a ledger of −1.0."""
    d = R.reconcile(inp(_x6_fills(), ledger_position=-1.0, real_position=0.0))
    assert len(d.corrections) == 1
    c = d.corrections[0]
    assert c.kind == "MARKET_CORRECT" and c.side == T.Side.SELL and abs(c.qty - 1.0) < 1e-9 and c.intent == "S"

def test_missed_entry_representative_is_list_order_independent():
    """NEW-2 (X6b): the same fills in the other order must give the same
    (correct-side) answer -- pre-fix the outcome flipped with list order."""
    fills = _x6_fills()
    reordered = [fills[2], fills[0], fills[1]]
    a = R.reconcile(inp(_x6_fills(), ledger_position=-1.0, real_position=0.0))
    b = R.reconcile(inp(reordered, ledger_position=-1.0, real_position=0.0))
    assert [(c.kind, c.side, c.qty, c.intent) for c in a.corrections] == [(c.kind, c.side, c.qty, c.intent) for c in b.corrections]
    assert b.corrections[0].side == T.Side.SELL

def test_opposite_side_qty_divergent_trims_to_zero_then_tops_up():
    """NEW-3 (X7b): `basis` and `ledger` on opposite sides is TWO orders,
    not one full-swing order -- an ungated reduce-only trim of `|basis|`
    back to flat, then a gated/budgeted `TOP_UP |ledger|`. Pre-fix: a
    single `TOP_UP BUY 1.5` (right net outcome, but the reducing half was
    mis-labelled exposure-increasing)."""
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, qty=1.0, intent="L")],
                        ledger_position=1.0, real_position=-0.5, our_signed_fills=-0.5))
    assert len(d.corrections) == 2
    trim, top = d.corrections
    assert trim.kind == "REDUCE_ONLY_TRIM" and trim.side == T.Side.BUY and abs(trim.qty - 0.5) < 1e-9
    assert top.kind == "TOP_UP" and top.side == T.Side.BUY and abs(top.qty - 1.0) < 1e-9

def test_opposite_side_qty_divergent_under_flat_only_still_trims_to_zero():
    """NEW-3 (X7): the case the split exists for -- under FLAT_ONLY the
    reduce-only half is exactly the order FLAT_ONLY is meant to permit, so
    it must still ship while the top-up is refused and carried. Pre-fix the
    whole 1.5 was one exposure-increasing order and the venue stayed short
    under a FLAT_ONLY stop."""
    d = R.reconcile(inp([cf(C.FillClass.QTY_DIVERGENT, qty=1.0, intent="L")],
                        ledger_position=1.0, real_position=-0.5, our_signed_fills=-0.5, stop_level=T.StopLevel.FLAT_ONLY))
    assert len(d.corrections) == 1
    trim = d.corrections[0]
    assert trim.kind == "REDUCE_ONLY_TRIM" and trim.side == T.Side.BUY and abs(trim.qty - 0.5) < 1e-9
    assert abs(d.residual_qty - 1.0) < 1e-9 and d.counters.get("refused_by_own_stop") == 1

def test_account_mismatch_with_a_missed_entry_marks_the_cycle_skipped():
    """NEW-5 (X10): a MISSED entry dropped by the L8 account cross-check is
    a cycle the reconciler declined to act on -- `skipped_cycle` must say
    so (Task 8 writes the `cycle_skipped` row from it); pre-fix only the
    STOP row recorded it."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, qty=1.0)], our_signed_fills=0.0, real_position=5.0, ledger_position=1.0))
    assert d.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "account_mismatch")
    assert d.corrections == [] and d.skipped_cycle and d.counters.get("skipped_cycle") == 1

def test_unreconcilable_sides_escalates_flat_only_hold():
    """X13: a MISSED fill that fails BOTH gates because the ledger and our
    own fills sit on OPPOSITE sides is not an ordinary "nothing to do" --
    the two sources disagree about which way the position points and no
    correction is derivable, so escalate `FLAT_ONLY / HOLD` (through the
    shared `_escalate`, so it stays monotonic and gates this same
    decision's own corrections). Pre-fix: silently counted, no STOP."""
    d = R.reconcile(inp([cf(C.FillClass.MISSED, leg="EXIT", is_long=True, intent="XL"), cf(C.FillClass.MISSED, leg="ENTRY", is_long=False, intent="S")],
                        ledger_position=-1.0, real_position=1.0, our_signed_fills=1.0))
    assert d.stop == (T.StopLevel.FLAT_ONLY, T.StopDisposition.HOLD, "unreconcilable_sides")
    assert d.corrections == [] and d.counters.get("skipped_position_mismatch") == 1
