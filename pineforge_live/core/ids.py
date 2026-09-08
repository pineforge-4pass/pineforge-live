"""Trade identity (spec §4) and intent keys."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence
from pineforge_live.types import canonical_sha256
from pineforge_live.engine.report import TradeRow

# Member names of `enum class OrderType { MARKET, ENTRY, EXIT, RAW_ORDER };`
# in ~/code/pineforge-engine-wt/main/include/pineforge/engine.hpp, in
# declaration order (no explicit values assigned, so position == code).
ORDER_TYPE_NAMES: tuple[str, ...] = ("MARKET", "ENTRY", "EXIT", "RAW_ORDER")

# Names that denote an entry order. Derived from ORDER_TYPE_NAMES: anything
# containing "ENTRY". IntentKey.kind stores the raw enum name (never
# translated to "ENTRY"/"EXIT"); this is how callers classify it.
ENTRY_KINDS: frozenset[str] = frozenset(n for n in ORDER_TYPE_NAMES if "ENTRY" in n)


def is_entry_kind(name: str) -> bool:
    return name in ENTRY_KINDS


def order_type_name(code: int) -> str:
    if 0 <= code < len(ORDER_TYPE_NAMES):
        return ORDER_TYPE_NAMES[code]
    raise ValueError(f"unknown OrderType code {code}")


@dataclass(frozen=True, order=True)
class TradeKey:
    entry_bar: int; entry_id: str; exit_bar: int; exit_id: str; close_cause: int; is_long: bool; qty: float; fragment_ordinal: int


def trade_keys(trades: Sequence[TradeRow]) -> list[TradeKey]:
    seen: dict[tuple, int] = {}
    out = []
    for t in trades:
        base = (t.entry_bar_index, t.entry_id, t.exit_bar_index, t.exit_id, t.close_cause, t.is_long, t.qty)
        n = seen.get(base, 0); seen[base] = n + 1
        out.append(TradeKey(*base, n))
    return out


def trades_sha256(trades: Sequence[TradeRow]) -> str:
    return canonical_sha256([list(k.__dict__.values()) for k in trade_keys(trades)])


@dataclass(frozen=True)
class IntentKey:
    order_id: str; kind: str; from_entry: str; created_cycle_seq: int

    @property
    def s(self) -> str:
        return f"{self.order_id}|{self.kind}|{self.from_entry}|{self.created_cycle_seq}"

    @classmethod
    def parse(cls, s: str) -> "IntentKey":
        o, k, f, c = s.split("|", 3)
        return cls(o, k, f, int(c))


def intent_key_for(po: dict, cycle_seq: int) -> IntentKey:
    return IntentKey(po["id"], order_type_name(int(po["type"])), po.get("from_entry", ""), int(cycle_seq))
