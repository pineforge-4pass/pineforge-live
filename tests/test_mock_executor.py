"""Deterministic Executor boundary, lost acknowledgements and safe matching."""
import asyncio
from dataclasses import replace

import pytest

from pineforge_live import types as T
from pineforge_live.adapters.base import Executor
from pineforge_live.adapters.mock import MockExecutor, SubmitFault, default_constraints


INSTRUMENT = T.InstrumentId("TAPE", T.MarketType.PERP, "TESTUSD")


def action(client_id="client-1", **changes):
    return replace(T.OrderAction(
        client_id=client_id, intent_key="entry|1", level_version=0, action_seq=1,
        kind=T.OrderKind.MARKET, side=T.Side.BUY, qty=1.0, price=None,
        stop_price=None, reduce_only=False, close_position=False, position_side="BOTH",
        tif="GTC", trigger_basis=T.TriggerBasis.LAST, lane=T.Lane.DISCRETIONARY,
        cls="TRIGGER", reason="test",
    ), **changes)


async def events(executor):
    return [event async for event in executor.events()]


def test_timeout_after_accept_is_adopted_without_another_physical_order():
    async def run():
        executor = MockExecutor(INSTRUMENT, faults=[SubmitFault(timeout="after_accept")])
        assert isinstance(executor, Executor)
        executor.advance(100.0, 1000)
        with pytest.raises(TimeoutError, match="after acceptance"):
            await executor.submit(action())
        adopted = await executor.lookup("client-1", None)
        assert adopted.status is T.OrderStatus.FILLED
        assert await executor.submit(action()) == adopted
        assert await executor.lookup(None, adopted.venue_order_id) == adopted
        assert await executor.lookup("wrong", adopted.venue_order_id) is None
        fills, cursor = await executor.fills_since(INSTRUMENT, None)
        assert len(fills) == 1 and fills[0].venue_order_id == adopted.venue_order_id
        assert await executor.position(INSTRUMENT) == 1.0
        assert await executor.fills_since(INSTRUMENT, cursor) == ([], cursor)
    asyncio.run(run())


def test_timeout_before_accept_leaves_a_safe_empty_lookup_and_retry():
    async def run():
        executor = MockExecutor(INSTRUMENT, faults=[SubmitFault(timeout="before_accept")])
        executor.advance(100.0, 1)
        with pytest.raises(TimeoutError, match="before acceptance"):
            await executor.submit(action())
        assert await executor.lookup("client-1", None) is None
        assert await executor.orders_since(INSTRUMENT, None) == ([], "orders:0")
        assert await executor.fills_since(INSTRUMENT, None) == ([], "fills:0")
        assert await events(executor) == []
        assert (await executor.submit(action())).status is T.OrderStatus.FILLED
        assert await executor.position(INSTRUMENT) == 1.0
    asyncio.run(run())


def test_partial_fill_cancel_and_new_remainder_have_distinct_ids():
    async def run():
        executor = MockExecutor(INSTRUMENT, faults=[SubmitFault(partial_fill_ratio=0.4)])
        executor.advance(100.0, 1)
        partial = await executor.submit(action())
        assert partial.status is T.OrderStatus.PARTIAL and partial.filled_qty == 0.4
        assert await executor.submit(action()) == partial
        assert (await executor.cancel(None, partial.venue_order_id)).status is T.OrderStatus.CANCELED
        remainder = await executor.submit(action("client-2", qty=0.6, action_seq=2))
        assert remainder.status is T.OrderStatus.FILLED
        assert remainder.venue_order_id != partial.venue_order_id
        executor.advance(105.0, 2)  # the canceled remainder cannot fill again
        fills, _ = await executor.fills_since(INSTRUMENT, None)
        assert [f.qty for f in fills] == [0.4, 0.6]
        assert len({f.venue_trade_id for f in fills}) == 2
        assert await executor.position(INSTRUMENT) == 1.0
        assert await executor.open_orders(INSTRUMENT) == []
    asyncio.run(run())


def test_partial_remainder_fills_on_next_advance_and_averages_prices():
    async def run():
        executor = MockExecutor(INSTRUMENT, faults=[SubmitFault(partial_fill_ratio=0.5)])
        executor.advance(100.0, 1)
        await executor.submit(action())
        executor.advance(110.0, 2)
        state = await executor.lookup("client-1", None)
        assert state.status is T.OrderStatus.FILLED and state.filled_qty == 1.0
        assert state.avg_price == 105.0
    asyncio.run(run())


def test_market_before_first_print_waits_and_partial_fills_respect_lot_step():
    async def run():
        executor = MockExecutor(INSTRUMENT, replace(default_constraints(), lot_step=0.1),
                                faults=[SubmitFault(partial_fill_ratio=0.333)])
        assert (await executor.submit(action())).status is T.OrderStatus.ACKED
        assert await executor.position(INSTRUMENT) == 0.0
        executor.advance(100.0, 1)
        assert (await executor.lookup("client-1", None)).filled_qty == 0.3
        executor.advance(110.0, 2)
        assert (await executor.lookup("client-1", None)).status is T.OrderStatus.FILLED
        assert await executor.position(INSTRUMENT) == 1.0
    asyncio.run(run())


