from dataclasses import replace

import pytest

from pineforge_live import types as T
from pineforge_live.core.book import Intent
from pineforge_live.core.ids import IntentKey
from pineforge_live.core.probe import ProbeResult
from pineforge_live.execution.protection import (
    DeadmanConfig, ProtectionRole, RateLimiter, check_stop_transition, plan_deadman, plan_mirrors,
)


CONSTRAINTS = T.VenueConstraints(
    tick_size=0.1, lot_step=0.01, min_qty=0.01, max_qty=1000, market_max_qty=100,
    min_notional=5, price_bands=(1, 1000), stop_price_bands=(1, 1000),
    max_open_orders=100, max_open_conditional_orders=10, position_modes=("ONE_WAY",),
    leverage=5, margin_modes=("isolated",), conditional_order_types=("STOP_MARKET", "TAKE_PROFIT_MARKET"),
    close_position_supported=True, reduce_only_min_notional_exempt=True,
    trigger_bases=(T.TriggerBasis.MARK, T.TriggerBasis.LAST), order_lookup_retention_ms=86_400_000,
    client_id_on_fills=True, orders_per_10s=10, request_weight_per_min=20,
)
CAPS = T.Capabilities(True, True, True, (T.TriggerBasis.MARK, T.TriggerBasis.LAST), False, True, 1)
CONFIG = DeadmanConfig(5, 0.1)


def plan(**kwargs):
    args = dict(position=2, is_open=True, cycle_seq=1, mark_price=100, liquidation_price=80,
                ledger_stop_prices=(98,), constraints=CONSTRAINTS)
    return plan_deadman(CONFIG, **(args | kwargs))


@pytest.mark.parametrize("position,liquidation,stops,expected", [
    (2, 80, (98,), 94), (-2, 120, (102,), 106),
    (2, 80, (98, 96), 88), (-2, 120, (102, 104), 112),
    (2, 80, (), 95), (-2, 120, (), 105),
])
def test_deadman_nominal_width_and_fallback_floor_are_mark_based(position, liquidation, stops, expected):
    result = plan(position=position, liquidation_price=liquidation, ledger_stop_prices=stops)
    assert result.refusal is None and result.order.stop_price == expected
    assert result.order.qty == abs(position) and result.order.close_position and result.order.reduce_only
    assert result.order.trigger_basis is T.TriggerBasis.MARK
    assert result.order.side is (T.Side.SELL if position > 0 else T.Side.BUY)


@pytest.mark.parametrize("position,liquidation,stops,expected", [
    (2, 96, (98,), 96.4), (-2, 104, (102,), 103.6),
    (2, 96.03, (98,), 96.5), (-2, 103.97, (102,), 103.5),
])
def test_liquidation_cap_and_tick_quantization_round_toward_mark(position, liquidation, stops, expected):
    result = plan(position=position, liquidation_price=liquidation, ledger_stop_prices=stops)
    assert result.refusal is None and result.order.stop_price == expected
    assert result.price_interval[0] <= expected <= result.price_interval[1]


def test_stop_bands_limit_width_and_can_force_a_narrow_interval():
    result = plan(constraints=replace(CONSTRAINTS, stop_price_bands=(97, 99)))
    assert result.order.stop_price == 97 and result.width_pct == 3
    result = plan(position=-2, liquidation_price=120, ledger_stop_prices=(102,),
                  constraints=replace(CONSTRAINTS, stop_price_bands=(101, 103)))
    assert result.order.stop_price == 103


@pytest.mark.parametrize("kwargs", [
    {"liquidation_price": 98}, {"liquidation_price": 101},
    {"constraints": replace(CONSTRAINTS, stop_price_bands=(98.1, 99))},
    {"ledger_stop_prices": (98.95,), "constraints": replace(CONSTRAINTS, stop_price_bands=(98.91, 98.99))},
])
@pytest.mark.parametrize("is_open", [False, True])
def test_empty_interval_refuses_entry_and_demands_flatten_only_when_already_open(kwargs, is_open):
    result = plan(**(kwargs | {"is_open": is_open}))
    assert result.order is None and result.refusal.cause == "deadman_empty_interval"
    assert result.refusal.refuse_entry and result.refusal.flatten is is_open


