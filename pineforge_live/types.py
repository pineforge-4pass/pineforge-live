"""Venue-neutral domain types (spec §2a). Frozen dataclasses; no venue names here."""
from __future__ import annotations
import enum, hashlib, json
from dataclasses import dataclass, field, asdict
from typing import Literal, Union

class MarketType(enum.Enum):
    SPOT = "spot"; PERP = "perp"; FUTURE = "future"
class OrderKind(enum.Enum):
    MARKET = "MARKET"; LIMIT = "LIMIT"; STOP_MARKET = "STOP_MARKET"; STOP_LIMIT = "STOP_LIMIT"; TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
class Side(enum.Enum):
    BUY = "BUY"; SELL = "SELL"
class OrderStatus(enum.Enum):
    PENDING = "PENDING"; ACKED = "ACKED"; PARTIAL = "PARTIAL"; FILLED = "FILLED"; CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"; REJECTED = "REJECTED"; UNKNOWN = "UNKNOWN"
class ReasonClass(enum.Enum):
    RETRYABLE = "RETRYABLE"; TERMINAL = "TERMINAL"; CONVERT = "CONVERT"
class FillCause(enum.Enum):
    OURS = "OURS"; LIQUIDATION = "LIQUIDATION"; ADL = "ADL"; MANUAL = "MANUAL"; UNATTRIBUTED = "UNATTRIBUTED"
class TriggerBasis(enum.Enum):
    LAST = "LAST"; MARK = "MARK"
class Lane(enum.Enum):
    EMERGENCY = "EMERGENCY"; DISCRETIONARY = "DISCRETIONARY"
class StopLevel(enum.Enum):
    NONE = "NONE"; FLAT_ONLY = "FLAT_ONLY"; HARD = "HARD"
class StopDisposition(enum.Enum):
    NONE = "NONE"; FLATTEN = "FLATTEN"; HOLD = "HOLD"

TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED})

def canonical_sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

@dataclass(frozen=True)
class InstrumentId:
    venue: str; market_type: MarketType; symbol: str
    def key(self) -> str: return f"{self.venue}:{self.market_type.value}:{self.symbol}"

@dataclass(frozen=True)
class EngineSyminfo:
    ticker: str; tickerid: str; prefix: str; root: str; type: str; currency: str; basecurrency: str
    mintick: float; pricescale: int; pointvalue: float; minmove: int; session: str; timezone: str
    volumetype: str; description: str
    numeric_metadata: dict[str, float] = field(default_factory=dict)
    string_metadata: dict[str, str] = field(default_factory=dict)
    def hash(self) -> str: return canonical_sha256(asdict(self))

@dataclass(frozen=True)
class VenueConstraints:
    tick_size: float; lot_step: float; min_qty: float; max_qty: float; market_max_qty: float; min_notional: float
    price_bands: tuple[float, float]; stop_price_bands: tuple[float, float]; max_open_orders: int
    max_open_conditional_orders: int; position_modes: tuple[str, ...]; leverage: int; margin_modes: tuple[str, ...]
    conditional_order_types: tuple[str, ...]; close_position_supported: bool; reduce_only_min_notional_exempt: bool
    trigger_bases: tuple[str, ...]; order_lookup_retention_ms: int; client_id_on_fills: bool
    orders_per_10s: int; request_weight_per_min: int
    maintenance_margin_tiers: tuple[tuple[float, float, float], ...] = ()   # (notional_cap, mmr, maintenance_amount)

@dataclass(frozen=True)
class NormalizedBar:
    ts_open: int; o: float; h: float; l: float; c: float; v: float; trade_count: int
    is_forming: bool = False; synthesized: bool = False
    def ohlcv(self) -> tuple[int, float, float, float, float, float]:
        return (self.ts_open, self.o, self.h, self.l, self.c, self.v)

@dataclass(frozen=True)
class NormalizedTick:
    ts: int; seq: int; price: float; qty: float; side: Side | None = None

@dataclass(frozen=True)
class OrderAction:
    client_id: str; intent_key: str; level_version: int; action_seq: int; kind: OrderKind; side: Side; qty: float
    price: float | None; stop_price: float | None; reduce_only: bool; close_position: bool; position_side: str
    tif: str; trigger_basis: TriggerBasis; lane: Lane; cls: str; reason: str

@dataclass(frozen=True)
class OrderState:
    status: OrderStatus; requested_qty: float; submitted_qty: float; filled_qty: float; avg_price: float
    fees: float; fee_currency: str; venue_order_id: str; client_id: str | None
    reason_class: ReasonClass | None = None; quantization_residual: float = 0.0

@dataclass(frozen=True)
class Fill:
    client_id: str | None; venue_order_id: str; venue_trade_id: str; ts: int; side: Side; qty: float; price: float
    fee: float; cause: FillCause

@dataclass(frozen=True)
class AccountState:
    wallet: float; available_margin: float; unrealized_pnl: float; margin_ratio: float; liquidation_price: float | None
    mark_price: float; funding_accrued: float; currency: str; leverage: int; margin_mode: str; position_mode: str

# --- events -----------------------------------------------------------------
@dataclass(frozen=True)
class OrderStateChanged: client_id: str | None; venue_order_id: str; state: OrderState; ts: int
@dataclass(frozen=True)
class PositionChangedExternally: cause: FillCause; qty: float; ts: int
@dataclass(frozen=True)
class MarginCall: margin_ratio: float; ts: int
@dataclass(frozen=True)
class FundingCharged: amount: float; ts: int
@dataclass(frozen=True)
class AccountConfigChanged: what: str; ts: int
@dataclass(frozen=True)
class VenueStateChanged: state: str; ts: int
@dataclass(frozen=True)
class StreamTokenExpired: ts: int
AccountEvent = Union[Fill, OrderStateChanged, PositionChangedExternally, MarginCall, FundingCharged,
                     AccountConfigChanged, VenueStateChanged, StreamTokenExpired]

@dataclass(frozen=True)
class Confirmed: bar: NormalizedBar
@dataclass(frozen=True)
class Forming: bar: NormalizedBar
@dataclass(frozen=True)
class Revised: bar: NormalizedBar; prior: NormalizedBar
@dataclass(frozen=True)
class Gap: from_ts: int; to_ts: int
BarEvent = Union[Confirmed, Forming, Revised, Gap]

@dataclass(frozen=True)
class Tick: tick: NormalizedTick
@dataclass(frozen=True)
class TickGap: from_seq: int; to_seq: int; healed: bool
TickEvent = Union[Tick, TickGap]

@dataclass(frozen=True)
class AdapterError(Exception):
    retryable: bool; retry_after_ms: int; reason_class: ReasonClass; message: str = ""
    def __str__(self) -> str: return f"{self.reason_class.value}: {self.message}"

@dataclass(frozen=True)
class Capabilities:
    conditional_orders: bool; close_position: bool; reduce_only: bool; trigger_bases: tuple[TriggerBasis, ...]
    amend: bool; client_id_on_fills: bool; adapter_api_version: int
