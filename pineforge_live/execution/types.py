"""Explicit receipt types: execution of an action is not an engine fill."""
from __future__ import annotations

from dataclasses import dataclass, field
from pineforge_live.core.classify import VenueFill
from pineforge_live import types as T


class ExecutionSafetyError(RuntimeError):
    """An unresolved execution fact that must fence new exposure."""


@dataclass(frozen=True)
class ExecutionReceipt:
    trade_key: str
    client_id: str | None
    venue_order_id: str
    origin_bar_index: int | None
    expected_ledger_bar_index: int | None
    observed_bar_index: int
    receipt_mode: str
    intent: str | None
    leg: str | None
    side: T.Side
    qty: float
    price: float
    fee: float
    cause: T.FillCause
    executed_trigger: bool = False


@dataclass(frozen=True)
class ExecutionSnapshot:
    ledger_fills: list[VenueFill] = field(default_factory=list)
    receipts: list[ExecutionReceipt] = field(default_factory=list)
    late_receipts: list[ExecutionReceipt] = field(default_factory=list)
    in_flight: set[str] = field(default_factory=set)
    mirrored: set[str] = field(default_factory=set)
    our_signed_fills: float = 0.0
    fill_watermark: int = 0
    unresolved: list[str] = field(default_factory=list)
    terminal_residuals: dict[str, float] = field(default_factory=dict)
