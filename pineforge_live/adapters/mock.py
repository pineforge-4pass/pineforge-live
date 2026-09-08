"""Deterministic, single-instrument Executor for B3 tests and tape replay.

Construct ``MockExecutor(instrument, constraints=None, initial_position=0,
wallet=100_000, currency="USD", immediate_market=True, faults=())``. Call
``advance(price, ts, mark_price=None)`` to supply prints; timestamps never
rewind. MARKET fills at the latest last price, immediately on submit when
configured, otherwise on the next advance. MARK conditionals use only the
last explicitly supplied mark; an omitted mark does not follow LAST.
Submitting before the first print is allowed when no notional minimum
requires a reference price; the market order then waits for that print.
Conditionals fill at their level; STOP_LIMIT latches its trigger then waits
for the last price to satisfy its limit. Matching follows submission order.

``plan(SubmitFault(...))`` queues a fault for the next distinct submit. A
partial ratio applies once, to that order's first fill; subsequent advances
can fill its remainder. Timeout-before creates nothing, timeout-after leaves
a lookup-adoptable order (possibly filled). Reusing a known client id returns
its current state without creating an order or consuming another fault.
Rejections are recorded, so retrying a recorded rejection also returns it.

``orders_since``/``fills_since`` return immutable history after an opaque,
stream-specific offset; None starts at zero. ``events`` drains a finite
snapshot of pending events. Duplicate-event faults replay identical event
objects but never duplicate history fills or position effects.

Fidelity limits: no network, clock sleeps, queue depth, latency distribution,
fees, funding, liquidation, margin admission, rate limiting, retention expiry,
or hedge mode. Account money is a fixed test fixture, not a P&L model. This
is not venue conformance evidence or the full spec §8 fault simulator.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN
from typing import AsyncIterator, Iterable

from pineforge_live import types as T


_CONDITIONAL = frozenset({T.OrderKind.STOP_MARKET, T.OrderKind.STOP_LIMIT, T.OrderKind.TAKE_PROFIT_MARKET})
_MARKET = frozenset({T.OrderKind.MARKET, T.OrderKind.STOP_MARKET, T.OrderKind.TAKE_PROFIT_MARKET})


def default_constraints() -> T.VenueConstraints:
    """Permissive neutral fixture; callers should supply their tape's filters."""
    return T.VenueConstraints(
        tick_size=0.01, lot_step=0.001, min_qty=0.001, max_qty=1e6,
        market_max_qty=1e6, min_notional=0.0, price_bands=(0.01, 1e9),
        stop_price_bands=(0.01, 1e9), max_open_orders=1000,
        max_open_conditional_orders=1000, position_modes=("ONE_WAY",), leverage=1,
        margin_modes=("cross",), conditional_order_types=tuple(sorted(k.value for k in _CONDITIONAL)),
        close_position_supported=True, reduce_only_min_notional_exempt=True,
        trigger_bases=(T.TriggerBasis.LAST, T.TriggerBasis.MARK),
        order_lookup_retention_ms=86_400_000, client_id_on_fills=True,
        orders_per_10s=1000, request_weight_per_min=10000,
    )


@dataclass(frozen=True)
class SubmitFault:
    """One queued submit fault; optional fill modifiers persist with its order."""
    timeout: str | None = None  # before_accept / after_accept
    rejection: T.ReasonClass | None = None
    partial_fill_ratio: float = 1.0
    omit_client_id: bool = False
    duplicate_events: int = 0  # extra copies, bounded to keep each drain finite

    def __post_init__(self):
        if self.timeout not in (None, "before_accept", "after_accept"):
            raise ValueError("timeout must be before_accept or after_accept")
        if self.rejection not in (None, T.ReasonClass.RETRYABLE, T.ReasonClass.TERMINAL):
            raise ValueError("planned rejection must be RETRYABLE or TERMINAL")
        if self.timeout is not None and self.rejection is not None:
            raise ValueError("a fault cannot both time out and reject")
        if not math.isfinite(self.partial_fill_ratio) or not 0 < self.partial_fill_ratio <= 1:
            raise ValueError("partial_fill_ratio must be in (0, 1]")
        if type(self.duplicate_events) is not int or not 0 <= self.duplicate_events <= 3:
            raise ValueError("duplicate_events must be an integer from 0 to 3")


