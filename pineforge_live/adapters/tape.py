"""Tape adapters (spec §8): bars and ticks from recorded feeds; a clock the tape drives."""
from __future__ import annotations
import csv, random
from pathlib import Path
from typing import AsyncIterator
from pineforge_live import types as T
from pineforge_live.bars.policy import tf_ms

PROBE_POINT_FRACTIONS = (1 / 15, 5 / 15, 10 / 15, 14 / 15)   # spec §10.2: 1/5/10/14 minutes of a 15m bar
TICK_POLICIES = ("real", "path4", "path4-reversed", "dense", "random-ohlc")

def load_feed_csv(path: str | Path) -> list[T.NormalizedBar]:
    out = []
    with Path(path).open() as fh:
        for r in csv.DictReader(fh):
            out.append(T.NormalizedBar(int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]),
                                       float(r["close"]), float(r["volume"]), 0))
    return out

class TapeClock:
    def __init__(self, start_ms: int):
        self._now = start_ms
    def now_ms(self) -> int: return self._now
    def venue_now_ms(self) -> int: return self._now
    def skew_ms(self) -> int: return 0
    def advance_to(self, ms: int) -> None:
        if ms > self._now: self._now = ms
    async def sleep_until(self, ms: int) -> None: self.advance_to(ms)
    def timeout(self, ms: int) -> int: return ms

class TapeBarSource:
    def __init__(self, bars: list[T.NormalizedBar], tf: str, clock: TapeClock):
        self.bars, self.tf, self.clock, self._tf_ms = list(bars), tf, clock, tf_ms(tf)
    async def history(self, instrument, tf, start_ms, end_ms):
        sel = [b for b in self.bars if start_ms <= b.ts_open < end_ms]
        complete = all(b.ts_open + self._tf_ms == n.ts_open for b, n in zip(sel, sel[1:]))
        return sel, complete
    async def events(self, instrument, tf) -> AsyncIterator[T.BarEvent]:
        prev = None
        for b in self.bars:
            if prev is not None and b.ts_open != prev.ts_open + self._tf_ms:
                yield T.Gap(prev.ts_open + self._tf_ms, b.ts_open)
            self.clock.advance_to(b.ts_open)
            yield T.Forming(T.NormalizedBar(b.ts_open, b.o, b.o, b.o, b.o, 0.0, 0, is_forming=True))
            self.clock.advance_to(b.ts_open + self._tf_ms)
            yield T.Confirmed(b)
            prev = b

def _path_prices(b: T.NormalizedBar, reversed_: bool) -> list[float]:
    high_first = abs(b.h - b.o) <= abs(b.o - b.l)          # spec §0: ties -> high-first
    near, far = (b.h, b.l) if high_first else (b.l, b.h)
    return [b.o, far, near, b.c] if reversed_ else [b.o, near, far, b.c]

def _interp(points: list[float], n: int) -> list[float]:
    out = []
    segs = len(points) - 1
    for i in range(n):
        x = i / (n - 1) * segs
        k = min(int(x), segs - 1); f = x - k
        out.append(points[k] + (points[k + 1] - points[k]) * f)
    return out

class TapeTickSource:
    def __init__(self, bars: list[T.NormalizedBar], tf: str, policy: str, seed: int = 0,
                 ticks_csv: str | Path | None = None):
        if policy not in TICK_POLICIES:
            raise ValueError(f"unknown tick policy {policy}; one of {TICK_POLICIES}")
        self.bars, self.tf, self.policy, self.seed, self.ticks_csv = list(bars), tf, policy, seed, ticks_csv
        self._tf_ms = tf_ms(tf)
    def _prints(self, b: T.NormalizedBar, rng: random.Random) -> list[tuple[float, float]]:
        """[(fraction of bucket, price)] for one bar under the policy."""
        if self.policy == "path4":
            return list(zip(PROBE_POINT_FRACTIONS, _path_prices(b, False)))
        if self.policy == "path4-reversed":
            return list(zip(PROBE_POINT_FRACTIONS, _path_prices(b, True)))
        if self.policy == "dense":
            prices = _interp(_path_prices(b, False), 16)
            return [((i + 1) / 17, p) for i, p in enumerate(prices)]
        if self.policy == "random-ohlc":
            n = 12; mid = [rng.uniform(b.l, b.h) for _ in range(n - 4)]
            high_first = abs(b.h - b.o) <= abs(b.o - b.l)
            ext = [b.h, b.l] if high_first else [b.l, b.h]
            prices = [b.o] + mid[: (n - 4) // 2] + [ext[0]] + mid[(n - 4) // 2:] + [ext[1], b.c]
            return [((i + 1) / (len(prices) + 1), p) for i, p in enumerate(prices)]
        raise AssertionError(self.policy)
    async def subscribe(self, instrument, from_seq: int) -> AsyncIterator[T.TickEvent]:
        seq = 0
        if self.policy == "real":
            with Path(self.ticks_csv).open() as fh:
                for r in csv.DictReader(fh):
                    seq = int(r["seq"])
                    if seq < from_seq: continue
                    yield T.Tick(T.NormalizedTick(int(r["ts"]), seq, float(r["price"]), float(r["qty"])))
            return
        rng = random.Random(self.seed)
        for b in self.bars:
            prints = self._prints(b, rng)
            qty = b.v / len(prints) if prints else 0.0
            for frac, price in prints:
                seq += 1
                if seq < from_seq: continue
                yield T.Tick(T.NormalizedTick(b.ts_open + int(frac * self._tf_ms), seq, price, qty))
