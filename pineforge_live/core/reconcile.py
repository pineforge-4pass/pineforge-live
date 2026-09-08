"""The reconciler (spec §5.4): after each settlement when quiescent."""
from __future__ import annotations
from dataclasses import dataclass, field
from pineforge_live import types as T
from .classify import ClassifiedFill, FillClass

@dataclass(frozen=True)
class DeadBand:
    lot_step: float; min_qty: float; min_notional: float
    def qty(self, price: float) -> float:
        return max(self.lot_step, self.min_qty, self.min_notional / price if price > 0 else 0.0)

@dataclass(frozen=True)
class ReconcileConfig:
    max_missed_age_bars: int; max_missed_entry_distance_bps: float; budget_notional: float; mirror_early_daily_cap: int; adopt_ledger_position: bool

@dataclass(frozen=True)
class CorrectionRequest:
    kind: str; side: T.Side; qty: float; reason: str; intent: str | None

@dataclass
class ReconcileInput:
    bar_index: int; classified: list[ClassifiedFill]; ledger_position: float; real_position: float; our_signed_fills: float; price: float
    quiescent: bool; in_flight: set[str]; stop_level: T.StopLevel; missed_age_bars: int; missed_distance_bps: float; cfg: ReconcileConfig
    dead_band: DeadBand; mirror_early_today: int

@dataclass
class ReconcileDecision:
    corrections: list[CorrectionRequest] = field(default_factory=list)
    stop: tuple[T.StopLevel, T.StopDisposition, str] | None = None
    skipped_cycle: bool = False; residual_qty: float = 0.0; counters: dict[str, int] = field(default_factory=dict)

def _side_for(delta: float) -> T.Side:
    return T.Side.BUY if delta > 0 else T.Side.SELL

def _escalate(d: ReconcileDecision, level: T.StopLevel, disp: T.StopDisposition, cause: str):
    rank = {T.StopLevel.NONE: 0, T.StopLevel.FLAT_ONLY: 1, T.StopLevel.HARD: 2}
    if d.stop is None or rank[level] > rank[d.stop[0]] or (level == d.stop[0] and disp == T.StopDisposition.FLATTEN):
        d.stop = (level, disp, cause)

def reconcile(inp: ReconcileInput) -> ReconcileDecision:
    d = ReconcileDecision(); band = inp.dead_band.qty(inp.price)
    def bump(k): d.counters[k] = d.counters.get(k, 0) + 1
    if not inp.quiescent:
        bump("skipped_not_quiescent"); return d
    flat_only = inp.stop_level in (T.StopLevel.FLAT_ONLY, T.StopLevel.HARD)
    for c in inp.classified:
        e, cls = c.emulated, c.cls
        if cls == FillClass.MISSED and e is not None:
            within = inp.missed_age_bars <= inp.cfg.max_missed_age_bars and inp.missed_distance_bps <= inp.cfg.max_missed_entry_distance_bps
            increases = (e.leg == "ENTRY")
            if (not within and not inp.cfg.adopt_ledger_position):
                d.skipped_cycle = True; bump("skipped_cycle"); continue
            if increases and flat_only:
                bump("refused_flat_only"); continue
            if increases and e.qty * inp.price > inp.cfg.budget_notional:
                bump("refused_budget"); d.skipped_cycle = True; continue
            # Task 8 carry (Task 5 review finding 11): a MISSED correction
            # is emitted only when the REAL position is a STRICT SUBSET of
            # the LEDGER position on that side (abs(real) < abs(ledger),
            # same sign, or real == 0) -- real == ledger means nothing is
            # actually missing (an unresolved "?" intent can turn a
            # correctly-filled entry into a spurious MISSED alongside a
            # CONFIRMED-with-note venue fill -- see classify.py's
            # docstring), and real beyond the ledger or on the OPPOSITE
            # side isn't a "missing fill" a MARKET_CORRECT can sensibly
            # patch either. Otherwise counted, never corrected.
            subset = inp.real_position == 0.0 or (abs(inp.real_position) < abs(inp.ledger_position)
                                                    and (inp.real_position > 0) == (inp.ledger_position > 0))
            if not subset:
                bump("skipped_position_mismatch"); continue
            side = (T.Side.BUY if e.is_long else T.Side.SELL) if e.leg == "ENTRY" else (T.Side.SELL if e.is_long else T.Side.BUY)
            d.corrections.append(CorrectionRequest("MARKET_CORRECT", side, e.qty, "MISSED", e.intent)); bump("missed_corrected")
        elif cls == FillClass.QTY_DIVERGENT and e is not None:
            # residual_qty's sign convention: `ledger_position - real_position`
            # (the still-uncorrected SHORTFALL) -- positive means real is
            # short of the ledger (a would-be BUY/top-up we didn't place),
            # negative means real carries an excess we're tolerating (a
            # would-be trim we didn't place). Both accumulation sites below
            # use the same `-delta` so a caller reading `residual_qty`
            # across bars/branches gets one consistent sign.
            delta = inp.real_position - inp.ledger_position
            if abs(delta) <= band:
                d.residual_qty += -delta; bump("residual_carried"); continue
            if abs(delta) > 0 and (delta > 0) == (inp.ledger_position >= 0):   # real exposure larger than ledger -> trim
                d.corrections.append(CorrectionRequest("REDUCE_ONLY_TRIM", _side_for(-delta), abs(delta), "QTY_DIVERGENT", e.intent)); bump("trimmed")
            elif flat_only:
                d.residual_qty += -delta; bump("refused_flat_only")
            else:
                d.corrections.append(CorrectionRequest("TOP_UP", _side_for(-delta), abs(delta), "QTY_DIVERGENT", e.intent)); bump("topped_up")
        elif cls == FillClass.PATH_DIVERGENT:
            bump("path_divergent")
        elif cls == FillClass.MIRROR_EARLY:
            bump("mirror_early")
            if inp.mirror_early_today + d.counters["mirror_early"] > inp.cfg.mirror_early_daily_cap:
                _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "MIRROR_EARLY daily cap")
        elif cls in (FillClass.TRIGGER_REVERSED, FillClass.ENTRY_SLIP):
            if inp.real_position != 0.0:
                d.corrections.append(CorrectionRequest("FLATTEN", _side_for(-inp.real_position), abs(inp.real_position), cls.value, None))
            _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, cls.value)
        elif cls == FillClass.RETRACTED:
            _escalate(d, T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE, "RETRACTED: real ≠ ledger beyond dead-band")
        elif cls == FillClass.UNATTRIBUTED_VENUE:
            _escalate(d, T.StopLevel.HARD, T.StopDisposition.FLATTEN, "venue-initiated fill")
        elif cls in (FillClass.CONFIRMED, FillClass.IN_FLIGHT, FillClass.SYNTHETIC):
            bump(cls.value.lower())
    return d