@dataclass
class _Order:
    action: T.OrderAction
    state: T.OrderState
    fault: SubmitFault
    triggered: bool = False
    first_fill: bool = True


class MockExecutor:
    def __init__(self, instrument: T.InstrumentId, constraints: T.VenueConstraints | None = None,
                 *, initial_position: float = 0.0, wallet: float = 100_000.0,
                 currency: str = "USD", immediate_market: bool = True,
                 faults: Iterable[SubmitFault] = ()):
        if not math.isfinite(initial_position) or not math.isfinite(wallet) or wallet < 0:
            raise ValueError("position must be finite and wallet finite/nonnegative")
        self.instrument = instrument
        self._constraints = constraints or default_constraints()
        if self._constraints.lot_step <= 0 or self._constraints.tick_size <= 0:
            raise ValueError("lot_step and tick_size must be positive")
        self._position, self._wallet, self._currency = initial_position, wallet, currency
        self.immediate_market = immediate_market
        self._ts = 0
        self._last: float | None = None
        self._mark: float | None = None
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._order_history: list[T.OrderState] = []
        self._fills: list[T.Fill] = []
        self._events: deque[T.AccountEvent] = deque()
        self._faults: deque[SubmitFault] = deque()
        for fault in faults:
            self.plan(fault)

    def plan(self, fault: SubmitFault) -> None:
        if not isinstance(fault, SubmitFault):
            raise TypeError("fault must be a SubmitFault")
        self._faults.append(fault)

    def _instrument(self, instrument: T.InstrumentId) -> None:
        if instrument != self.instrument:
            raise ValueError(f"mock serves only {self.instrument.key()}")

    def _emit(self, event: T.AccountEvent, fault: SubmitFault) -> None:
        self._events.extend([event] * (1 + fault.duplicate_events))

    def _state(self, order: _Order, **changes) -> None:
        order.state = replace(order.state, **changes)
        self._order_history.append(order.state)
        self._emit(T.OrderStateChanged(order.state.client_id, order.state.venue_order_id,
                                       order.state, self._ts), order.fault)

    def _floor(self, qty: float) -> float:
        step = Decimal(str(self._constraints.lot_step))
        return float((Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step)

    def _available(self, action: T.OrderAction) -> float:
        return max(0.0, -self._position if action.side is T.Side.BUY else self._position)

    def _valid_price(self, value: float | None, bands: tuple[float, float]) -> bool:
        if value is None or not math.isfinite(value) or value <= 0 or not bands[0] <= value <= bands[1]:
            return False
        return Decimal(str(value)) % Decimal(str(self._constraints.tick_size)) == 0

    def _validate(self, action: T.OrderAction) -> tuple[float, float, bool]:
        c = self._constraints
        if (not action.client_id or not math.isfinite(action.qty) or action.qty < 0
                or action.position_side != "BOTH" or action.tif != "GTC"):
            return 0.0, 0.0, False
        if action.close_position and (not c.close_position_supported or action.kind not in _CONDITIONAL):
            return 0.0, action.qty, False
        if action.kind in _CONDITIONAL:
            if (action.kind.value not in c.conditional_order_types or action.trigger_basis not in c.trigger_bases
                    or not self._valid_price(action.stop_price, c.stop_price_bands)):
                return 0.0, action.qty, False
        if action.kind in (T.OrderKind.LIMIT, T.OrderKind.STOP_LIMIT) and not self._valid_price(action.price, c.price_bands):
            return 0.0, action.qty, False
        active = [o for o in self._orders.values() if o.state.status not in T.TERMINAL_STATUSES]
        if len(active) >= c.max_open_orders or (action.kind in _CONDITIONAL and
                sum(o.action.kind in _CONDITIONAL for o in active) >= c.max_open_conditional_orders):
            return 0.0, action.qty, False
        if action.close_position:
            return 0.0, 0.0, True  # quantity comes from venue position when the stop triggers
        qty = self._floor(action.qty)
        residual = float(Decimal(str(action.qty)) - Decimal(str(qty)))
        maximum = min(c.max_qty, c.market_max_qty) if action.kind in _MARKET else c.max_qty
        if qty < c.min_qty or qty <= 0 or qty > maximum:
            return qty, residual, False
        price = action.price if action.kind in (T.OrderKind.LIMIT, T.OrderKind.STOP_LIMIT) else (
            action.stop_price if action.kind in _CONDITIONAL else self._last)
        if not (action.reduce_only and c.reduce_only_min_notional_exempt):
            if (price is None and c.min_notional > 0) or (price is not None and qty * price < c.min_notional):
                return qty, residual, False
        if action.reduce_only and self._available(action) <= 0:
            return qty, residual, False
        return qty, residual, True

    async def submit(self, action: T.OrderAction) -> T.OrderState:
        if action.client_id in self._by_client:
            return self._orders[self._by_client[action.client_id]].state
        fault = self._faults.popleft() if self._faults else SubmitFault()
        if fault.timeout == "before_accept":
            raise TimeoutError("mock submit timed out before acceptance")
        qty, residual, valid = self._validate(action)
        rejection = fault.rejection or (None if valid else T.ReasonClass.TERMINAL)
        venue_id = f"mock-order-{len(self._orders) + 1}"
        state = T.OrderState(T.OrderStatus.REJECTED if rejection else T.OrderStatus.ACKED,
                             action.qty, qty if not rejection else 0.0, 0.0, 0.0,
                             0.0, self._currency, venue_id, action.client_id, rejection, residual)
        order = _Order(action, state, fault)
        self._orders[venue_id] = order
        self._by_client[action.client_id] = venue_id
        self._state(order)
        if not rejection and self.immediate_market and action.kind is T.OrderKind.MARKET:
            self._match(order)
        if fault.timeout == "after_accept" and not rejection:
            raise TimeoutError("mock submit timed out after acceptance")
        return order.state

    def advance(self, price: float, ts: int, mark_price: float | None = None) -> None:
        """Match all working orders against a new print, once each, in ID order."""
        if type(ts) is not int or ts < self._ts:
            raise ValueError("timestamp must be an integer and cannot rewind")
        if not math.isfinite(price) or price <= 0 or (mark_price is not None and
                (not math.isfinite(mark_price) or mark_price <= 0)):
            raise ValueError("prices must be finite and positive")
        self._last, self._ts = price, ts
        if mark_price is not None:
            self._mark = mark_price
        for order in self._orders.values():
            self._match(order)

    def _match(self, order: _Order) -> None:
        a, s = order.action, order.state
        if s.status in T.TERMINAL_STATUSES or self._last is None:
            return
        if a.close_position and self._available(a) <= 0:
            return  # closePosition persists while flat, without latching a trigger
        if a.kind in _CONDITIONAL and not order.triggered:
            basis = self._last if a.trigger_basis is T.TriggerBasis.LAST else self._mark
            rising = (a.side is T.Side.BUY) != (a.kind is T.OrderKind.TAKE_PROFIT_MARKET)
            if basis is None or (basis < a.stop_price if rising else basis > a.stop_price):
                return
            order.triggered = True
        if a.kind in (T.OrderKind.LIMIT, T.OrderKind.STOP_LIMIT):
            if (self._last > a.price if a.side is T.Side.BUY else self._last < a.price):
                return
            price = a.price
        elif a.kind in _CONDITIONAL:
            price = a.stop_price
        else:
            price = self._last
        if a.close_position and s.submitted_qty == 0:
            self._state(order, submitted_qty=self._available(a))
            s = order.state
        remaining = max(0.0, s.submitted_qty - s.filled_qty)
        qty = min(remaining, self._available(a)) if a.reduce_only or a.close_position else remaining
        if qty <= 0:
            self._state(order, status=T.OrderStatus.CANCELED)
            return
        if order.first_fill and order.fault.partial_fill_ratio < 1:
            qty = self._floor(qty * order.fault.partial_fill_ratio)
        order.first_fill = False
        if qty <= 0:
            return
        sign = 1 if a.side is T.Side.BUY else -1
        self._position += sign * qty
        filled = s.filled_qty + qty
        complete = math.isclose(filled, s.submitted_qty, rel_tol=1e-12, abs_tol=1e-12)
        status = T.OrderStatus.FILLED if complete else T.OrderStatus.PARTIAL
        if not complete and (a.reduce_only or a.close_position) and self._available(a) <= 1e-12:
            status = T.OrderStatus.CANCELED  # any excess remainder must never flip the position
        fill = T.Fill(None if order.fault.omit_client_id or not self._constraints.client_id_on_fills else a.client_id,
                      s.venue_order_id, f"mock-trade-{len(self._fills) + 1}", self._ts,
                      a.side, qty, price, 0.0, T.FillCause.OURS)
        self._fills.append(fill)
        self._emit(fill, order.fault)
        self._state(order, status=status, filled_qty=filled,
                    avg_price=(s.avg_price * s.filled_qty + price * qty) / filled)

    def _find(self, client_id: str | None, venue_order_id: str | None) -> _Order | None:
        if client_id is None and venue_order_id is None:
            raise ValueError("client_id or venue_order_id required")
        key = venue_order_id if venue_order_id is not None else self._by_client.get(client_id)
        order = self._orders.get(key)
        if order is not None and client_id is not None and order.state.client_id != client_id:
            return None
        return order

    async def lookup(self, client_id: str | None, venue_order_id: str | None) -> T.OrderState | None:
        order = self._find(client_id, venue_order_id)
        return order.state if order is not None else None

    async def cancel(self, client_id: str | None, venue_order_id: str | None) -> T.OrderState:
        order = self._find(client_id, venue_order_id)
        if order is None:
            raise T.AdapterError(False, 0, T.ReasonClass.TERMINAL, "order not found")
        if order.state.status not in T.TERMINAL_STATUSES:
            self._state(order, status=T.OrderStatus.CANCELED)
        return order.state

    async def open_orders(self, instrument: T.InstrumentId) -> list[T.OrderState]:
        self._instrument(instrument)
        return [o.state for o in self._orders.values() if o.state.status not in T.TERMINAL_STATUSES]

    @staticmethod
    def _page(history: list, stream: str, cursor: str | None) -> tuple[list, str]:
        if cursor is None:
            offset = 0
        else:
            prefix, sep, value = cursor.partition(":")
            if prefix != stream or not sep or not value.isdigit():
                raise ValueError("invalid mock cursor")
            offset = int(value)
            if offset > len(history):
                raise ValueError("cursor is ahead of mock history")
        return history[offset:].copy(), f"{stream}:{len(history)}"

    async def orders_since(self, instrument: T.InstrumentId, cursor: str | None) -> tuple[list[T.OrderState], str]:
        self._instrument(instrument)
        return self._page(self._order_history, "orders", cursor)

    async def fills_since(self, instrument: T.InstrumentId, cursor: str | None) -> tuple[list[T.Fill], str]:
        self._instrument(instrument)
        return self._page(self._fills, "fills", cursor)

    async def events(self) -> AsyncIterator[T.AccountEvent]:
        pending = list(self._events)
        self._events.clear()
        for event in pending:
            yield event

    async def position(self, instrument: T.InstrumentId) -> float:
        self._instrument(instrument)
        return self._position

    async def account(self) -> T.AccountState:
        return T.AccountState(self._wallet, self._wallet, 0.0, 0.0, None,
                              self._mark or 0.0, 0.0, self._currency,
                              self._constraints.leverage, "cross", "ONE_WAY")

    async def constraints(self, instrument: T.InstrumentId) -> T.VenueConstraints:
        self._instrument(instrument)
        return self._constraints
