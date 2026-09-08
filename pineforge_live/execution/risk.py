"""Pure B3 account/venue checks; no I/O, clocks, STOP writes or order submission.

Percentages use percentage points (2 means 2%). Maxima allow equality,
minimum liquidation distance allows equality, and a mismatch streak trips
at its configured count. Callers supply both the observations and the
response for limits whose STOP disposition is not specified by §5.5.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pineforge_live import types as T
from pineforge_live.core.riskguard import RiskLimits

UTC_DAY_MS = 86_400_000


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _integer(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _require_number(name: str, value: object, *, positive: bool = False) -> None:
    if not _finite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")


@dataclass(frozen=True)
class RiskResponse:
    level: T.StopLevel
    disposition: T.StopDisposition

    def __post_init__(self) -> None:
        if self.level not in (T.StopLevel.FLAT_ONLY, T.StopLevel.HARD):
            raise ValueError("risk response must stop exposure increases")
        if not isinstance(self.disposition, T.StopDisposition):
            raise ValueError("risk response disposition must be a StopDisposition")
        if self.level is T.StopLevel.HARD and self.disposition is T.StopDisposition.NONE:
            raise ValueError("HARD risk response requires HOLD or FLATTEN")


@dataclass(frozen=True)
class RiskBreach:
    cause: str
    level: T.StopLevel
    disposition: T.StopDisposition
    detail: str


@dataclass(frozen=True)
class RuntimeRiskConfig:
    clock_skew_ms_max: int
    margin_ratio_spike_delta_min: float
    margin_quiet_ms: int
    allowed_venue_states: tuple[str, ...]
    limit_response: RiskResponse
    missing_input_response: RiskResponse
    venue_state_response: RiskResponse

    def __post_init__(self) -> None:
        if not _integer(self.clock_skew_ms_max):
            raise ValueError("clock_skew_ms_max must be a nonnegative integer")
        if not _integer(self.margin_quiet_ms, minimum=1):
            raise ValueError("margin_quiet_ms must be a positive integer")
        _require_number("margin_ratio_spike_delta_min", self.margin_ratio_spike_delta_min, positive=True)
        if not isinstance(self.allowed_venue_states, (tuple, list)):
            raise ValueError("allowed_venue_states must contain explicit nonempty state names")
        states = tuple(self.allowed_venue_states)
        if not states or any(not isinstance(s, str) or not s for s in states):
            raise ValueError("allowed_venue_states must contain explicit nonempty state names")
        object.__setattr__(self, "allowed_venue_states", states)
        for name in ("limit_response", "missing_input_response", "venue_state_response"):
            if not isinstance(getattr(self, name), RiskResponse):
                raise ValueError(f"{name} must be an explicit RiskResponse")


@dataclass(frozen=True)
class RiskInputs:
    """One coherent observation; None means unavailable, never a healthy zero.

    `daily_realized_pnl` is signed, fee-adjusted PnL for the UTC day index
    `realized_pnl_utc_day`, not a lifetime total or a wallet delta. The
    caller must supply a fresh daily aggregate after midnight; the evaluator
    cannot safely reset a prior day's loss without the new day's fills.

    Both marked equities must be re-marked at the same bar close. Shadow
    equity already includes funding. `divergence_reference_equity` is an
    explicitly supplied positive equity denominator for both comparisons;
    realized PnL comparison is additionally required only when BOTH sides
    are flat. Positions are signed and zero means exactly flat.
    """
    now_ms: int
    account: T.AccountState | None = None
    venue_position: float | None = None
    ledger_position: float | None = None
    feed_updated_ms: int | None = None
    eval_updated_ms: int | None = None
    daily_realized_pnl: float | None = None
    realized_pnl_utc_day: int | None = None
    bar_mismatch_streak: int | None = None
    shadow_marked_equity: float | None = None
    ledger_marked_equity: float | None = None
    shadow_realized_pnl: float | None = None
    ledger_realized_pnl: float | None = None
    divergence_reference_equity: float | None = None
    recompute_ms_p99: float | None = None
    clock_skew_ms: float | None = None
    venue_state: str | None = None


def validate_risk_limits(limits: RiskLimits) -> None:
    """Validate this module's limits; never interpret invalid limits as off."""
    for name in ("max_daily_realized_loss", "unexplained_divergence_pct", "liquidation_distance_pct_min"):
        _require_number(name, getattr(limits, name))
    for name in ("stale_feed_ms", "stale_eval_ms", "bar_mismatch_streak", "recompute_ms_p99_max"):
        if not _integer(getattr(limits, name), minimum=1):
            raise ValueError(f"{name} must be a positive integer")


