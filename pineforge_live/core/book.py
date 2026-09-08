"""The settled book (spec §4 settle 5): intents derived from the engine's pending-order mirror."""
from __future__ import annotations
import enum, math
from dataclasses import dataclass
from pineforge_live.types import canonical_sha256
from .ids import IntentKey, intent_key_for

class IntentState(enum.Enum):
    """One intent's transition between two consecutive settled books
    (`book_diff`). `RESTING` = new this bar; `MODIFIED` = same key,
    different `content_hash`; `CANCELLED` = no longer in the book -- this
    covers BOTH an order the venue/script cancelled AND one that FILLED
    (the mirror drops a row the instant it leaves the book either way);
    disambiguate using `SettleResult.new_closed`/`entry_fills` for the
    bar, not the name alone. `DEAD` is reserved for a consumed
    partial-exit id (spec §4) -- unreachable from `book_diff` itself,
    since a diff over two mirror snapshots can't see which of a
    CANCELLED key's departures was a fill; a fill-aware consumer (Task
    5/8) is expected to produce it, not this function.
    """
    RESTING = "RESTING"; MODIFIED = "MODIFIED"; CANCELLED = "CANCELLED"; DEAD = "DEAD"

def content_hash(stop, limit, activation, is_long, qty) -> str:
    return canonical_sha256({"stop": stop, "limit": limit, "activation": activation, "is_long": is_long, "qty": qty})