def test_exact_ledger_floor_and_single_band_tick_are_allowed():
    config = DeadmanConfig(5, 0)
    result = plan_deadman(config, position=1, is_open=False, cycle_seq=1, mark_price=100,
                          liquidation_price=98, ledger_stop_prices=(98,),
                          constraints=replace(CONSTRAINTS, stop_price_bands=(98, 98)))
    assert result.order.stop_price == 98 and result.refusal is None


@pytest.mark.parametrize("field,value", [
    ("position", None), ("mark_price", 0), ("mark_price", float("nan")),
    ("mark_price", float("inf")), ("mark_price", True), ("liquidation_price", None),
    ("liquidation_price", -1), ("ledger_stop_prices", None),
    ("ledger_stop_prices", (100,)), ("ledger_stop_prices", (101,)),
    ("ledger_stop_prices", (float("nan"),)),
    ("constraints", replace(CONSTRAINTS, tick_size=0)),
    ("constraints", replace(CONSTRAINTS, stop_price_bands=(1000, 1))),
    ("constraints", replace(CONSTRAINTS, trigger_bases=(T.TriggerBasis.LAST,))),
])
def test_deadman_missing_invalid_or_unsupported_inputs_refuse(field, value):
    result = plan(**{field: value})
    assert result.order is None and result.refusal.refuse_entry and result.refusal.flatten


def test_flat_deadman_plan_requests_cycle_cleanup_without_fabricating_an_order():
    result = plan(position=0, is_open=False)
    assert result.order is None and result.refusal is None and result.cancel_existing_when_flat
    assert plan(position=0, is_open=True).refusal is not None


@pytest.mark.parametrize("field,value", [("max_loss_pct", None), ("max_loss_pct", 0),
    ("max_loss_pct", float("nan")), ("margin_buffer", -0.1), ("margin_buffer", 1), ("margin_buffer", True)])
def test_deadman_config_has_no_implicit_risk_defaults(field, value):
    with pytest.raises(ValueError):
        replace(CONFIG, **{field: value})


def intent(oid="exit", **kwargs):
    args = dict(key=IntentKey(oid, "EXIT", "entry", 1), index=0, is_long=False, kind="EXIT",
                from_entry="entry", stop=98, limit=105, activation=None, level_resolved=True,
                created_bar=1, qty=None, qty_percent=100, requested_partial=False,
                full_percent_exit_request=True, content_hash="unused")
    return Intent(**(args | kwargs))


def mirrors(*, intents=None, levels=None, probe=None, **kwargs):
    intents = [intent()] if intents is None else intents
    book = {it.key.s: it for it in intents}
    levels = {key: (it.stop, it.limit, it.activation) for key, it in book.items()} if levels is None else levels
    probe = probe or ProbeResult(2, T.NormalizedBar(0, 100, 101, 99, 100, 1, 1), [], [], [], levels, False, 1, False)
    args = dict(position=2, cycle_seq=1, open_lots=1, constraints=CONSTRAINTS, capabilities=CAPS,
                trail_refresh_policy="bar_open_level")
    return plan_mirrors(book, probe, **(args | kwargs))


def test_mirror_pair_has_distinct_roles_and_latest_probe_levels_with_full_qty():
    it = intent()
    result = mirrors(intents=[it], levels={it.key.s: (97.5, 106, None)})
    assert not result.refusals
    assert [(o.role, o.kind, o.stop_price) for o in result.orders] == [
        (ProtectionRole.STOP, T.OrderKind.STOP_MARKET, 97.5),
        (ProtectionRole.TARGET, T.OrderKind.TAKE_PROFIT_MARKET, 106)]
    assert all(o.qty == 2 and o.side is T.Side.SELL and o.trigger_basis is T.TriggerBasis.LAST
               and o.reduce_only and o.close_position for o in result.orders)


def test_mirror_short_pair_and_entries_never_preplaced():
    entry = intent("entry", kind="ENTRY", is_long=True)
    result = mirrors(intents=[entry, intent(is_long=True, stop=102, limit=95)], position=-2)
    assert not result.refusals and len(result.orders) == 2
    assert all(o.side is T.Side.BUY and o.intent_key != entry.key.s for o in result.orders)
    assert mirrors(intents=[entry]).orders == ()