def test_missing_client_id_and_duplicate_events_keep_trade_identity_and_position():
    async def run():
        executor = MockExecutor(INSTRUMENT, faults=[SubmitFault(omit_client_id=True, duplicate_events=1)])
        executor.advance(100.0, 1)
        state = await executor.submit(action())
        emitted = await events(executor)
        trades = [event for event in emitted if isinstance(event, T.Fill)]
        assert len(trades) == 2 and trades[0] == trades[1]
        assert trades[0].client_id is None and trades[0].venue_order_id == state.venue_order_id
        assert trades[0].cause is T.FillCause.OURS
        changes = [event for event in emitted if isinstance(event, T.OrderStateChanged)]
        assert all(event.client_id == "client-1" and event.venue_order_id == state.venue_order_id for event in changes)
        assert await executor.position(INSTRUMENT) == 1.0
        assert len((await executor.fills_since(INSTRUMENT, None))[0]) == 1
        assert await events(executor) == []
    asyncio.run(run())


def test_cursors_replay_immutable_transitions_and_reject_wrong_stream():
    async def run():
        executor = MockExecutor(INSTRUMENT, immediate_market=False)
        executor.advance(100.0, 1)
        ack = await executor.submit(action())
        first, cursor = await executor.orders_since(INSTRUMENT, None)
        assert first == [ack] and ack.status is T.OrderStatus.ACKED
        executor.advance(101.0, 2)
        delta, end = await executor.orders_since(INSTRUMENT, cursor)
        assert [s.status for s in delta] == [T.OrderStatus.FILLED]
        assert await executor.orders_since(INSTRUMENT, cursor) == (delta, end)
        assert await executor.orders_since(INSTRUMENT, end) == ([], end)
        assert first[0].status is T.OrderStatus.ACKED
        with pytest.raises(ValueError, match="cursor"):
            await executor.fills_since(INSTRUMENT, end)
        with pytest.raises(ValueError, match="ahead"):
            await executor.orders_since(INSTRUMENT, "orders:999")
    asyncio.run(run())


@pytest.mark.parametrize("position,side", [(0.5, T.Side.SELL), (-0.5, T.Side.BUY)])
def test_reduce_only_clamps_to_real_position_and_cancels_excess(position, side):
    async def run():
        executor = MockExecutor(INSTRUMENT, initial_position=position)
        executor.advance(100.0, 1)
        state = await executor.submit(action(side=side, qty=2.0, reduce_only=True))
        assert state.status is T.OrderStatus.CANCELED and state.filled_qty == abs(position)
        assert await executor.position(INSTRUMENT) == 0.0
        executor.advance(110.0, 2)
        assert await executor.position(INSTRUMENT) == 0.0
        assert (await executor.submit(action("empty", side=side, reduce_only=True))).status is T.OrderStatus.REJECTED
    asyncio.run(run())


def test_market_max_quantity_and_min_notional_filters_with_lot_residual():
    async def run():
        filters = replace(default_constraints(), lot_step=0.1, min_qty=0.1, market_max_qty=2.0,
                          max_qty=10.0, min_notional=50.0)
        executor = MockExecutor(INSTRUMENT, filters)
        executor.advance(100.0, 1)
        for cid, qty in [("too-large", 2.1), ("too-small", 0.4)]:
            state = await executor.submit(action(cid, qty=qty))
            assert state.status is T.OrderStatus.REJECTED and state.reason_class is T.ReasonClass.TERMINAL
        quantized = await executor.submit(action("quantized", qty=1.27))
        assert quantized.requested_qty == 1.27 and quantized.submitted_qty == 1.2
        assert quantized.quantization_residual == 0.07 and quantized.filled_qty == 1.2
        limit = await executor.submit(action("limit", kind=T.OrderKind.LIMIT, qty=2.1, price=99.0))
        assert limit.status is T.OrderStatus.ACKED  # market_max_qty is distinct from max_qty
        assert await executor.position(INSTRUMENT) == 1.2
    asyncio.run(run())


@pytest.mark.parametrize("exempt,expected", [(True, T.OrderStatus.FILLED), (False, T.OrderStatus.REJECTED)])
def test_reduce_only_min_notional_uses_constraint_exemption(exempt, expected):
    async def run():
        filters = replace(default_constraints(), min_notional=50.0, reduce_only_min_notional_exempt=exempt)
        executor = MockExecutor(INSTRUMENT, filters, initial_position=0.1)
        executor.advance(100.0, 1)
        assert (await executor.submit(action(qty=0.1, side=T.Side.SELL, reduce_only=True))).status is expected
    asyncio.run(run())