def evaluate_risk(limits: RiskLimits, config: RuntimeRiskConfig, inputs: RiskInputs) -> tuple[RiskBreach, ...]:
    """Return all independent breaches, including explicit input refusals.

    This function does not clear prior STOPs. The runtime journals/raises
    these responses and separately gates reconcile using MarginQuietState.
    Invalid configuration raises ValueError; missing/invalid observations
    return `risk_input:<field>` with the configured refusal response.
    """
    validate_risk_limits(limits)
    breaches: list[RiskBreach] = []

    def emit(cause: str, detail: str, response: RiskResponse = config.limit_response) -> None:
        breaches.append(RiskBreach(cause, response.level, response.disposition, detail))

    def refuse(name: str, detail: str = "missing or invalid observation") -> None:
        emit(f"risk_input:{name}", detail, config.missing_input_response)

    def number(name: str, value: object, *, nonnegative: bool = False, positive: bool = False) -> bool:
        ok = _finite(value) and (not nonnegative or value >= 0) and (not positive or value > 0)
        if not ok:
            refuse(name)
        return ok

    if not _integer(inputs.now_ms):
        refuse("now_ms")
        return tuple(breaches)

    for field, limit_name in (("feed_updated_ms", "stale_feed_ms"), ("eval_updated_ms", "stale_eval_ms")):
        stamp = getattr(inputs, field)
        if not _integer(stamp) or stamp > inputs.now_ms:
            refuse(field, "timestamp unavailable, invalid, or in the future")
        elif inputs.now_ms - stamp > getattr(limits, limit_name):
            emit(limit_name, f"age_ms={inputs.now_ms - stamp} exceeds {getattr(limits, limit_name)}")

    day_valid = _integer(inputs.realized_pnl_utc_day) and inputs.realized_pnl_utc_day == inputs.now_ms // UTC_DAY_MS
    if not day_valid:
        refuse("realized_pnl_utc_day", "daily realized PnL must cover the current UTC day")
    if number("daily_realized_pnl", inputs.daily_realized_pnl) and day_valid:
        if -inputs.daily_realized_pnl > limits.max_daily_realized_loss:
            emit("max_daily_realized_loss", f"realized_pnl={inputs.daily_realized_pnl}")

    if not _integer(inputs.bar_mismatch_streak):
        refuse("bar_mismatch_streak")
    elif inputs.bar_mismatch_streak >= limits.bar_mismatch_streak:
        emit("bar_mismatch_streak", f"streak={inputs.bar_mismatch_streak}")

    if number("recompute_ms_p99", inputs.recompute_ms_p99, nonnegative=True):
        if inputs.recompute_ms_p99 > limits.recompute_ms_p99_max:
            emit("recompute_ms_p99_max", f"p99_ms={inputs.recompute_ms_p99}")
    if number("clock_skew_ms", inputs.clock_skew_ms):
        if abs(inputs.clock_skew_ms) > config.clock_skew_ms_max:
            emit("clock_skew", f"skew_ms={inputs.clock_skew_ms}")
    if not isinstance(inputs.venue_state, str) or not inputs.venue_state:
        refuse("venue_state")
    elif inputs.venue_state not in config.allowed_venue_states:
        emit("venue_state", f"state={inputs.venue_state}", config.venue_state_response)

    venue_valid = number("venue_position", inputs.venue_position)
    ledger_valid = number("ledger_position", inputs.ledger_position)
    denominator_valid = number("divergence_reference_equity", inputs.divergence_reference_equity, positive=True)

    def divergence(kind: str, shadow: object, ledger: object) -> None:
        shadow_ok = number(f"shadow_{kind}", shadow)
        ledger_ok = number(f"ledger_{kind}", ledger)
        if shadow_ok and ledger_ok and denominator_valid:
            # If opposite-signed finite equities overflow on subtraction,
            # scale each term first. Equal large equities still subtract
            # before division, avoiding inf - inf for small denominators.
            difference = abs(shadow - ledger)
            fraction = (difference / inputs.divergence_reference_equity if math.isfinite(difference)
                        else abs(shadow / inputs.divergence_reference_equity - ledger / inputs.divergence_reference_equity))
            pct = fraction * 100
            if pct > limits.unexplained_divergence_pct:
                emit("unexplained_divergence_pct", f"basis={kind}; divergence_pct={pct}")

    divergence("marked_equity", inputs.shadow_marked_equity, inputs.ledger_marked_equity)
    if venue_valid and ledger_valid and inputs.venue_position == 0 and inputs.ledger_position == 0:
        divergence("realized_pnl", inputs.shadow_realized_pnl, inputs.ledger_realized_pnl)

    if not isinstance(inputs.account, T.AccountState):
        refuse("account")
    else:
        number("account.margin_ratio", inputs.account.margin_ratio, nonnegative=True)
        if venue_valid and inputs.venue_position != 0:
            mark_ok = number("account.mark_price", inputs.account.mark_price, positive=True)
            liq_ok = number("account.liquidation_price", inputs.account.liquidation_price, positive=True)
            if mark_ok and liq_ok:
                # Signed direction matters: already past liquidation is a
                # negative distance, not an apparently safe absolute gap.
                sign = 1 if inputs.venue_position > 0 else -1
                distance = sign * (inputs.account.mark_price - inputs.account.liquidation_price) / inputs.account.mark_price * 100
                if distance < limits.liquidation_distance_pct_min:
                    emit("liquidation_distance_pct_min", f"distance_pct={distance}",
                         RiskResponse(T.StopLevel.FLAT_ONLY, T.StopDisposition.FLATTEN))
    return tuple(breaches)