@pytest.mark.parametrize("it", [
    intent(requested_partial=True), intent(qty_percent=50), intent(qty=1),
    intent(qty=float("nan")), intent(qty_percent=None, full_percent_exit_request=False),
])
def test_partial_or_unproven_full_position_sizing_refuses_entire_set(it):
    result = mirrors(intents=[it])
    assert not result.orders and result.refusals


def test_pyramiding_multiple_pairs_and_wrong_side_are_explicitly_unsupported():
    assert mirrors(open_lots=2).refusals[0].cause == "mirror_multi_lot"
    result = mirrors(intents=[intent("a"), intent("b")])
    assert not result.orders and {r.cause for r in result.refusals} == {"mirror_multiple_pairs"}
    assert mirrors(intents=[intent(is_long=True)]).refusals[0].cause == "mirror_side"


def test_unresolved_levels_are_not_mirrored_and_intrabar_requires_own_resolution_evidence():
    it = intent(level_resolved=False)
    assert mirrors(intents=[it]).orders == ()
    assert mirrors(trail_refresh_policy="intrabar_best").refusals[0].cause == "mirror_resolution"
    assert len(mirrors(intents=[it], trail_refresh_policy="intrabar_best", resolved={it.key.s: True}).orders) == 2
    assert mirrors(trail_refresh_policy="intrabar_best", resolved={it.key.s: False}).orders == ()


def test_missing_and_aborted_probe_never_mean_cancel_all_protection():
    assert mirrors(levels={}).refusals[0].cause == "mirror_levels"
    aborted = ProbeResult(2, T.NormalizedBar(0, 100, 101, 99, 100, 1, 1), [], [], [], {}, False, 1, False, aborted=True)
    result = mirrors(probe=aborted)
    assert not result.orders and result.refusals[0].cause == "mirror_probe"


@pytest.mark.parametrize("stop,target", [(100, 105), (101, 105), (98, 99), (98.05, 105), (float("nan"), 105)])
def test_mirror_bad_or_already_triggered_prices_do_not_invent_convert(stop, target):
    result = mirrors(intents=[intent(stop=stop, limit=target)])
    assert not result.orders and result.refusals


def test_mirror_requires_both_venue_and_adapter_capabilities():
    assert mirrors(capabilities=replace(CAPS, close_position=False)).refusals
    assert mirrors(constraints=replace(CONSTRAINTS, conditional_order_types=("STOP_MARKET",))).refusals
    assert mirrors(position=0, open_lots=0).orders == ()
    assert mirrors(position=0, open_lots=1).refusals


@pytest.mark.parametrize("position,liquidation,stops,closer,wider", [(2, 80, (98,), 95, 93), (-2, 120, (102,), 105, 107)])
def test_stop_transition_preserves_or_tightens_each_current_cycle_protective_stop(position, liquidation, stops, closer, wider):
    old = plan(position=position, liquidation_price=liquidation, ledger_stop_prices=stops).order
    for price in (old.stop_price, closer):
        assert not check_stop_transition(position=position, cycle_seq=1, previous=[old], proposed=[replace(old, stop_price=price)])
    for changed in (replace(old, stop_price=wider), replace(old, qty=1), replace(old, reduce_only=False),
                    replace(old, trigger_basis=T.TriggerBasis.LAST), replace(old, side=T.Side.BUY if position > 0 else T.Side.SELL)):
        assert check_stop_transition(position=position, cycle_seq=1, previous=[old], proposed=[changed])
    assert check_stop_transition(position=position, cycle_seq=1, previous=[old], proposed=[])
    assert not check_stop_transition(position=position, cycle_seq=2, previous=[old], proposed=[])


def test_target_cannot_substitute_for_stop_and_same_cycle_duplicates_are_ambiguous():
    old = plan().order
    assert check_stop_transition(position=2, cycle_seq=1, previous=[old], proposed=[replace(old, role=ProtectionRole.TARGET)])
    assert check_stop_transition(position=2, cycle_seq=1, previous=[old], proposed=[old, old])
    assert not check_stop_transition(position=0, cycle_seq=1, previous=[old], proposed=[])


class Clock:
    now = 0

    def __call__(self):
        return self.now


def limiter(clock):
    return RateLimiter(CONSTRAINTS, emergency_orders_reserve=2, emergency_weight_reserve=5, now_ms=clock)


