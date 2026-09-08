"""Epoch, code identity and runtime configuration (spec §1)."""
from __future__ import annotations
import copy
from collections.abc import Sequence
from dataclasses import dataclass, field, asdict
from pineforge_live import ADAPTER_API_VERSION
from pineforge_live import types as T
from pineforge_live.bars.policy import BAR_POLICY_VERSION, tf_ms
from pineforge_live.types import canonical_sha256
from pineforge_live.bars.calendar import normalize_windows

@dataclass(frozen=True)
class CodeIdentity:
    """The engine/codegen/strategy-source identity a run was built from
    (spec §1's "code identity incl. build receipt"). `build_receipt` is
    typed as a free dict here, but spec [r4] pins its shape to four
    fields -- codegen sha, source sha, compiler id, sha256(.so) -- and
    callers are expected to populate exactly those four keys (N3).
    Deep-copied at construction (F4, same class of bug as
    `EngineSyminfo`'s defensive copy): without it a caller mutating its
    own dict after constructing this would silently change epoch_hash()."""
    engine_bundle_sha: str; codegen_bundle_sha: str; strategy_source_sha: str; build_receipt: dict

    def __post_init__(self):
        object.__setattr__(self, "build_receipt", copy.deepcopy(self.build_receipt))

@dataclass(frozen=True)
class RuntimeConfig:
    """Execution parameters that shape the action stream; hashed separately (§1)."""
    poll_interval_ms: int; drain_bound_ms: int; grace_ms: int; open_wait_ms: int; risk_limits: dict
    dead_band_ticks: int = 2; min_replace_interval_ms: int = 1000; max_eval_rate: int = 5

    def __post_init__(self):
        # Final 4: same class of bug as CodeIdentity.build_receipt (F4) --
        # without a defensive copy, a caller mutating its own risk_limits
        # dict after construction would silently change hash().
        object.__setattr__(self, "risk_limits", copy.deepcopy(self.risk_limits))

    def hash(self) -> str:
        """SHA-256 over this config's own fields, deliberately separate
        from `EpochSpec.epoch_hash()` (spec §1): changing a RuntimeConfig
        field must never change the epoch hash, and vice versa."""
        return canonical_sha256(asdict(self))

