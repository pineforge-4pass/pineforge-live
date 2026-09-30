"""The settled book (spec §4 settle 5): intents derived from the engine's pending-order mirror."""
from __future__ import annotations
import enum, math
from dataclasses import dataclass
from pineforge_live.types import canonical_sha256
from .ids import IntentKey, intent_key_for, order_type_name

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
    "caller passed qty_percent < 100" flag (spec §5.1); `full_percent_exit_request`
    is the mirror's own "the original exit call was a default/full-percent
    request, before reservation normalization" flag (engine.hpp) -- see
    `mirrorable` for how the qty/qty_percent/requested_partial trio keeps
    partial-exit/pyramided legs out of the v1 venue mirror
    (`full_percent_exit_request` is carried for a later consumer; v1 does
    not read it).
    """
    key: IntentKey; index: int; is_long: bool; kind: str; from_entry: str
    stop: float | None; limit: float | None; activation: float | None; level_resolved: bool; created_bar: int
    qty: float | None; qty_percent: float | None; requested_partial: bool; full_percent_exit_request: bool; content_hash: str
    @property
    def is_entry(self) -> bool: return self.kind == "ENTRY"
    @property
    def is_market(self) -> bool:
        # Controller ruling: the settled book's MARKET intents are what
        # LiveCore turns into MARKET_AT_OPEN requests (spec §4 settle 5) --
        # distinct from is_entry, which stays strict to kind=="ENTRY".
        # `ids.intent_kind` gives a market strategy.entry this kind on engine
        # v1.0.0 too, where the mirror reports it as ENTRY without levels.
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
    a flat->long->flat cycle unfilled (created while broker-flat, where
    the pre-1.0 engine stamps cycle 0 and engine v1.0.0 the last position
    cycle) and must keep the SAME key once the position opens and
    `result.cycle_seq` moves on, or it reads as a fresh order (`RESTING`)
    instead of the same one continuing to rest. Engine v1.0.0 replaces a
    same-id re-issue with a new order stamped with the cycle it is placed
    in, so a re-issue in a later cycle than the original reads as a new key.

    A MARKET key the engine reported as an ENTRY (see `ids.intent_kind`)
    is checked against the handle's own levels: a finite stop or limit
    there means the mirror row lost the levels the key was derived from,
    and treating the order as a market entry would send it at the next
    open, so that raises `RuntimeError` too.

    Contract: MUST be called with the SAME `handle` that produced `result`
    -- immediately after the `run_full()` call that produced it (see
    `Ledger._settled_book`/`SettleResult.book`, and `Probe.evaluate`'s M2
    capture), before any subsequent `run_full` on that handle. `level_resolved`
    and `effective_levels` are accessors on the handle's LAST run only (see
    `EngineHandle`'s class docstring); calling this outside that window
    reads the WRONG run's mirror. A stale read (`effective_levels` rc != 0,
    or `level_resolved` < 0, for a row taken from `result`'s OWN mirror)
    raises `RuntimeError` -- a caller hitting this has broken the contract
    above, not encountered a normal unresolved order (that's rc==0,
    `level_resolved`==0, handled below). This does NOT catch every
    contract violation (a same-size mirror from a LATER run can still read
    a plausible but wrong level under rc==0 -- see `SettleResult.book`'s
    docstring for why only settle-time capture is safe), but it turns the
    detectable half of it into a loud failure instead of a silently
    fabricated price.
    """
    out: dict[str, Intent] = {}
    for po in result.pending_orders:
        i = int(po["index"]); key = intent_key_for(po, int(po["created_position_cycle_seq"]))
        rc, stop, limit, act = handle.effective_levels(i)
        lr = handle.level_resolved(i)
        if rc != 0 or lr < 0:
            raise RuntimeError(f"settled_book: stale handle read for {key.s!r} (mirror index {i}): "
                               f"effective_levels rc={rc}, level_resolved={lr} -- settled_book() must be called "
                               "immediately after the run_full() that produced `result`, before any later run_full()")
        stop, limit, act = _num(stop), _num(limit), _num(act)
        if key.kind == "MARKET" and order_type_name(int(po["type"])) == "ENTRY" and (stop is not None or limit is not None):
            raise RuntimeError(f"settled_book: {key.s!r} (mirror index {i}) has no limit/stop level in its mirror row "
                               f"but effective_levels reports stop={stop}, limit={limit} -- refusing to treat a priced "
                               "entry as a market entry")
        resolved = lr == 1
        created_bar = int(po["created_bar"])
        qty, qty_percent = _num(po["qty"]), _num(po["qty_percent"])
        partial = bool(po["requested_partial"])
        full_percent_exit_request = bool(po["full_percent_exit_request"])
        out[key.s] = Intent(key, i, bool(po["is_long"]), key.kind, key.from_entry, stop, limit, act, resolved, created_bar,
                            qty, qty_percent, partial, full_percent_exit_request,
                            content_hash(stop, limit, act, bool(po["is_long"]), qty))
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