@pytest.mark.parametrize("basis", [T.TriggerBasis.LAST, T.TriggerBasis.MARK])
def test_conditionals_use_independent_last_and_mark_series(basis):
    async def run():
        executor = MockExecutor(INSTRUMENT)
        executor.advance(100.0, 1, mark_price=100.0)
        state = await executor.submit(action(kind=T.OrderKind.STOP_MARKET, stop_price=110.0, trigger_basis=basis))
        assert state.status is T.OrderStatus.ACKED
        executor.advance(115.0, 2)
        expected = T.OrderStatus.FILLED if basis is T.TriggerBasis.LAST else T.OrderStatus.ACKED
        assert (await executor.lookup("client-1", None)).status is expected
        executor.advance(100.0, 3, mark_price=115.0)
        fills, _ = await executor.fills_since(INSTRUMENT, None)
        assert len(fills) == 1 and fills[0].price == 110.0
        assert fills[0].ts == (2 if basis is T.TriggerBasis.LAST else 3)
    asyncio.run(run())


def test_take_profit_reverses_trigger_direction_and_stop_limit_latches():
    async def run():
        executor = MockExecutor(INSTRUMENT, initial_position=1.0)
        executor.advance(100.0, 1)
        await executor.submit(action("tp", kind=T.OrderKind.TAKE_PROFIT_MARKET,
                                     side=T.Side.SELL, stop_price=110.0, reduce_only=True))
        await executor.submit(action("sl", kind=T.OrderKind.STOP_LIMIT, stop_price=110.0, price=105.0))
        executor.advance(115.0, 2)
        assert (await executor.lookup("tp", None)).status is T.OrderStatus.FILLED
        assert (await executor.lookup("sl", None)).status is T.OrderStatus.ACKED
        executor.advance(104.0, 3)
        assert (await executor.lookup("sl", None)).status is T.OrderStatus.FILLED
        assert (await executor.lookup("sl", None)).avg_price == 105.0
    asyncio.run(run())


def test_close_position_stop_persists_while_flat_then_uses_current_position():
    async def run():
        executor = MockExecutor(INSTRUMENT)
        executor.advance(100.0, 1)
        close = await executor.submit(action("close", kind=T.OrderKind.STOP_MARKET,
                                            side=T.Side.SELL, qty=0.0, stop_price=90.0, close_position=True))
        assert close.status is T.OrderStatus.ACKED and close.submitted_qty == 0.0
        executor.advance(85.0, 2)
        assert (await executor.lookup("close", None)).status is T.OrderStatus.ACKED
        await executor.submit(action("entry", qty=2.0))
        executor.advance(95.0, 3)  # crossing while flat did not latch the stop
        assert await executor.position(INSTRUMENT) == 2.0
        executor.advance(89.0, 4)
        closed = await executor.lookup("close", None)
        assert closed.status is T.OrderStatus.FILLED and closed.filled_qty == 2.0
        assert closed.submitted_qty == 2.0 and await executor.position(INSTRUMENT) == 0.0
    asyncio.run(run())


@pytest.mark.parametrize("reason", [T.ReasonClass.RETRYABLE, T.ReasonClass.TERMINAL])
def test_planned_rejection_is_stable_and_does_not_consume_next_fault_on_retry(reason):
    async def run():
        executor = MockExecutor(INSTRUMENT, faults=[SubmitFault(rejection=reason), SubmitFault(timeout="before_accept")])
        executor.advance(100.0, 1)
        state = await executor.submit(action())
        assert state.status is T.OrderStatus.REJECTED and state.reason_class is reason
        assert await executor.submit(action()) == state
        with pytest.raises(TimeoutError):
            await executor.submit(action("client-2"))
        assert await executor.position(INSTRUMENT) == 0.0
    asyncio.run(run())


def test_cancel_filters_account_and_timestamp_validation():
    async def run():
        filters = replace(default_constraints(), max_open_conditional_orders=1, stop_price_bands=(90.0, 110.0))
        executor = MockExecutor(INSTRUMENT, filters, wallet=1234.0, currency="USD")
        executor.advance(100.0, 10, mark_price=101.0)
        stop = action(kind=T.OrderKind.STOP_MARKET, stop_price=110.0)
        assert (await executor.submit(stop)).status is T.OrderStatus.ACKED
        assert (await executor.submit(replace(stop, client_id="cap"))).status is T.OrderStatus.REJECTED
        canceled = await executor.cancel("client-1", None)
        assert await executor.cancel("client-1", None) == canceled
        assert (await executor.submit(replace(stop, client_id="band", stop_price=120.0))).status is T.OrderStatus.REJECTED
        assert await executor.open_orders(INSTRUMENT) == []
        assert await executor.constraints(INSTRUMENT) == filters
        account = await executor.account()
        assert account.wallet == 1234.0 and account.mark_price == 101.0
        with pytest.raises(ValueError, match="rewind"):
            executor.advance(100.0, 9)
        with pytest.raises(ValueError, match="only"):
            await executor.position(replace(INSTRUMENT, symbol="OTHER"))
    asyncio.run(run())
