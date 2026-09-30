"""Trade identity (spec §4) and intent keys."""
from __future__ import annotations
import dataclasses
import math
from dataclasses import dataclass
from typing import Sequence
from pineforge_live.types import canonical_sha256
from pineforge_live.engine.report import TradeRow

# The pending-order mirror's `type` codes, by position: the engine's former
# `enum class OrderType { MARKET, ENTRY, EXIT, RAW_ORDER }`. Engine v1.0.0
# keeps the codes but drops the enum: `mirror_order_type` in
# src/source/pine_adapter.cpp maps each Pine order family to one of them.
ORDER_TYPE_NAMES: tuple[str, ...] = ("MARKET", "ENTRY", "EXIT", "RAW_ORDER")

# Names that denote an entry order. Derived from ORDER_TYPE_NAMES: anything
# containing "ENTRY". IntentKey.kind stores the enum name (never translated
# to "ENTRY"/"EXIT"; see `intent_kind` for the one MARKET case); this is how
# callers classify it.
ENTRY_KINDS: frozenset[str] = frozenset(n for n in ORDER_TYPE_NAMES if "ENTRY" in n)


def is_entry_kind(name: str) -> bool:
    """Whether `name` (a raw `IntentKey.kind`/OrderType name) is one of ENTRY_KINDS."""
    return name in ENTRY_KINDS


def order_type_name(code: int) -> str:
    """The `OrderType` enum member name for the engine's numeric `code` (its declaration index)."""
    if 0 <= code < len(ORDER_TYPE_NAMES):
        return ORDER_TYPE_NAMES[code]
    raise ValueError(f"unknown OrderType code {code}")


@dataclass(frozen=True, order=True)
class TradeKey:
    """A closed trade's identity (spec §4): `(entry_bar, entry_id, exit_bar,
    exit_id, close_cause, is_long, qty, fragment_ordinal)`. `order=True` so a
    list of these sorts deterministically (field order above is the sort
    precedence, `fragment_ordinal` breaking ties between identical trades)."""
    entry_bar: int; entry_id: str; exit_bar: int; exit_id: str; close_cause: int; is_long: bool; qty: float; fragment_ordinal: int


def trade_keys(trades: Sequence[TradeRow]) -> list[TradeKey]:
    """`TradeKey`s for `trades`, in the SAME order the engine reported them
    (not sorted). Two trades sharing every identity field but `qty`/order
    (e.g. a partial-exit fragment) are disambiguated by `fragment_ordinal`,
    a 0-based counter over repeats of the same base tuple."""
    seen: dict[tuple, int] = {}
    out = []
    for t in trades:
        base = (t.entry_bar_index, t.entry_id, t.exit_bar_index, t.exit_id, t.close_cause, t.is_long, t.qty)
        n = seen.get(base, 0); seen[base] = n + 1
        out.append(TradeKey(*base, n))
    return out


def trades_sha256(trades: Sequence[TradeRow]) -> str:
    """Canonical digest of `trade_keys(trades)`, in report order. The
    settlement layer's G1 check and `settlements.trades_sha256` column both
    rely on this being order-sensitive (a reordering of otherwise-identical
    trades must digest differently)."""
    return canonical_sha256([list(dataclasses.astuple(k)) for k in trade_keys(trades)])


def _escape(s: str) -> str:
    """Backslash-escape `\\` and `|` so `s` can occupy one `|`-joined field
    without being mistaken for a field boundary; inverse of `_unescape`."""
    return s.replace("\\", "\\\\").replace("|", "\\|")


def _unescape(s: str) -> str:
    """Inverse of `_escape`: collapses each `\\` + next-character pair back
    to that character (`\\\\` -> `\\`, `\\|` -> `|`)."""
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(s[i + 1]); i += 2
        else:
            out.append(c); i += 1
    return "".join(out)


def _split_unescaped(s: str) -> list[str]:
    """Split `s` on `|` characters not preceded by an (unescaped) `\\`.
    Each returned part is still in escaped form -- pass it through
    `_unescape` individually to recover the original field."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            cur.append(s[i]); cur.append(s[i + 1]); i += 2
        elif c == "|":
            parts.append("".join(cur)); cur = []; i += 1
        else:
            cur.append(c); i += 1
    parts.append("".join(cur))
    return parts


@dataclass(frozen=True)
class IntentKey:
    """One pending order's identity within one position cycle (spec §4):
    `(pine order id, kind, from_entry, created position cycle seq)`. `.s`
    is the stable string form used as the journal's `intents`/`actions`
    `intent_key` PRIMARY KEY and the settled book's dict key, so it MUST be
    injective -- a collision there would silently merge two distinct
    intents. Pine order ids (`order_id`/`from_entry`) are arbitrary
    strings and may legally contain `|` or `\\`, so `.s` backslash-escapes
    those two fields (`kind`/`created_cycle_seq` never contain either
    character and are joined verbatim)."""
    order_id: str; kind: str; from_entry: str; created_cycle_seq: int

    @property
    def s(self) -> str:
        """The `|`-joined string form; see the class docstring for the escaping rule."""
        return f"{_escape(self.order_id)}|{self.kind}|{_escape(self.from_entry)}|{self.created_cycle_seq}"

    @classmethod
    def parse(cls, s: str) -> "IntentKey":
        """Inverse of `.s`. Raises `ValueError` if `s` does not split into
        exactly 4 unescaped-`|`-separated fields, rather than silently
        misassigning a malformed or truncated string."""
        parts = _split_unescaped(s)
        if len(parts) != 4:
            raise ValueError(f"IntentKey.parse: expected 4 fields, got {len(parts)}: {s!r}")
        o, k, f, c = parts
        return cls(_unescape(o), k, _unescape(f), int(c))


def intent_kind(po: dict) -> str:
    """The `IntentKey.kind` of one engine pending-order mirror row `po`: its
    `type` name, except that an ENTRY with neither a limit nor a stop level
    is a MARKET entry.

    Engine v1.0.0 reports every `strategy.entry` as ENTRY, market or priced;
    the ABI-v4 engine before it reported a market entry as MARKET. The
    mirror's `limit_price`/`stop_price` are the requested levels, NaN when
    absent, so the market entry is found the same way on both and keeps the
    MARKET kind that `Intent.is_market` (LiveCore's `MARKET_AT_OPEN` legs)
    reads. Both fields are read strictly: a mirror without them must fail
    loudly, not turn every priced entry into a market order."""
    kind = order_type_name(int(po["type"]))
    if kind == "ENTRY" and math.isnan(po["limit_price"]) and math.isnan(po["stop_price"]):
        return "MARKET"
    return kind


def intent_key_for(po: dict, cycle_seq: int) -> IntentKey:
    """`IntentKey` for one engine pending-order mirror row `po`, tagged with
    the settlement's `cycle_seq` (the position cycle it was created under)."""
    return IntentKey(po["id"], intent_kind(po), po.get("from_entry", ""), int(cycle_seq))


def keys_sha256(keys: Sequence[TradeKey]) -> str:
    """sha256 (hex) over an already-computed, ORDER-SENSITIVE list of
    `TradeKey`s -- the same digest `trades_sha256` computes from raw
    trades, but taking keys directly so `ledger.settle()`'s G1 check can
    hash an arbitrary sub-prefix of `SettleResult.keys` without re-deriving
    keys from trades. Moved here from `ledger.py` (single copy; `ledger.py`
    imports it) per the Task 6 prelim ruling."""
    return canonical_sha256([list(dataclasses.astuple(k)) for k in keys])
