from dataclasses import replace

import pytest

from pineforge_live import types as T
from pineforge_live.core.riskguard import RiskLimits
from pineforge_live.execution.risk import (
    UTC_DAY_MS, MarginQuietState, RiskInputs, RiskResponse, RuntimeRiskConfig,
    StartupRequirements, advance_margin_quiet, evaluate_risk, validate_startup,
)


LIMITS = RiskLimits(100, 100_000, 10_000, 8, 32, 100, 50, 1000, 2000, 3, 2, 2, 1, 500)
CONFIG = RuntimeRiskConfig(100, 0.125, 1000, ("TRADING",),
                           RiskResponse(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE),
                           RiskResponse(T.StopLevel.HARD, T.StopDisposition.HOLD),
                           RiskResponse(T.StopLevel.HARD, T.StopDisposition.HOLD))
ACCOUNT = T.AccountState(1000, 900, 0, 0.125, None, 100, 0, "USDT", 5, "isolated", "ONE_WAY")
HEALTHY = RiskInputs(
    now_ms=UTC_DAY_MS + 10_000, account=ACCOUNT, venue_position=0, ledger_position=0,
    feed_updated_ms=UTC_DAY_MS + 10_000, eval_updated_ms=UTC_DAY_MS + 10_000,
    daily_realized_pnl=0, realized_pnl_utc_day=1, bar_mismatch_streak=0,
    shadow_marked_equity=1000, ledger_marked_equity=1000, shadow_realized_pnl=0,
    ledger_realized_pnl=0, divergence_reference_equity=1000, recompute_ms_p99=100,
    clock_skew_ms=0, venue_state="TRADING",
)


def causes(inputs=HEALTHY, limits=LIMITS, config=CONFIG):
    return {b.cause for b in evaluate_risk(limits, config, inputs)}


def test_complete_snapshot_passes_and_evaluation_is_pure():
    assert evaluate_risk(LIMITS, CONFIG, HEALTHY) == ()
    assert evaluate_risk(LIMITS, CONFIG, HEALTHY) == ()


@pytest.mark.parametrize("field,value,expected", [
    ("daily_realized_pnl", -100, set()),
    ("daily_realized_pnl", -100.01, {"max_daily_realized_loss"}),
    ("daily_realized_pnl", 1000, set()),
    ("feed_updated_ms", HEALTHY.now_ms - 1000, set()),
    ("feed_updated_ms", HEALTHY.now_ms - 1001, {"stale_feed_ms"}),
    ("eval_updated_ms", HEALTHY.now_ms - 2000, set()),
    ("eval_updated_ms", HEALTHY.now_ms - 2001, {"stale_eval_ms"}),
    ("bar_mismatch_streak", 2, set()),
    ("bar_mismatch_streak", 3, {"bar_mismatch_streak"}),
    ("recompute_ms_p99", 500, set()),
    ("recompute_ms_p99", 500.01, {"recompute_ms_p99_max"}),
    ("clock_skew_ms", -100, set()),
    ("clock_skew_ms", 100, set()),
    ("clock_skew_ms", -101, {"clock_skew"}),
    ("clock_skew_ms", 101, {"clock_skew"}),
    ("venue_state", "HALTED", {"venue_state"}),
])
def test_threshold_direction_and_equality(field, value, expected):
    assert causes(replace(HEALTHY, **{field: value})) == expected


@pytest.mark.parametrize("field", [
    "venue_position", "ledger_position", "daily_realized_pnl", "shadow_marked_equity",
    "ledger_marked_equity", "shadow_realized_pnl", "ledger_realized_pnl",
    "divergence_reference_equity", "recompute_ms_p99", "clock_skew_ms",
])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf"), True, "0"])
def test_missing_nonfinite_or_non_numeric_values_refuse(field, value):
    breaches = evaluate_risk(LIMITS, CONFIG, replace(HEALTHY, **{field: value}))
    breach = next(b for b in breaches if b.cause == f"risk_input:{field}")
    assert (breach.level, breach.disposition) == (T.StopLevel.HARD, T.StopDisposition.HOLD)