@dataclass(frozen=True)
class EpochSpec:
    """Everything spec §1 says identifies one live epoch: venue/instrument/
    timeframe, the code that will run, the syminfo surface, and the
    policy knobs the epoch hash freezes.

    `inputs`/`overrides` are coerced to tuples of tuples at construction
    (F4): the caller's original list can be appended to after
    construction without the frozen dataclass noticing, which used to
    change `epoch_hash()` after the fact. `script_tf` is validated via
    `tf_ms()` at construction (F6), the same validator `run_full()` uses,
    so an epoch that could never run is rejected where it is built, not
    on first use.

    `realtime_tail` and `horizon_bars` ARE applied, via
    `setter_sequence()`'s `set_realtime_tail` call, so they are both
    hashed and enforced. `probe_suppress_tail_logic` and
    `path_order_policy` are hashed here too but are NOT applied by
    `setter_sequence()` -- they are per-run probe flags owned by Plan B2,
    which applies them via `EngineHandle.run_full(..., per_run=(...))`
    (Final 2): replayed on top of this epoch's prefix for just that one
    run, never appended to `setter_log`, so `apply_epoch()`'s
    `setter_log == setter_sequence()` invariant survives any number of
    probe/evaluate calls.

    `chart_timezone` is separate from the exchange's `syminfo.timezone`.
    Absence retains the original behavior of using the exchange timezone
    for both setters. An empty string explicitly chooses the engine's UTC
    chart-time fast path without changing exchange/session calculations.
    """
    venue: str; instrument: T.InstrumentId; script_tf: str; history_start_ms: int; horizon_bars: int
    code_identity: CodeIdentity; syminfo: T.EngineSyminfo; reference_tape_sha256: str
    # Constructor input may be any Sequence of (key, value) pairs; __post_init__
    # coerces to tuple[tuple[str, str], ...] below (F4), which is what every
    # instance actually holds -- annotated as the input shape, not the stored one.
    inputs: Sequence[tuple[str, str]] = field(default_factory=list)
    overrides: Sequence[tuple[str, str]] = field(default_factory=list)
    realtime_tail: bool = True; probe_suppress_tail_logic: bool = True
    path_order_policy: str = "AUTO+OTHER"; trail_refresh_policy: str = "bar_open_level"
    trade_start_ms: int | None = None
    parent_windows: tuple[tuple[int,int], ...] | None = None
    chart_timezone: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "inputs", tuple(tuple(kv) for kv in self.inputs))
        object.__setattr__(self, "overrides", tuple(tuple(kv) for kv in self.overrides))
        tf_ms(self.script_tf)  # F6: reject an epoch that can never run, where it is built
        if self.chart_timezone is not None:
            if not isinstance(self.chart_timezone, str) or '\x00' in self.chart_timezone:
                raise ValueError('chart_timezone must be a string or None')
            # Absence keeps the original exchange-timezone setter and hash.
            # An explicitly equal value is the same configuration; empty is
            # distinct and requests the engine's UTC chart-time fast path.
            if self.chart_timezone == self.syminfo.timezone:
                object.__setattr__(self, "chart_timezone", None)
        object.__setattr__(self, "parent_windows", normalize_windows(self.parent_windows))
        object.__setattr__(self, "_parent_windows_sha256", canonical_sha256(self.parent_windows) if self.parent_windows is not None else None)

    def setter_sequence(self) -> list[tuple[str, tuple]]:
        """The exact ordered sequence of `strategy_set_*` calls
        `apply_epoch()` replays onto a fresh engine strategy, and that
        `epoch_hash()` covers (spec §1's "exact ordered sequence of
        strategy_set_* calls the runtime makes").

        Syminfo string keys with an empty value are omitted (F3): the
        engine's `set_syminfo_string` returns rc=-1 (rejected) for an
        empty value, so emitting one here would only make
        `apply_epoch()`'s rc check fail on a perfectly normal syminfo
        (e.g. an empty `description`) -- the value is still part of the
        epoch via `engine_syminfo_hash`, just not a setter call.

        Final 1: `pricescale`/`minmove` are typed `EngineSyminfo` fields
        (hashed via `engine_syminfo_hash`) but the engine only ever learns
        them through `strategy_set_syminfo_metadata("pricescale"|"minmove",
        v)` -- the same channel as `numeric_metadata`, keyed by the names
        `engine.hpp` documents for its syminfo-metadata map. Without this,
        a script reading `syminfo.pricescale`/`syminfo.minmove` runs live
        with `na()` despite being graded with the real value. Emitted right
        after `set_syminfo_pointvalue` and before the caller's
        `numeric_metadata` keys; `EngineSyminfo.__post_init__` rejects a
        `numeric_metadata` that also carries either key, so there is no
        second source these two calls could disagree with.

        `set_realtime_tail` and `set_broker_state_hash_recording` are
        appended after inputs/overrides and before the optional
        `set_trade_start_time` -- see the class docstring for what this
        sequence deliberately does NOT apply.
        """
        s = self.syminfo
        chart_timezone = s.timezone if self.chart_timezone is None else self.chart_timezone
        seq: list[tuple[str, tuple]] = [("set_chart_timezone", (chart_timezone,)), ("set_syminfo_timezone", (s.timezone,)),
                                        ("set_syminfo_session", (s.session,)), ("set_syminfo_type", (s.type,))]
        for key in ("ticker", "tickerid", "currency", "basecurrency", "description", "volumetype"):
            value = getattr(s, key)
            if value:
                seq.append(("set_syminfo_string", (key, value)))
        seq += [("set_syminfo_mintick", (s.mintick,)), ("set_syminfo_pointvalue", (s.pointvalue,))]
        seq.append(("set_syminfo_metadata", ("pricescale", float(s.pricescale))))
        seq.append(("set_syminfo_metadata", ("minmove", float(s.minmove))))
        for k in sorted(s.numeric_metadata):
            seq.append(("set_syminfo_metadata", (k, s.numeric_metadata[k])))
        seq += [("set_input", (k, v)) for k, v in self.inputs]
        seq += [("set_override", (k, v)) for k, v in self.overrides]
        seq.append(("set_realtime_tail", (self.realtime_tail, self.horizon_bars)))
        seq.append(("set_broker_state_hash_recording", (True,)))
        if self.trade_start_ms is not None:
            seq.append(("set_trade_start_time", (self.trade_start_ms,)))
        return seq

    def epoch_hash(self) -> str:
        """SHA-256 over every §1 field: the setter sequence, venue/
        instrument, `input_tf_eq_script_tf` (N2: a constant `True`
        marker recording the script-TF-feed decision -- there is no
        separate `input_tf` field to compare against, it never checks
        anything), script_tf, the history window, code identity, the
        tail/path-order policy fields (hashed even though only two of
        them are actually applied -- see `setter_sequence()`'s
        docstring), bar/adapter policy versions, the syminfo hash and
        the reference tape sha."""
        identity = {
            "setter_sequence": self.setter_sequence(), "venue": self.venue, "instrument": self.instrument.key(),
            "input_tf_eq_script_tf": True, "script_tf": self.script_tf, "history_start": self.history_start_ms,
            "horizon_bars": self.horizon_bars, "code_identity": asdict(self.code_identity),
            "realtime_tail": self.realtime_tail, "probe_suppress_tail_logic": self.probe_suppress_tail_logic,
            "path_order_policy": self.path_order_policy, "trail_refresh_policy": self.trail_refresh_policy,
            "bar_policy_version": BAR_POLICY_VERSION, "adapter_api_version": ADAPTER_API_VERSION,
            "engine_syminfo_hash": self.syminfo.hash(), "reference_tape_sha256": self.reference_tape_sha256}
        if self.parent_windows is not None:
            identity['parent_windows_sha256'] = self._parent_windows_sha256
        return canonical_sha256(identity)

