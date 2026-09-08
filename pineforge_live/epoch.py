"""Epoch, code identity and runtime configuration (spec §1)."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from pineforge_live import ADAPTER_API_VERSION
from pineforge_live import types as T
from pineforge_live.bars.policy import BAR_POLICY_VERSION
from pineforge_live.types import canonical_sha256

@dataclass(frozen=True)
class CodeIdentity:
    engine_bundle_sha: str; codegen_bundle_sha: str; strategy_source_sha: str; build_receipt: dict

@dataclass(frozen=True)
class RuntimeConfig:
    """Execution parameters that shape the action stream; hashed separately (§1)."""
    poll_interval_ms: int; drain_bound_ms: int; grace_ms: int; open_wait_ms: int; risk_limits: dict
    dead_band_ticks: int = 2; min_replace_interval_ms: int = 1000; max_eval_rate: int = 5
    def hash(self) -> str: return canonical_sha256(asdict(self))

@dataclass(frozen=True)
class EpochSpec:
    venue: str; instrument: T.InstrumentId; script_tf: str; history_start_ms: int; horizon_bars: int
    code_identity: CodeIdentity; syminfo: T.EngineSyminfo; reference_tape_sha256: str
    inputs: list[tuple[str, str]] = field(default_factory=list)
    overrides: list[tuple[str, str]] = field(default_factory=list)
    realtime_tail: bool = True; probe_suppress_tail_logic: bool = True
    path_order_policy: str = "AUTO+OTHER"; trail_refresh_policy: str = "bar_open_level"
    trade_start_ms: int | None = None

    def setter_sequence(self) -> list[tuple[str, tuple]]:
        s = self.syminfo
        seq: list[tuple[str, tuple]] = [("set_chart_timezone", (s.timezone,)), ("set_syminfo_timezone", (s.timezone,)),
                                        ("set_syminfo_session", (s.session,)), ("set_syminfo_type", (s.type,))]
        for key in ("ticker", "tickerid", "currency", "basecurrency", "description", "volumetype"):
            seq.append(("set_syminfo_string", (key, getattr(s, key))))
        seq += [("set_syminfo_mintick", (s.mintick,)), ("set_syminfo_pointvalue", (s.pointvalue,))]
        for k in sorted(s.numeric_metadata):
            seq.append(("set_syminfo_metadata", (k, s.numeric_metadata[k])))
        seq += [("set_input", (k, v)) for k, v in self.inputs]
        seq += [("set_override", (k, v)) for k, v in self.overrides]
        if self.trade_start_ms is not None:
            seq.append(("set_trade_start_time", (self.trade_start_ms,)))
        return seq

    def epoch_hash(self) -> str:
        return canonical_sha256({
            "setter_sequence": self.setter_sequence(), "venue": self.venue, "instrument": self.instrument.key(),
            "input_tf_eq_script_tf": True, "script_tf": self.script_tf, "history_start": self.history_start_ms,
            "horizon_bars": self.horizon_bars, "code_identity": asdict(self.code_identity),
            "realtime_tail": self.realtime_tail, "probe_suppress_tail_logic": self.probe_suppress_tail_logic,
            "path_order_policy": self.path_order_policy, "trail_refresh_policy": self.trail_refresh_policy,
            "bar_policy_version": BAR_POLICY_VERSION, "adapter_api_version": ADAPTER_API_VERSION,
            "engine_syminfo_hash": self.syminfo.hash(), "reference_tape_sha256": self.reference_tape_sha256})

def apply_epoch(handle, spec: EpochSpec) -> list[tuple[str, tuple]]:
    handle.setter_log.clear()
    for name, args in spec.setter_sequence():
        rc = getattr(handle, name)(*args)
        if name == "set_syminfo_string" and rc:
            # Configuration-time failure: fail fast rather than deferring to
            # the next run, where a silently-unset syminfo string would show
            # up only as a downstream parity mismatch.
            raise RuntimeError(f"engine rejected syminfo string {args[0]!r} (rc={rc})")
    if handle.setter_log != spec.setter_sequence():
        raise RuntimeError("engine setter log diverged from the epoch's setter sequence")
    return list(handle.setter_log)
