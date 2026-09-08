"""The settled book (spec §4 settle 5): intents derived from the engine's pending-order mirror."""
from __future__ import annotations
import enum, math
from dataclasses import dataclass
from pineforge_live.types import canonical_sha256
from .ids import IntentKey, intent_key_for

class IntentState(enum.Enum):
    RESTING = "RESTING"; MODIFIED = "MODIFIED"; CANCELLED = "CANCELLED"; DEAD = "DEAD"

def content_hash(stop, limit, activation, is_long) -> str:
    return canonical_sha256({"stop": stop, "limit": limit, "activation": activation, "is_long": is_long})

@dataclass(frozen=True)
class Intent:
    key: IntentKey; index: int; is_long: bool; kind: str; from_entry: str
    stop: float | None; limit: float | None; activation: float | None; level_resolved: bool; created_bar: int; content_hash: str
    @property
    def is_entry(self) -> bool: return self.kind == "ENTRY"
    @property
    def is_market(self) -> bool:
        # Controller ruling: the settled book's MARKET intents are what
        # LiveCore turns into MARKET_AT_OPEN requests (spec §4 settle 5) --
        # distinct from is_entry, which stays strict to kind=="ENTRY".
        return self.kind == "MARKET"
    @property
    def pure_stop(self) -> bool: return self.stop is not None and self.limit is None

def _num(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

def settled_book(handle, result) -> dict[str, Intent]:
    """The current resting intents, keyed by `IntentKey.s`, built from
    `result.pending_orders` (the engine's pending-order mirror) plus
    `handle.level_resolved`/`handle.effective_levels` per order.

    MUST be called with the SAME `handle` that produced `result` (i.e.
    right after the `run_full()`/`Ledger.seed`/`Ledger.settle` call that
    returned `result`, before any subsequent `run_full`) -- `level_resolved`
    and `effective_levels` are accessors on the handle's LAST run (see
    `EngineHandle.run_full`'s docstring); a later run replaces the live
    strategy and these accessors would silently read the WRONG run.
    """
    out: dict[str, Intent] = {}
    for po in result.pending_orders:
        i = int(po["index"]); key = intent_key_for(po, result.cycle_seq)
        rc, stop, limit, act = handle.effective_levels(i)
        stop, limit, act = (_num(stop), _num(limit), _num(act)) if rc == 0 else (_num(po.get("stop_price")), _num(po.get("limit_price")), None)
        resolved = handle.level_resolved(i) == 1
        created_bar = int(po.get("created_bar", -1))
        out[key.s] = Intent(key, i, bool(po["is_long"]), key.kind, key.from_entry, stop, limit, act, resolved, created_bar,
                            content_hash(stop, limit, act, bool(po["is_long"])))
    return out

def book_diff(prev: dict[str, Intent], cur: dict[str, Intent]) -> dict[str, IntentState]:
    d: dict[str, IntentState] = {}
    for k, it in cur.items():
        if k not in prev: d[k] = IntentState.RESTING
        elif prev[k].content_hash != it.content_hash: d[k] = IntentState.MODIFIED
    for k in prev:
        if k not in cur: d[k] = IntentState.CANCELLED
    return d

def _counts_as_entry(it: Intent, position_size: float) -> bool:
    """Whether `it` should be treated as an "entry" for `dual_entry_guard`.

    A kind=="ENTRY" order always counts. A RAW_ORDER (spec §3.6: a scripted
    `strategy.order()` call bypasses the engine's ENTRY/EXIT
    classification) counts ONLY when it would OPEN or INCREASE exposure --
    its side (`is_long`) agrees with the currently-held position's sign
    (a same-direction/pyramiding add), or the book is flat
    (`position_size == 0.0`, where either side opens new exposure). A
    RAW_ORDER on the side OPPOSITE a held position is a reduce/close (and
    possibly partial-reversal) order, not an entry, and does not count
    here -- kept simple deliberately: v1 does not attempt to compare the
    order's qty against the held size to detect a reversal remainder.
    kind MARKET/EXIT never count as entries.
    """
    if it.kind == "ENTRY":
        return True
    if it.kind == "RAW_ORDER":
        return position_size == 0.0 or it.is_long == (position_size > 0)
    return False

def dual_entry_guard(book: dict[str, Intent], position_size: float) -> bool:
    entries = [it for it in book.values() if _counts_as_entry(it, position_size)]
    longs = [it for it in entries if it.is_long and it.pure_stop]; shorts = [it for it in entries if not it.is_long and it.pure_stop]
    if longs and shorts:
        return True
    if position_size != 0.0:
        want_long = position_size < 0
        return any(it.is_long == want_long and (it.stop is not None or it.limit is not None) for it in entries)
    return False

def mirrorable(it: Intent) -> bool:
    # v1 (interface spec): approximates "EXIT kind, level_resolved, not a
    # partial-exit bracket" as "not an entry, resolved, has a stop or limit
    # price" -- MARKET intents are excluded in practice because the mirror
    # never carries a stop/limit for them (verified against the corpus).
    # Does not yet special-case a RAW_ORDER that is itself acting as an
    # exit (see _counts_as_entry's docstring) -- left for a later task.
    return (not it.is_entry) and it.level_resolved and (it.stop is not None or it.limit is not None)