# Member names of `strategy_last_bar_dual_entry_path`'s doxygen table in
# ~/code/pineforge-engine-wt/main/include/pineforge/pineforge.h, codes 0..2
# in the order the doc lists them (there is no C enum for this -- the
# function just returns `int` -- so the names are pinned by a
# header-reading test, same idea as classify.CLOSE_CAUSE_NAMES). `-1` is
# not in the table: it is the NULL-handle error return.
DUAL_ENTRY_PATH_NAMES: tuple[str, ...] = ("None", "LongFirst", "ShortFirst")
DUAL_ENTRY_PATH_NONE: int = DUAL_ENTRY_PATH_NAMES.index("None")


def dual_entry_guard(book: dict[str, Intent], position_size: float, dual_entry_path: int = DUAL_ENTRY_PATH_NONE) -> bool:
    """Spec §4 evaluate 2's "never emit an intrabar TRIGGER when the
    settled book holds two opposite pure-stop entries (`dual_entry_path !=
    None`) or a resting priced entry opposite an open position's reversal
    entry" -- the two clauses have two different sources.

    The FIRST clause is the ENGINE's own signal (m9):
    `RunResult.last_bar_dual_entry_path`, passed in by the caller from the
    probe run it is guarding. The guard arms on a POSITIVE code
    (`LongFirst`/`ShortFirst` -- the engine's broker emulator actually
    arbitrated an opposite pure-stop pair on that bar); `0`
    (`DUAL_ENTRY_PATH_NONE`: "not flat, no matching pair, or neither/only
    one side touched") and the `-1` NULL-handle return do not.
    Re-deriving it from the book -- "any long pure-stop entry rests AND
    any short pure-stop entry rests" -- is strictly broader: it
    suppresses every intrabar entry while a reversal stop merely rests on
    the other side, however far away, so a script that always keeps one
    resting never TRIGGERs at all (the fill then settles as `MISSED` and
    is corrected a bar late, within budget). The engine reports whether
    BOTH legs were actually touched on the path it ran.

    The SECOND clause has no engine signal and stays a book rule: a priced
    (stop or limit) entry resting OPPOSITE an open position is the
    reversal entry, and an intrabar fill of it would cross zero on a
    single print.

    `dual_entry_path` defaults to `DUAL_ENTRY_PATH_NONE` so a caller with
    no run to hand (a book-only consumer) gets the second clause alone
    rather than a fabricated first one."""
    if dual_entry_path > DUAL_ENTRY_PATH_NONE:
        return True
    if position_size != 0.0:
        entries = [it for it in book.values() if _counts_as_entry(it, position_size)]
        want_long = position_size < 0
        return any(it.is_long == want_long and (it.stop is not None or it.limit is not None) for it in entries)
    return False

def mirrorable(it: Intent, position_size: float, eps: float = 1e-9) -> bool:
    """Whether `it` should be placed on the venue as a whole-position
    mirror order (spec §5.1: "EXIT kind, level_resolved, not a
    partial-exit bracket").

    "not a partial-exit bracket" is read from the mirror's own signal
    rather than derived: `it.requested_partial` (the mirror's "caller
    passed qty_percent < 100" flag) directly rules a leg out, and so does
    a fixed `it.qty` smaller than the held position by more than `eps`
    (`abs(it.qty) < abs(position_size) - eps`, `position_size == 0.0`
    treated as "no exclusion possible" since there is nothing to compare
    a resting exit's qty against yet) -- both catch the qty_percent-partial
    and pyramided-leg cases the interface spec calls out (e.g. a `HALF_TP`
    leg at `qty_percent=50` closing 1 of 2 held lots would otherwise read
    `mirrorable=True` and go to the venue as a `closePosition=true` order
    that closes the WHOLE position).

    `eps` is a dead-band tolerance (default 1e-9), not a comparison
    epsilon picked here -- a live caller (LiveCore) is expected to pass
    its own configured dead-band (spec's `DeadBand`), so a fp-summed
    pyramided position (e.g. `0.1+0.1+0.1 == 0.30000000000000004`) with a
    whole-position leg at `qty=0.3` doesn't misread as "qty < position"
    and get wrongly excluded, leaving the position unprotected.

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
    if position_size != 0.0 and it.qty is not None and abs(it.qty) < abs(position_size) - eps:
        return False
    return True
