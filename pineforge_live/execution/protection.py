"""Bounded protection proposals and local rate accounting (§§5.1, 5.2, 5.5).

No proposal is a submitted/ACKed order. These helpers do not implement
create-before-cancel, replacement timing, arm deadlines, DISASTER recovery,
venue flag serialization or durable rate accounting. Mirror support is
limited to proven full-position exits of a single open lot. A plan with
refusals is unusable as a replacement set: retain existing protection until
the driver has handled the refusal. Nothing here authorizes cancellations.
"""
from __future__ import annotations

import enum
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from pineforge_live import types as T
from pineforge_live.core.book import Intent
from pineforge_live.core.probe import ProbeResult
from .risk import _finite, _integer


class ProtectionRole(enum.Enum):
    DEAD_MAN = "DEAD_MAN"
    STOP = "STOP"
    TARGET = "TARGET"


@dataclass(frozen=True)
class ProtectiveOrder:
    intent_key: str
    cycle_seq: int
    role: ProtectionRole
    kind: T.OrderKind
    side: T.Side
    qty: float
    stop_price: float
    trigger_basis: T.TriggerBasis
    reduce_only: bool = True
    close_position: bool = True


@dataclass(frozen=True)
class ProtectionRefusal:
    cause: str
    detail: str
    refuse_entry: bool
    flatten: bool


@dataclass(frozen=True)
class DeadmanConfig:
    max_loss_pct: float
    margin_buffer: float

    def __post_init__(self) -> None:
        if not _finite(self.max_loss_pct) or self.max_loss_pct <= 0:
            raise ValueError("max_loss_pct must be explicitly finite and positive")
        if not _finite(self.margin_buffer) or not 0 <= self.margin_buffer < 1:
            raise ValueError("margin_buffer must be in [0, 1)")


@dataclass(frozen=True)
class DeadmanPlan:
    order: ProtectiveOrder | None
    price_interval: tuple[float, float] | None
    width_pct: float | None
    refusal: ProtectionRefusal | None
    cancel_existing_when_flat: bool = False


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _valid_bands(constraints: T.VenueConstraints) -> bool:
    bands = constraints.stop_price_bands
    return (isinstance(bands, (tuple, list)) and len(bands) == 2
            and all(_finite(x) and x > 0 for x in bands) and bands[0] <= bands[1]
            and _finite(constraints.tick_size) and constraints.tick_size > 0)