@pytest.mark.parametrize("field,value", [
    ("now_ms", None), ("now_ms", -1), ("now_ms", True),
    ("feed_updated_ms", None), ("eval_updated_ms", HEALTHY.now_ms + 1),
    ("feed_updated_ms", 1.0), ("bar_mismatch_streak", -1),
    ("bar_mismatch_streak", True), ("bar_mismatch_streak", 0.5),
    ("recompute_ms_p99", -0.1), ("divergence_reference_equity", 0),
    ("divergence_reference_equity", -1000), ("account", None), ("venue_state", None),
])
def test_invalid_critical_observations_are_explicit_refusals(field, value):
    assert f"risk_input:{field}" in causes(replace(HEALTHY, **{field: value}))


def test_all_independent_failures_are_returned_with_explicit_responses():
    snapshot = replace(HEALTHY, daily_realized_pnl=-101, clock_skew_ms=101, venue_state="HALTED")
    breaches = {b.cause: b for b in evaluate_risk(LIMITS, CONFIG, snapshot)}
    assert set(breaches) == {"max_daily_realized_loss", "clock_skew", "venue_state"}
    assert breaches["max_daily_realized_loss"].level is T.StopLevel.FLAT_ONLY
    assert breaches["venue_state"].level is T.StopLevel.HARD
    assert all(b.detail for b in breaches.values())


def test_utc_day_roll_requires_fresh_daily_aggregate_without_carrying_prior_loss():
    before = replace(HEALTHY, now_ms=2 * UTC_DAY_MS - 1,
                     feed_updated_ms=2 * UTC_DAY_MS - 1, eval_updated_ms=2 * UTC_DAY_MS - 1,
                     daily_realized_pnl=-101)
    assert causes(before) == {"max_daily_realized_loss"}
    after = replace(before, now_ms=2 * UTC_DAY_MS)
    assert causes(after) == {"risk_input:realized_pnl_utc_day"}
    assert not causes(replace(after, realized_pnl_utc_day=2, daily_realized_pnl=0))
    assert causes(replace(after, realized_pnl_utc_day=2, daily_realized_pnl=-101)) == {"max_daily_realized_loss"}


def test_equity_divergence_uses_explicit_denominator_and_supplied_marked_equity():
    # Wallet is deliberately unrelated: supplied close-marked equity is authoritative.
    account = replace(ACCOUNT, wallet=999_999)
    assert not causes(replace(HEALTHY, account=account, shadow_marked_equity=980))
    assert causes(replace(HEALTHY, account=account, shadow_marked_equity=979)) == {"unexplained_divergence_pct"}
    assert causes(replace(HEALTHY, shadow_marked_equity=1021)) == {"unexplained_divergence_pct"}
    assert not causes(replace(HEALTHY, shadow_marked_equity=1021, divergence_reference_equity=2000))


@pytest.mark.parametrize("venue,ledger", [(1, 1), (-1, -1), (0, 1), (1, 0)])
def test_realized_only_divergence_is_not_compared_unless_both_sides_are_flat(venue, ledger):
    account = replace(ACCOUNT, liquidation_price=90 if venue >= 0 else 110)
    snapshot = replace(HEALTHY, account=account, venue_position=venue, ledger_position=ledger,
                       shadow_realized_pnl=None, ledger_realized_pnl=float("nan"))
    assert not causes(snapshot)


def test_flat_realized_divergence_checks_signed_pnl_even_when_marked_equity_agrees():
    assert not causes(replace(HEALTHY, shadow_realized_pnl=-10, ledger_realized_pnl=10))
    breaches = evaluate_risk(LIMITS, CONFIG, replace(HEALTHY, shadow_realized_pnl=-11, ledger_realized_pnl=10))
    assert [b.cause for b in breaches] == ["unexplained_divergence_pct"]
    assert "basis=realized_pnl" in breaches[0].detail


@pytest.mark.parametrize("position,liquidation,breached", [
    (1, 99, False), (1, 99.01, True), (1, 101, True), (1, 80, False),
    (-1, 101, False), (-1, 100.99, True), (-1, 99, True), (-1, 120, False),
])
def test_liquidation_distance_is_directional_and_flatten_is_specified(position, liquidation, breached):
    snapshot = replace(HEALTHY, venue_position=position, ledger_position=position,
                       account=replace(ACCOUNT, liquidation_price=liquidation))
    result = evaluate_risk(LIMITS, CONFIG, snapshot)
    assert bool(result) is breached
    if breached:
        assert [(b.cause, b.level, b.disposition) for b in result] == [
            ("liquidation_distance_pct_min", T.StopLevel.FLAT_ONLY, T.StopDisposition.FLATTEN)]