def apply_epoch(handle, spec: EpochSpec) -> list[tuple[str, tuple]]:
    """Configure `handle` for a live epoch: clear its setter log, replay
    every call in `spec.setter_sequence()` through the handle's public
    `set_*` methods (each lands on the handle's CURRENT live strategy
    immediately and is appended to `handle.setter_log`, which is what
    every future `run_full()` replays onto that run's fresh strategy --
    see `EngineHandle`'s docstring), then verify the resulting log
    matches the sequence exactly. "Verified" here means exactly that log
    equality: the ordered record of calls this Python binding actually
    made, which is also everything `epoch_hash()` covers and everything
    `run_full()` will replay -- there is no separate "engine-side" log to
    check against.

    Raises RuntimeError, fail-fast, on either a rejected syminfo-string
    setter (non-zero rc) or a log/sequence mismatch. In both cases
    `handle.setter_log` (and the handle's live strategy) is left holding
    only the partial prefix up to the failure -- the caller must discard
    the handle rather than continue using or retrying it.
    """
    handle.setter_log.clear()
    for name, args in spec.setter_sequence():
        rc = getattr(handle, name)(*args)
        if name == "set_syminfo_string" and rc:
            # Configuration-time failure: fail fast rather than deferring to
            # the next run, where a silently-unset syminfo string would show
            # up only as a downstream parity mismatch.
            raise RuntimeError(f"engine rejected syminfo string {args[0]!r} (rc={rc}); discard the handle")
    if handle.setter_log != spec.setter_sequence():
        raise RuntimeError("engine setter log diverged from the epoch's setter sequence; discard the handle")
    return list(handle.setter_log)