@dataclass(frozen=True)
class Intent:
    """One resting pending order in the settled book (spec §4 settle 5),
    keyed by `IntentKey.s` and built by `settled_book` from one row of
    `result.pending_orders` (the engine's pending-order mirror) plus the
    handle's `level_resolved`/`effective_levels` accessors for that row's
    mirror index.

    `index` is the mirror index into the run that produced this Intent --
    like `level_resolved`/`effective_levels` themselves, it (and this
    whole Intent) is stale the instant the handle's next `run_full` runs
    (see `EngineHandle`'s class docstring for the "last run" contract).

    `is_long` is the ORDER's own side, not the position's -- an EXIT
    order closing a long position reads `is_long=False` (it sells to
    close); callers mapping to a venue `Side` need to know which one they
    have.

    `qty`/`qty_percent` are the mirror's raw fields (`None` when the
    order carries no fixed qty, e.g. a `strategy.exit` leg sized to
    "close the position"); `requested_partial` is the mirror's own
    "caller passed qty_percent < 100" flag (spec §5.1) -- see
    `mirrorable` for how these three keep partial-exit/pyramided legs out
    of the v1 venue mirror.
    """
    key: IntentKey; index: int; is_long: bool; kind: str; from_entry: str
    stop: float | None; limit: float | None; activation: float | None; level_resolved: bool; created_bar: int
    qty: float | None; qty_percent: float | None; requested_partial: bool; content_hash: str
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

    Each row is keyed by its OWN `created_position_cycle_seq` (spec §4:
    key = `(id, kind, from_entry, created_position_cycle_seq)`), not by
    the book's current `result.cycle_seq` -- a resting order can survive
    a flat->long->flat cycle unfilled (created while broker-flat,
    `created_position_cycle_seq == 0`) and must keep the SAME key once
    the position opens and `result.cycle_seq` moves on, or it reads as a
    fresh order (`RESTING`) instead of the same one continuing to rest.

    MUST be called with the SAME `handle` that produced `result` (i.e.
    right after the `run_full()`/`Ledger.seed`/`Ledger.settle` call that
    returned `result`, before any subsequent `run_full`) -- `level_resolved`
    and `effective_levels` are accessors on the handle's LAST run (see
    `EngineHandle`'s class docstring); a later run replaces the live
    strategy and these accessors would silently read the WRONG run.

    A stale handle (`effective_levels` rc != 0, or `level_resolved` < 0,
    on an index taken from `result`'s OWN mirror) degrades that one
    Intent to "unresolved, no price" -- the SAME shape as an ordinary
    not-yet-resolved order (rc==0, `level_resolved`==0) -- rather than
    fabricating a stop/limit from the mirror row's raw (possibly stale)
    `stop_price`/`limit_price`, which is what pre-review code did and
    Task 3 review finding 2 correctly flagged as misleading. Finding 2's
    literal suggested fix was a hard `RuntimeError` here; that was tried
    and reverted -- it broke `Probe.evaluate()` (Task 4), which recaptures
    `settled_book(self.h, self.L.last)` at the top of EVERY evaluate()
    call, including calls after the handle has since run one or more
    probe recomputes on top of `result`'s run. Verified live: after a
    single `Probe.evaluate()`, a previously-valid mirror index reads
    `effective_levels` rc=-1 / `level_resolved`=-1 (the probe's recompute
    has a different-sized mirror) -- this is the STEADY STATE for a probe
    issuing multiple evaluate() calls per bar, not a rare contract
    violation, so raising here would make `Probe.evaluate()` fail on
    essentially every call past the first. This function still keys
    every row (so keys/count in the returned book always match
    `result.pending_orders`, regardless of accessor staleness); only the
    stale row's own price fields lose fidelity.
    """
    out: dict[str, Intent] = {}
    for po in result.pending_orders:
        i = int(po["index"]); key = intent_key_for(po, int(po["created_position_cycle_seq"]))
        rc, stop, limit, act = handle.effective_levels(i)
        lr = handle.level_resolved(i)
        if rc != 0 or lr < 0:
            stop = limit = act = None; resolved = False
        else:
            stop, limit, act = _num(stop), _num(limit), _num(act)
            resolved = lr == 1
        created_bar = int(po["created_bar"])
        qty, qty_percent = _num(po.get("qty")), _num(po.get("qty_percent"))
        partial = bool(po.get("requested_partial", 0))
        out[key.s] = Intent(key, i, bool(po["is_long"]), key.kind, key.from_entry, stop, limit, act, resolved, created_bar,
                            qty, qty_percent, partial, content_hash(stop, limit, act, bool(po["is_long"]), qty))
    return out

def book_diff(prev: dict[str, Intent], cur: dict[str, Intent]) -> dict[str, IntentState]:
    """See `IntentState` for what each value means -- in particular,
    `CANCELLED` here means "no longer in `cur`", which includes a fill."""
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

def mirrorable(it: Intent, position_size: float) -> bool:
    """Whether `it` should be placed on the venue as a whole-position
    mirror order (spec §5.1: "EXIT kind, level_resolved, not a
    partial-exit bracket").

    "not a partial-exit bracket" is read from the mirror's own signal
    rather than derived: `it.requested_partial` (the mirror's "caller
    passed qty_percent < 100" flag) directly rules a leg out, and so does
    a fixed `it.qty` smaller than the held position (`abs(it.qty) <
    abs(position_size)`, `position_size == 0.0` treated as "no exclusion
    possible" since there is nothing to compare a resting exit's qty
    against yet) -- both catch the qty_percent-partial and pyramided-leg
    cases the interface spec calls out (e.g. a `HALF_TP` leg at
    `qty_percent=50` closing 1 of 2 held lots would otherwise read
    `mirrorable=True` and go to the venue as a `closePosition=true`
    order that closes the WHOLE position).

    Otherwise unchanged from the v1 approximation: not an entry,
    resolved, has a stop or limit price -- MARKET intents are excluded in
    practice because the mirror never carries a stop/limit for them
    (verified against the corpus). Does not yet special-case a RAW_ORDER
    that is itself acting as an exit (see `_counts_as_entry`'s
    docstring) -- left for a later task.
    """
    if it.is_entry or not it.level_resolved or (it.stop is None and it.limit is None):
        return False
    if it.requested_partial:
        return False
    if position_size != 0.0 and it.qty is not None and abs(it.qty) < abs(position_size):
        return False
    return True