@pytest.mark.parametrize("field", ["mark_price", "liquidation_price"])
@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), True])
def test_open_position_requires_valid_mark_and_liquidation(field, value):
    snapshot = replace(HEALTHY, venue_position=1, ledger_position=1,
                       account=replace(ACCOUNT, liquidation_price=90, **({field: value} if field != "liquidation_price" else {})))
    if field == "liquidation_price":
        snapshot = replace(snapshot, account=replace(snapshot.account, liquidation_price=value))
    assert f"risk_input:account.{field}" in causes(snapshot)


def test_no_liquidation_price_is_required_when_flat_but_margin_ratio_is_required():
    assert not causes(HEALTHY)
    assert causes(replace(HEALTHY, account=replace(ACCOUNT, margin_ratio=None))) == {"risk_input:account.margin_ratio"}


def test_margin_spike_and_call_block_only_for_quiet_window_and_do_not_clear_early():
    state = MarginQuietState()
    assert state.blocks_increase(1000)
    state = advance_margin_quiet(CONFIG, state, now_ms=1000, margin_ratio=0.125)
    assert not state.blocks_increase(1000)
    state = advance_margin_quiet(CONFIG, state, now_ms=1100, margin_ratio=0.25)  # exact delta
    assert state.quiet_until_ms == 2100 and state.blocks_increase(2099)
    assert not state.blocks_increase(2100)
    state = advance_margin_quiet(CONFIG, state, now_ms=1200, margin_ratio=0.125, margin_call_ms=1150)
    assert state.quiet_until_ms == 2150
    state = advance_margin_quiet(CONFIG, state, now_ms=1300, margin_ratio=0.125, margin_call_ms=1000)
    assert state.quiet_until_ms == 2150  # old replay cannot shorten the gate
    assert state.blocks_increase(2149) and not state.blocks_increase(2150)


def test_below_threshold_margin_delta_and_signed_decreases_do_not_open_gate():
    state = MarginQuietState(0.25, 0, 1000)
    assert not advance_margin_quiet(CONFIG, state, now_ms=1100, margin_ratio=0.374).blocks_increase(1100)
    assert not advance_margin_quiet(CONFIG, state, now_ms=1100, margin_ratio=0).blocks_increase(1100)


@pytest.mark.parametrize("kwargs", [
    {"margin_ratio": None}, {"margin_ratio": float("nan")}, {"margin_ratio": -0.1},
    {"margin_ratio": True}, {"now_ms": 999}, {"margin_call_ms": 1101},
])
def test_invalid_margin_observation_refuses_and_leaves_existing_gate_intact(kwargs):
    state = MarginQuietState(0.25, 2000, 1000)
    args = {"now_ms": 1100, "margin_ratio": 0.25, **kwargs}
    with pytest.raises(ValueError):
        advance_margin_quiet(CONFIG, state, **args)
    assert state.quiet_until_ms == 2000 and state.blocks_increase(1100)


@pytest.mark.parametrize("field,value", [
    ("max_daily_realized_loss", -1), ("max_daily_realized_loss", float("nan")),
    ("stale_feed_ms", 0), ("stale_eval_ms", True), ("bar_mismatch_streak", 1.5),
    ("unexplained_divergence_pct", float("inf")), ("liquidation_distance_pct_min", -1),
    ("recompute_ms_p99_max", None),
])
def test_invalid_declared_limits_raise_configuration_error(field, value):
    with pytest.raises(ValueError, match=field):
        evaluate_risk(replace(LIMITS, **{field: value}), CONFIG, HEALTHY)


@pytest.mark.parametrize("field,value", [
    ("clock_skew_ms_max", -1), ("clock_skew_ms_max", True),
    ("margin_ratio_spike_delta_min", 0), ("margin_ratio_spike_delta_min", float("inf")),
    ("margin_quiet_ms", 0), ("allowed_venue_states", ()), ("allowed_venue_states", "TRADING"),
    ("limit_response", None), ("missing_input_response", None), ("venue_state_response", None),
])
def test_absent_or_invalid_explicit_runtime_configuration_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(CONFIG, **{field: value})


