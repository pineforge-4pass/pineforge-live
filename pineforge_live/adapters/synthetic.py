"""Deterministic mock trade paths from confirmed minute candles.

These paths are test inputs, not recovered historical ticks. Empty minutes
produce an explicit minute boundary carrying their OHLCV and no trade ticks;
their quoted high/low/close remain part of the chart-feed aggregation policy.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, replace

from pineforge_live import types as T
from pineforge_live.bars.minute import MINUTE_MS, _integer, validate_minute
from pineforge_live.bars.calendar import ParentWindows

SYNTHETIC_POLICIES = ("high-first", "low-first", "seeded")
_OFFSETS = (0, 20_000, 40_000, 59_999)


@dataclass(frozen=True)
class SyntheticMinute:
    bar: T.NormalizedBar
    ticks: tuple[T.NormalizedTick, ...]

    @property
    def ts_close(self) -> int:
        return self.bar.ts_open + MINUTE_MS

    def reconstructed_bar(self) -> T.NormalizedBar:
        """Fold generated trades; empty minutes retain explicit quote metadata.

        Trade count measures the mock ticks, not an unknown historical count.
        OHLCV is independently folded for every positive-volume minute.
        """
        if not self.ticks:
            return replace(self.bar, trade_count=0)
        return T.NormalizedBar(
            self.bar.ts_open, self.ticks[0].price, max(t.price for t in self.ticks),
            min(t.price for t in self.ticks), self.ticks[-1].price,
            sum(t.qty for t in self.ticks), len(self.ticks), synthesized=self.bar.synthesized,
        )


class SyntheticMinuteTicks:
    """Four positive-quantity ticks per nonempty minute, with contiguous seq.

    Paths are O→H→L→C or O→L→H→C. Seeded mode chooses between these paths
    from the seed and minute timestamp, so restoring a remainder is stable.
    Each timestamp is within its minute; the final print is at +59,999ms.
    The fourth quantity carries the floating-point division remainder.
    """

    def __init__(self, policy: str = "high-first", *, seed: int = 0, start_seq: int = 1,
                 parent_windows=None):
        if policy not in SYNTHETIC_POLICIES:
            raise ValueError(f"unknown synthetic policy; expected one of {SYNTHETIC_POLICIES}")
        _integer(seed, "seed")
        _integer(start_seq, "start_seq")
        self.policy, self.seed, self.next_seq = policy, seed, start_seq
        self.calendar=ParentWindows(parent_windows) if parent_windows is not None else None
        self._last: SyntheticMinute | None = None

    def _packet(self, bar: T.NormalizedBar, start_seq: int) -> SyntheticMinute:
        if bar.v == 0:
            return SyntheticMinute(bar, ())
        high_first = self.policy == "high-first"
        if self.policy == "seeded":
            high_first = bool(random.Random(f"{self.seed}:{bar.ts_open}").getrandbits(1))
        prices = (bar.o, bar.h, bar.l, bar.c) if high_first else (bar.o, bar.l, bar.h, bar.c)
        quantities = [bar.v / 4] * 3
        quantities.append(bar.v - sum(quantities))
        if min(quantities) <= 0:
            raise ValueError("minute volume is too small for four positive-quantity ticks")
        _integer(start_seq + 4, "next_seq")
        return SyntheticMinute(bar, tuple(
            T.NormalizedTick(bar.ts_open + offset, start_seq + i, price, quantity)
            for i, (offset, price, quantity) in enumerate(zip(_OFFSETS, prices, quantities))
        ))

    def push(self, bar: T.NormalizedBar) -> SyntheticMinute:
        validate_minute(bar)
        if self.calendar is not None:
            self.calendar.containing(bar.ts_open)
        if self._last is not None:
            if bar.ts_open == self._last.bar.ts_open:
                if bar == self._last.bar:
                    return self._last
                raise ValueError("changed duplicate synthetic minute")
            expected=(self.calendar.next_minute(self._last.bar.ts_open) if self.calendar is not None
                      else self._last.ts_close)
            if bar.ts_open != expected:
                raise ValueError("synthetic minutes must be contiguous and increasing")
        packet = self._packet(bar, self.next_seq)
        self.next_seq += len(packet.ticks)
        self._last = packet
        return packet

    def export_state(self) -> dict:
        return {"version": 1, "policy": self.policy, "seed": self.seed,
                "next_seq": self.next_seq,
                "parent_windows": self.calendar.records if self.calendar is not None else None,
                "last_minute": asdict(self._last.bar) if self._last is not None else None}

    @classmethod
    def from_state(cls, state: dict) -> "SyntheticMinuteTicks":
        if (not isinstance(state, dict)
                or set(state) != {"version", "policy", "seed", "next_seq", "last_minute", "parent_windows"}
                or type(state["version"]) is not int or state["version"] != 1):
            raise ValueError("invalid synthetic-minute checkpoint")
        result = cls(state["policy"], seed=state["seed"], start_seq=state["next_seq"],parent_windows=state['parent_windows'])
        if state["last_minute"] is not None:
            try:
                bar = T.NormalizedBar(**state["last_minute"])
            except (TypeError, KeyError):
                raise ValueError("invalid synthetic-minute checkpoint bar") from None
            validate_minute(bar)
            if result.calendar is not None:result.calendar.containing(bar.ts_open)
            start_seq = result.next_seq - (4 if bar.v > 0 else 0)
            _integer(start_seq, "last minute start_seq")
            result._last = result._packet(bar, start_seq)
        return result