def test_discretionary_cannot_draw_reserve_and_emergency_shares_total_cap():
    clock = Clock(); rates = limiter(clock)
    assert rates.acquire(T.Lane.DISCRETIONARY, orders=8, weight=15).allowed
    blocked = rates.acquire(T.Lane.DISCRETIONARY, orders=1, weight=1)
    assert not blocked.allowed and blocked.retry_after_ms == 60_000
    assert rates.acquire(T.Lane.EMERGENCY, orders=2, weight=5).allowed
    assert not rates.acquire(T.Lane.EMERGENCY, orders=1, weight=1).allowed


def test_emergency_can_use_unspent_normal_budget_but_not_exceed_shared_budget():
    rates = limiter(Clock())
    assert rates.acquire(T.Lane.EMERGENCY, orders=7, weight=10).allowed
    assert rates.acquire(T.Lane.DISCRETIONARY, orders=3, weight=5).allowed
    assert not rates.acquire(T.Lane.DISCRETIONARY, orders=1, weight=0).allowed
    assert rates.acquire(T.Lane.EMERGENCY, orders=0, weight=5).allowed
    assert not rates.acquire(T.Lane.EMERGENCY, orders=0, weight=1).allowed


def test_failed_two_budget_acquisition_does_not_spend_other_budget():
    rates = limiter(Clock())
    assert rates.acquire(T.Lane.DISCRETIONARY, orders=0, weight=15).allowed
    assert not rates.acquire(T.Lane.DISCRETIONARY, orders=8, weight=1).allowed
    assert rates.acquire(T.Lane.DISCRETIONARY, orders=8, weight=0).allowed


def test_independent_windows_expire_exactly_on_boundary_and_retry_is_sufficient():
    clock = Clock(); rates = limiter(clock)
    assert rates.acquire(T.Lane.EMERGENCY, orders=10, weight=20).allowed
    clock.now = 9999
    assert rates.acquire(T.Lane.EMERGENCY, orders=1, weight=0).retry_after_ms == 1
    clock.now = 10_000
    assert rates.acquire(T.Lane.EMERGENCY, orders=10, weight=0).allowed
    assert rates.acquire(T.Lane.EMERGENCY, orders=0, weight=1).retry_after_ms == 50_000
    clock.now = 60_000
    assert rates.acquire(T.Lane.DISCRETIONARY, orders=8, weight=15).allowed


def test_retry_waits_until_enough_cost_expires_not_just_first_event():
    clock = Clock(); rates = limiter(clock)
    assert rates.acquire(T.Lane.EMERGENCY, orders=2, weight=0).allowed
    clock.now = 1000
    assert rates.acquire(T.Lane.EMERGENCY, orders=8, weight=0).allowed
    clock.now = 2000
    result = rates.acquire(T.Lane.EMERGENCY, orders=5, weight=0)
    assert not result.allowed and result.retry_after_ms == 9000
    clock.now += result.retry_after_ms
    assert rates.acquire(T.Lane.EMERGENCY, orders=5, weight=0).allowed


def test_impossible_cost_has_no_retry_and_clock_rollback_refuses():
    clock = Clock(); rates = limiter(clock)
    result = rates.acquire(T.Lane.DISCRETIONARY, orders=9, weight=0)
    assert not result.allowed and result.retry_after_ms is None
    clock.now = 100
    assert rates.acquire(T.Lane.EMERGENCY, orders=1, weight=1).allowed
    clock.now = 99
    with pytest.raises(ValueError, match="monotonic"):
        rates.acquire(T.Lane.EMERGENCY, orders=1, weight=1)


@pytest.mark.parametrize("reserve", [None, 0, -1, True, 11])
def test_rate_reserve_must_be_explicit_and_reachable(reserve):
    with pytest.raises(ValueError):
        RateLimiter(CONSTRAINTS, emergency_orders_reserve=reserve, emergency_weight_reserve=5, now_ms=Clock())


@pytest.mark.parametrize("orders,weight", [(0, 0), (True, 1), (-1, 1), (1.5, 1), (1, float("nan"))])
def test_request_costs_cannot_bypass_rate_accounting(orders, weight):
    with pytest.raises(ValueError):
        limiter(Clock()).acquire(T.Lane.EMERGENCY, orders=orders, weight=weight)