@pytest.mark.parametrize("level,disposition", [
    (T.StopLevel.NONE, T.StopDisposition.NONE), (T.StopLevel.HARD, T.StopDisposition.NONE),
    ("HARD", T.StopDisposition.HOLD), (T.StopLevel.HARD, "HOLD"),
])
def test_response_cannot_silently_permit_risk(level, disposition):
    with pytest.raises(ValueError):
        RiskResponse(level, disposition)


INSTRUMENT = T.InstrumentId("mock", T.MarketType.PERP, "ETHUSDT")
SYMINFO = T.EngineSyminfo("ETHUSDT", "mock:ETHUSDT", "mock", "ETH", "crypto", "USDT", "ETH",
                          0.01, 100, 1, 1, "24x7", "UTC", "base", "Ether")
STARTUP = StartupRequirements(INSTRUMENT, SYMINFO, 5, "isolated", 20, 20, 1, 10)
CONSTRAINTS = T.VenueConstraints(
    tick_size=0.01, lot_step=0.001, min_qty=0.001, max_qty=1000, market_max_qty=100,
    min_notional=5, price_bands=(1, 1_000_000), stop_price_bands=(1, 1_000_000),
    max_open_orders=100, max_open_conditional_orders=10, position_modes=("ONE_WAY",),
    leverage=5, margin_modes=("isolated",), conditional_order_types=("STOP_MARKET",),
    close_position_supported=True, reduce_only_min_notional_exempt=True,
    trigger_bases=(T.TriggerBasis.MARK,), order_lookup_retention_ms=86_400_000,
    client_id_on_fills=True, orders_per_10s=50, request_weight_per_min=1200,
)


def startup_causes(requirements=STARTUP, **kwargs):
    return {b.cause for b in validate_startup(requirements, **{
        "resolved_instrument": INSTRUMENT, "account": ACCOUNT, "constraints": CONSTRAINTS, **kwargs})}


def test_startup_evidence_matches_and_missing_evidence_refuses():
    assert not startup_causes()
    assert startup_causes(account=None, constraints=None, resolved_instrument=None) == {
        "startup:account", "startup:constraints", "startup:instrument"}


@pytest.mark.parametrize("field,value,cause", [
    ("currency", "USD", "currency"), ("position_mode", "hedge", "position_mode"),
    ("leverage", 10, "leverage"), ("leverage", True, "leverage"),
    ("margin_mode", "cross", "margin_mode"),
])
def test_startup_account_mismatch(field, value, cause):
    assert startup_causes(account=replace(ACCOUNT, **{field: value})) == {f"startup:{cause}"}


@pytest.mark.parametrize("field,value,cause", [
    ("tick_size", 0.02, "mintick"), ("tick_size", float("nan"), "mintick"),
    ("tick_size", 0, "mintick"), ("position_modes", ("hedge",), "supported_position_mode"),
    ("leverage", 10, "constraint_leverage"), ("margin_modes", ("cross",), "supported_margin_mode"),
])
def test_startup_constraints_mismatch(field, value, cause):
    assert startup_causes(constraints=replace(CONSTRAINTS, **{field: value})) == {f"startup:{cause}"}


def test_startup_contract_and_compiled_margin_checks_are_both_directional():
    assert startup_causes(replace(STARTUP, contract_multiplier=2)) == {"startup:pointvalue"}
    assert startup_causes(replace(STARTUP, compiled_margin_long_pct=10)) == {"startup:compiled_margin_long_pct"}
    assert startup_causes(replace(STARTUP, compiled_margin_short_pct=10)) == {"startup:compiled_margin_short_pct"}
    assert startup_causes(resolved_instrument=replace(INSTRUMENT, market_type=T.MarketType.SPOT)) == {"startup:instrument"}


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), True])
def test_startup_entry_slippage_requires_explicit_finite_positive_budget(value):
    with pytest.raises(ValueError, match="max_entry_slip_bps"):
        replace(STARTUP, max_entry_slip_bps=value)