def plan_deadman(config: DeadmanConfig, *, position: float, is_open: bool, cycle_seq: int,
                 mark_price: float, liquidation_price: float | None, ledger_stop_prices: Sequence[float],
                 constraints: T.VenueConstraints) -> DeadmanPlan:
    """Choose the widest legal MARK stop after inward tick quantization.

    `position` is the signed current/expected full position; `is_open`
    distinguishes pre-entry refusal from an already-open flatten demand.
    Nominal width is 3× the widest supplied ledger stop distance, or the
    explicit max_loss_pct floor when there are no ledger stops. The actual
    width cannot exceed liquidation distance × (1-buffer), the nominal
    width, or stop bands, and cannot be narrower than the ledger's widest
    stop. Missing liquidation evidence or no tick in this interval refuses.
    """
    if not isinstance(is_open, bool) or not _integer(cycle_seq):
        raise ValueError("is_open and cycle_seq must be explicit valid state")

    def refuse(cause: str, detail: str) -> DeadmanPlan:
        return DeadmanPlan(None, None, None, ProtectionRefusal(cause, detail, True, is_open))

    if not _finite(position):
        return refuse("deadman_position", "signed expected/current position is unavailable or invalid")
    if position == 0:
        if is_open:
            return refuse("deadman_position", "open-position evidence contradicts zero size")
        return DeadmanPlan(None, None, None, None, cancel_existing_when_flat=True)
    if not _finite(mark_price) or mark_price <= 0 or not _finite(liquidation_price) or liquidation_price <= 0:
        return refuse("deadman_prices", "finite positive mark and liquidation prices are required")
    if not isinstance(constraints, T.VenueConstraints) or not _valid_bands(constraints):
        return refuse("deadman_constraints", "positive finite tick and ordered absolute stop bands are required")
    if (T.TriggerBasis.MARK not in constraints.trigger_bases or not constraints.close_position_supported
            or T.OrderKind.STOP_MARKET.value not in constraints.conditional_order_types):
        return refuse("deadman_capability", "MARK close-position STOP_MARKET support is required")
    if not isinstance(ledger_stop_prices, (tuple, list)) or any(not _finite(p) or p <= 0 for p in ledger_stop_prices):
        return refuse("deadman_ledger_stops", "explicit finite positive ledger stops (or an empty sequence) are required")

    mark, liquidation = _decimal(mark_price), _decimal(liquidation_price)
    sign = Decimal(1 if position > 0 else -1)
    distances = [sign * (mark - _decimal(price)) for price in ledger_stop_prices]
    if any(distance <= 0 for distance in distances):
        return refuse("deadman_ledger_stops", "ledger stop is already through MARK or on the wrong side")
    floor = max(distances, default=Decimal(0))
    nominal = 3 * floor if distances else mark * _decimal(config.max_loss_pct) / 100
    liquidation_cap = sign * (mark - liquidation) * (1 - _decimal(config.margin_buffer))
    cap = min(nominal, liquidation_cap)
    low_band, high_band = map(_decimal, constraints.stop_price_bands)
    if position > 0:
        low, high = max(low_band, mark - cap), min(high_band, mark - floor)
    else:
        low, high = max(low_band, mark + floor), min(high_band, mark + cap)
    tick = _decimal(constraints.tick_size)
    if low > high or cap <= 0:
        return refuse("deadman_empty_interval", "liquidation/band cap cannot contain the ledger stop distance")
    price = ((low / tick).to_integral_value(rounding=ROUND_CEILING) * tick if position > 0
             else (high / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)
    width = sign * (mark - price)
    if not low <= price <= high or width <= 0 or width < floor or width > cap:
        return refuse("deadman_empty_interval", "no protective venue tick exists inside the legal interval")
    order = ProtectiveOrder("DEAD_MAN", cycle_seq, ProtectionRole.DEAD_MAN, T.OrderKind.STOP_MARKET,
                            T.Side.SELL if position > 0 else T.Side.BUY, abs(position), float(price), T.TriggerBasis.MARK)
    return DeadmanPlan(order, (float(low), float(high)), float(width / mark * 100), None)


@dataclass(frozen=True)
class MirrorPlan:
    orders: tuple[ProtectiveOrder, ...]
    refusals: tuple[ProtectionRefusal, ...]


def plan_mirrors(book: Mapping[str, Intent], probe: ProbeResult, *, position: float, cycle_seq: int,
                 open_lots: int, constraints: T.VenueConstraints, capabilities: T.Capabilities,
                 trail_refresh_policy: str, resolved: Mapping[str, bool] | None = None) -> MirrorPlan:
    """Resolve a single STOP/TARGET pair from settled EXIT intents only.

    ProbeResult does not export per-probe resolution flags. bar_open_level
    uses the settled Intent flag; intrabar_best requires an explicit mapping
    captured from the same probe run, never guessed from a numeric price.
    Missing levels or an aborted probe refuse replacement. Trigger-through
    levels also refuse: the full driver must establish ENTRY_SLIP/CONVERT
    evidence instead of this helper guessing a market execution.
    """
    failures: list[ProtectionRefusal] = []
    orders: list[ProtectiveOrder] = []

    def refuse(cause: str, detail: str) -> None:
        failures.append(ProtectionRefusal(cause, detail, True, False))

    if not _finite(position) or not _integer(cycle_seq) or not _integer(open_lots):
        return MirrorPlan((), (ProtectionRefusal("mirror_position", "valid position, cycle and lot count required", True, False),))
    if position == 0:
        if open_lots:
            refuse("mirror_position", "flat position contradicts open lots")
        return MirrorPlan((), tuple(failures))
    if open_lots != 1:
        refuse("mirror_multi_lot", "only exactly one open lot is supported")
    if trail_refresh_policy not in {"bar_open_level", "intrabar_best"}:
        refuse("mirror_policy", "unsupported trail refresh policy")
    if (not isinstance(probe, ProbeResult) or probe.aborted or not _finite(probe.forming.c) or probe.forming.c <= 0):
        refuse("mirror_probe", "completed probe with a positive LAST close is required")
    if (not isinstance(constraints, T.VenueConstraints) or not _valid_bands(constraints)
            or not isinstance(capabilities, T.Capabilities) or not capabilities.conditional_orders
            or not capabilities.close_position or not capabilities.reduce_only
            or not constraints.close_position_supported
            or T.TriggerBasis.LAST not in capabilities.trigger_bases
            or T.TriggerBasis.LAST not in constraints.trigger_bases):
        refuse("mirror_capability", "LAST reduce-only close-position conditional support and valid bands/tick required")
    if failures:
        return MirrorPlan((), tuple(failures))
    low, high = constraints.stop_price_bands
    tick = _decimal(constraints.tick_size)
    for key, intent in sorted(book.items()):
        if intent.kind != "EXIT":
            continue
        if (intent.requested_partial or (intent.qty_percent is not None and
                (not _finite(intent.qty_percent) or intent.qty_percent < 100))):
            refuse("mirror_partial", f"{key}: partial exits are unsupported")
            continue
        if intent.qty is not None and (not _finite(intent.qty) or intent.qty <= 0 or intent.qty < abs(position)):
            refuse("mirror_partial", f"{key}: fixed quantity does not cover the full position")
            continue
        if intent.qty is None and not intent.full_percent_exit_request:
            refuse("mirror_full_position_unproven", f"{key}: no proof of full-position sizing")
            continue
        if not isinstance(intent.is_long, bool) or intent.is_long != (position < 0):
            refuse("mirror_side", f"{key}: exit side does not reduce the position")
            continue
        if trail_refresh_policy == "intrabar_best":
            if resolved is None or not isinstance(resolved.get(key), bool):
                refuse("mirror_resolution", f"{key}: same-probe resolution evidence is missing")
                continue
            level_resolved = resolved[key]
        else:
            level_resolved = intent.level_resolved
        if not isinstance(level_resolved, bool):
            refuse("mirror_resolution", f"{key}: invalid resolution evidence")
            continue
        if not level_resolved:
            continue
        levels = probe.levels.get(key)
        if not isinstance(levels, (tuple, list)) or len(levels) != 3:
            refuse("mirror_levels", f"{key}: latest probe levels are missing")
            continue
        stop, target, _activation = levels
        for role, price, kind in ((ProtectionRole.STOP, stop, T.OrderKind.STOP_MARKET),
                                  (ProtectionRole.TARGET, target, T.OrderKind.TAKE_PROFIT_MARKET)):
            if price is None:
                continue
            if not _finite(price) or price <= 0 or not low <= price <= high or _decimal(price) % tick != 0:
                refuse("mirror_price", f"{key}/{role.value}: level is invalid, outside bands, or not on a venue tick")
                continue
            protective_side = price < probe.forming.c if position > 0 else price > probe.forming.c
            if price == probe.forming.c or protective_side != (role is ProtectionRole.STOP):
                refuse("mirror_trigger_through", f"{key}/{role.value}: level needs explicit ENTRY_SLIP/CONVERT handling")
                continue
            if kind.value not in constraints.conditional_order_types:
                refuse("mirror_kind", f"{key}/{role.value}: conditional kind is unsupported")
                continue
            orders.append(ProtectiveOrder(key, cycle_seq, role, kind, T.Side.SELL if position > 0 else T.Side.BUY,
                                          abs(position), price, T.TriggerBasis.LAST))
    for role in (ProtectionRole.STOP, ProtectionRole.TARGET):
        if sum(order.role is role for order in orders) > 1:
            refuse("mirror_multiple_pairs", f"more than one {role.value} cannot be mirrored for one position")
    return MirrorPlan(() if failures else tuple(orders), tuple(failures))


def check_stop_transition(*, position: float, cycle_seq: int, previous: Sequence[ProtectiveOrder],
                          proposed: Sequence[ProtectiveOrder]) -> tuple[ProtectionRefusal, ...]:
    """Require every same-cycle stop to remain present and no farther away.

    Comparison is price/direction based so moving MARK cannot disguise a
    widened stop. TARGET legs do not substitute for a protective stop.
    Callers provide the retained-plus-created venue set AFTER the proposed
    transition, not just new creates. Same-cycle side changes and dropping
    a stop refuse; stale-cycle orders are outside this check.
    """
    if not _finite(position) or not _integer(cycle_seq):
        raise ValueError("valid signed position and cycle_seq required")
    if position == 0:
        return ()
    side = T.Side.SELL if position > 0 else T.Side.BUY
    failures: list[ProtectionRefusal] = []
    old_stops = [order for order in previous if order.cycle_seq == cycle_seq and order.role is not ProtectionRole.TARGET]
    new_stops = [order for order in proposed if order.cycle_seq == cycle_seq and order.role is not ProtectionRole.TARGET]
    for old in old_stops:
        matches = [new for new in new_stops if (new.intent_key, new.role) == (old.intent_key, old.role)]
        valid = (old.side is side and _finite(old.stop_price) and old.stop_price > 0 and len(matches) == 1)
        if valid:
            new = matches[0]
            valid = (new.side is side and new.reduce_only and new.close_position and _finite(new.qty)
                     and new.qty >= abs(position) and _finite(new.stop_price) and new.stop_price > 0
                     and new.kind is T.OrderKind.STOP_MARKET and new.trigger_basis is old.trigger_basis
                     and (new.stop_price >= old.stop_price if position > 0 else new.stop_price <= old.stop_price))
        if not valid:
            failures.append(ProtectionRefusal("stop_protection_widened", f"{old.intent_key}/{old.role.value}: missing, changed, or widened stop", True, False))
    return tuple(failures)


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    cause: str | None
    retry_after_ms: int | None


class RateLimiter:
    """One process/host's shared rolling venue caps, with hard lane reserves.

    Every request (including reads/cancels and all strategies sharing the
    venue/IP allocation) must acquire its explicit order/weight costs here.
    Emergency use shares the total cap; discretionary use additionally may
    consume at most cap-minus-reserve. Acquiring both budgets is atomic.
    Admission consumes budget even if the subsequent request fails. State
    is in-memory, not restart-safe or coordinated between processes; a full
    driver must restore external usage before live admission. Serialized
    calls only. Rolling windows are conservative vs venue fixed windows.
    """
    def __init__(self, constraints: T.VenueConstraints, *, emergency_orders_reserve: int,
                 emergency_weight_reserve: int, now_ms: Callable[[], int]):
        for name, cap, reserve in (("orders", constraints.orders_per_10s, emergency_orders_reserve),
                                   ("weight", constraints.request_weight_per_min, emergency_weight_reserve)):
            if not _integer(cap, minimum=1) or not _integer(reserve, minimum=1) or reserve > cap:
                raise ValueError(f"{name} cap/reserve must be positive integers with reserve <= cap")
        if not callable(now_ms):
            raise ValueError("now_ms must be an injected clock callable")
        self._clock = now_ms
        self._last_ms: int | None = None
        self._limits = ((constraints.orders_per_10s, emergency_orders_reserve, 10_000),
                        (constraints.request_weight_per_min, emergency_weight_reserve, 60_000))
        self._events: tuple[deque, deque] = (deque(), deque())

    def acquire(self, lane: T.Lane, *, orders: int, weight: int) -> RateDecision:
        if not isinstance(lane, T.Lane) or not _integer(orders) or not _integer(weight) or orders + weight == 0:
            raise ValueError("valid lane and explicit nonnegative, nonzero request costs required")
        now = self._clock()
        if not _integer(now) or (self._last_ms is not None and now < self._last_ms):
            raise ValueError("rate clock must be valid and monotonic")
        self._last_ms = now
        discretionary = lane is T.Lane.DISCRETIONARY
        for cost, (cap, reserve, _window) in zip((orders, weight), self._limits):
            if cost > cap or (discretionary and cost > cap - reserve):
                return RateDecision(False, "request_exceeds_lane_capacity", None)

        wait_ms = 0
        blocked: list[str] = []
        for name, cost, limits, events in zip(("orders", "weight"), (orders, weight), self._limits, self._events):
            cap, reserve, window = limits
            while events and events[0][0] + window <= now:
                events.popleft()
            total = sum(event[1] for event in events)
            normal = sum(event[1] for event in events if event[2] is T.Lane.DISCRETIONARY)
            if total + cost <= cap and (not discretionary or normal + cost <= cap - reserve):
                continue
            blocked.append(name)
            for timestamp, amount, prior_lane in events:
                total -= amount
                if prior_lane is T.Lane.DISCRETIONARY:
                    normal -= amount
                if total + cost <= cap and (not discretionary or normal + cost <= cap - reserve):
                    wait_ms = max(wait_ms, timestamp + window - now)
                    break
        if blocked:
            return RateDecision(False, "rate_limit:" + ",".join(blocked), wait_ms)
        for cost, events in zip((orders, weight), self._events):
            if cost:
                events.append((now, cost, lane))
        return RateDecision(True, None, None)