@dataclass(frozen=True)
class MarginQuietState:
    """Transient reconciliation gate; persist/restore with runtime state.

    `previous_ratio=None` means no baseline and refuses to permit increases
    until advance_margin_quiet has received the first valid observation.
    """
    previous_ratio: float | None = None
    quiet_until_ms: int = 0
    observed_ms: int | None = None

    def __post_init__(self) -> None:
        if self.previous_ratio is not None:
            _require_number("previous_ratio", self.previous_ratio)
        if not _integer(self.quiet_until_ms):
            raise ValueError("quiet_until_ms must be a nonnegative integer")
        if self.observed_ms is not None and not _integer(self.observed_ms):
            raise ValueError("observed_ms must be a nonnegative integer")

    def blocks_increase(self, now_ms: int) -> bool:
        if not _integer(now_ms):
            raise ValueError("now_ms must be a nonnegative integer")
        return self.previous_ratio is None or self.observed_ms is None or now_ms < self.observed_ms or now_ms < self.quiet_until_ms


def advance_margin_quiet(config: RuntimeRiskConfig, state: MarginQuietState, *, now_ms: int,
                         margin_ratio: float | None, margin_call_ms: int | None = None) -> MarginQuietState:
    """Observe an absolute ratio increase or MarginCall; return a new gate.

    Spike threshold is a delta in the venue-neutral ratio units. Equality
    triggers it. Repeated/older MarginCalls cannot shorten the gate. Invalid
    input raises ValueError so a caller cannot accidentally replace a gate
    with a healthy state; evaluate_risk also returns missing ratio refusals.
    """
    if not _integer(now_ms) or (state.observed_ms is not None and now_ms < state.observed_ms):
        raise ValueError("margin observation time must be valid and monotonic")
    _require_number("margin_ratio", margin_ratio)
    until = state.quiet_until_ms
    if state.previous_ratio is not None and margin_ratio - state.previous_ratio >= config.margin_ratio_spike_delta_min:
        until = max(until, now_ms + config.margin_quiet_ms)
    if margin_call_ms is not None:
        if not _integer(margin_call_ms) or margin_call_ms > now_ms:
            raise ValueError("margin_call_ms must be valid and not in the future")
        until = max(until, margin_call_ms + config.margin_quiet_ms)
    return MarginQuietState(margin_ratio, until, now_ms)


@dataclass(frozen=True)
class StartupRequirements:
    instrument: T.InstrumentId
    syminfo: T.EngineSyminfo
    leverage: int
    margin_mode: str
    compiled_margin_long_pct: float
    compiled_margin_short_pct: float
    contract_multiplier: float
    max_entry_slip_bps: float

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, T.InstrumentId) or not isinstance(self.syminfo, T.EngineSyminfo):
            raise ValueError("startup requires InstrumentId and EngineSyminfo")
        if not _integer(self.leverage, minimum=1):
            raise ValueError("leverage must be a positive integer")
        if not isinstance(self.margin_mode, str) or not self.margin_mode:
            raise ValueError("margin_mode must be explicitly supplied")
        for name in ("compiled_margin_long_pct", "compiled_margin_short_pct", "contract_multiplier", "max_entry_slip_bps"):
            _require_number(name, getattr(self, name), positive=True)


def validate_startup(requirements: StartupRequirements, *, resolved_instrument: T.InstrumentId | None,
                     account: T.AccountState | None, constraints: T.VenueConstraints | None) -> tuple[RiskBreach, ...]:
    """Check supplied identity and account/filter evidence before admission.

    This bounded helper is not a complete live-readiness verdict: ABI/key
    permissions/history/tape/marker checks belong to their owning components.
    Refusals use HARD/HOLD since no execution may start with a mismatch.
    """
    breaches: list[RiskBreach] = []

    def require(condition: bool, cause: str, detail: str) -> None:
        if not condition:
            breaches.append(RiskBreach(f"startup:{cause}", T.StopLevel.HARD, T.StopDisposition.HOLD, detail))

    require(resolved_instrument == requirements.instrument, "instrument", "resolved instrument differs or is unavailable")
    syminfo = requirements.syminfo
    require(_finite(syminfo.pointvalue) and syminfo.pointvalue == requirements.contract_multiplier,
            "pointvalue", "pointvalue must equal the supplied contract multiplier")
    for name in ("compiled_margin_long_pct", "compiled_margin_short_pct"):
        require(math.isclose(getattr(requirements, name), 100 / requirements.leverage, rel_tol=1e-12, abs_tol=0),
                name, "compiled margin percentage must match 100 / pinned leverage")
    if not isinstance(account, T.AccountState):
        require(False, "account", "account snapshot is unavailable")
    else:
        require(bool(syminfo.currency) and account.currency == syminfo.currency, "currency", "account currency must equal engine currency")
        require(account.position_mode == "ONE_WAY", "position_mode", "account must use normalized ONE_WAY position mode")
        require(_integer(account.leverage, minimum=1) and account.leverage == requirements.leverage,
                "leverage", "account leverage differs from pinned leverage")
        require(account.margin_mode == requirements.margin_mode, "margin_mode", "account margin mode differs from configuration")
    if not isinstance(constraints, T.VenueConstraints):
        require(False, "constraints", "venue constraints are unavailable")
    else:
        require(_finite(constraints.tick_size) and constraints.tick_size > 0 and _finite(syminfo.mintick)
                and syminfo.mintick == constraints.tick_size, "mintick", "mintick must exactly equal positive venue tick_size")
        require("ONE_WAY" in constraints.position_modes, "supported_position_mode", "venue must support ONE_WAY position mode")
        require(_integer(constraints.leverage, minimum=1) and constraints.leverage == requirements.leverage,
                "constraint_leverage", "venue constraints differ from pinned leverage")
        require(requirements.margin_mode in constraints.margin_modes, "supported_margin_mode", "venue must support configured margin mode")
    return tuple(breaches)
