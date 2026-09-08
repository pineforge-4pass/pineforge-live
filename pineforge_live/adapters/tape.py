"""Tape adapters (spec §8): bars and ticks from recorded feeds; a clock the tape drives."""
from __future__ import annotations
import csv, random
from pathlib import Path
from typing import AsyncIterator
from pineforge_live import types as T
from pineforge_live.bars.policy import tf_ms

PROBE_POINT_FRACTIONS = (1 / 15, 5 / 15, 10 / 15, 14 / 15)   # spec §10.2: 1/5/10/14 minutes of a 15m bar
TICK_POLICIES = ("real", "path4", "path4-reversed", "dense", "random-ohlc")

def probe_point_offsets_ms(tf: str) -> tuple[int, int, int, int]:
    """The four probe-point offsets, in ms from the bucket's open, for
    timeframe `tf`: `int(frac * tf_ms(tf))` for each of
    PROBE_POINT_FRACTIONS. Spec §10.2 defines k=4 only for the 15m live
    triple ("after 1/5/10/14 minutes from the 1m sub-bars") -- this
    proportional generalisation to other timeframes is a defensible
    reading, not spec-pinned wording, for any `tf` other than "15"."""
    ms = tf_ms(tf)
    return tuple(int(f * ms) for f in PROBE_POINT_FRACTIONS)

def load_feed_csv(path: str | Path) -> list[T.NormalizedBar]:
    """Read a `timestamp,open,high,low,close,volume` CSV (the derived-feed
    export format) into NormalizedBars; `trade_count` is always 0 (a
    derived feed carries no per-bar trade count)."""
    out = []
    with Path(path).open() as fh:
        for r in csv.DictReader(fh):
            out.append(T.NormalizedBar(int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]),
                                       float(r["close"]), float(r["volume"]), 0))
    return out

class TapeClock:
    """A Clock (spec §2a protocol) driven entirely by the tape it plays
    alongside: `now_ms`/`venue_now_ms` only ever advance to timestamps the
    tape hands it (never rewind, `skew_ms()` is always 0), and
    `sleep_until` advances the clock immediately instead of actually
    blocking."""
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
    """A BarSource (spec §2a protocol) replaying a fixed, in-memory list of
    confirmed bars: `events()` yields a Forming (open-only) print then the
    Confirmed bar for each, advancing `clock` to each timestamp in turn,
    with a Gap between two bars whenever the next one's `ts_open` isn't
    exactly one bucket after the previous one's. `bars` must be strictly
    increasing by `ts_open` -- an out-of-order or duplicated bar is a
    recording/export bug, not a gap to report, and is rejected at
    construction rather than turned into a nonsense Gap."""
    def __init__(self, bars: list[T.NormalizedBar], tf: str, clock: TapeClock):
        for prev, cur in zip(bars, bars[1:]):
            if cur.ts_open <= prev.ts_open:
                raise ValueError(f"tape not strictly increasing at ts_open={cur.ts_open}")
        self.bars, self.tf, self.clock, self._tf_ms = list(bars), tf, clock, tf_ms(tf)
    async def history(self, instrument, tf, start_ms, end_ms):
        """Bars with `ts_open in [start_ms, end_ms)`, plus whether that
        selection is internally contiguous (no missing bucket between
        consecutive selected bars) -- a hole outside the selection, and an
        empty selection, both report complete=True: this checks internal
        contiguity only, not boundary coverage."""
        if tf != self.tf:
            raise ValueError(f"history() tf {tf!r} != tape tf {self.tf!r}")
        sel = [b for b in self.bars if start_ms <= b.ts_open < end_ms]
        complete = all(b.ts_open + self._tf_ms == n.ts_open for b, n in zip(sel, sel[1:]))
        return sel, complete
    async def events(self, instrument, tf) -> AsyncIterator[T.BarEvent]:
        """Forming, then Confirmed, for every bar in order (with a Gap
        event between two bars that aren't exactly one bucket apart),
        advancing `self.clock` to each bar's open and then its close."""
        if tf != self.tf:
            raise ValueError(f"events() tf {tf!r} != tape tf {self.tf!r}")
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
    """The four-point O / near-extreme / far-extreme / C path spec §0
    defines for one bar; a tie (|H-O| == |O-L|) resolves high-first."""
    high_first = abs(b.h - b.o) <= abs(b.o - b.l)          # spec §0: ties -> high-first
    near, far = (b.h, b.l) if high_first else (b.l, b.h)
    return [b.o, far, near, b.c] if reversed_ else [b.o, near, far, b.c]

def _interp(points: list[float], n: int) -> list[float]:
    """`n` values interpolated along the polyline through `points`,
    landing exactly on each input point at its evenly-spaced index
    (endpoint exactness relies on `a + (b-a) == b`, exact under Sterbenz
    for prices within 2x of each other -- true for any real bar)."""
    out = []
    segs = len(points) - 1
    for i in range(n):
        x = i / (n - 1) * segs
        k = min(int(x), segs - 1); f = x - k
        out.append(points[k] + (points[k + 1] - points[k]) * f)
    return out

class TapeTickSource:
    """A TickSource (spec §2a protocol) synthesizing ticks from tape bars
    under one of TICK_POLICIES: `path4`/`path4-reversed` print exactly at
    `probe_point_offsets_ms(tf)`, `dense` interpolates 16 prints along the
    same O/near/far/C path, `random-ohlc` draws a seeded pseudo-random walk
    within [L,H] that starts at O and ends at C, and `real` replays an
    actual `ts,seq,price,qty` tick CSV (`ticks_csv`, required for this
    policy). Each `subscribe()` call on a synthetic policy uses its own
    `random.Random(self.seed)`, so repeated subscriptions are identical."""
    def __init__(self, bars: list[T.NormalizedBar], tf: str, policy: str, seed: int = 0,
                 ticks_csv: str | Path | None = None):
        if policy not in TICK_POLICIES:
            raise ValueError(f"unknown tick policy {policy}; one of {TICK_POLICIES}")
        if policy == "real" and ticks_csv is None:
            raise ValueError("real tick policy requires ticks_csv")
        self.bars, self.tf, self.policy, self.seed, self.ticks_csv = list(bars), tf, policy, seed, ticks_csv
        self._tf_ms = tf_ms(tf)
    def _prints(self, b: T.NormalizedBar, rng: random.Random) -> list[tuple[int, float]]:
        """[(offset_ms from bucket open, price)] for one bar under the policy."""
        if self.policy == "path4":
            return list(zip(probe_point_offsets_ms(self.tf), _path_prices(b, False)))
        if self.policy == "path4-reversed":
            return list(zip(probe_point_offsets_ms(self.tf), _path_prices(b, True)))
        if self.policy == "dense":
            prices = _interp(_path_prices(b, False), 16)
            return [(int((i + 1) / 17 * self._tf_ms), p) for i, p in enumerate(prices)]
        if self.policy == "random-ohlc":
            n = 12; mid = [rng.uniform(b.l, b.h) for _ in range(n - 4)]
            high_first = abs(b.h - b.o) <= abs(b.o - b.l)
            ext = [b.h, b.l] if high_first else [b.l, b.h]
            prices = [b.o] + mid[: (n - 4) // 2] + [ext[0]] + mid[(n - 4) // 2:] + [ext[1], b.c]
            return [(int((i + 1) / (len(prices) + 1) * self._tf_ms), p) for i, p in enumerate(prices)]
        raise AssertionError(self.policy)
    async def subscribe(self, instrument, from_seq: int) -> AsyncIterator[T.TickEvent]:
        """Ticks from `from_seq` onward. `real` replays the CSV verbatim
        and emits a `TickGap(from_seq, to_seq, healed=False)` whenever the
        file's own seq jumps by more than 1 at or after `from_seq` (a hole
        entirely below `from_seq` is not this subscriber's concern).
        Synthetic policies rebuild the tape bar-by-bar with a fresh
        `random.Random(self.seed)` so two subscriptions are identical."""
        seq = 0
        if self.policy == "real":
            with Path(self.ticks_csv).open() as fh:
                prev_seq = None
                for r in csv.DictReader(fh):
                    seq = int(r["seq"])
                    if prev_seq is not None and seq - prev_seq > 1 and seq - 1 >= from_seq:
                        yield T.TickGap(prev_seq, seq, healed=False)
                    prev_seq = seq
                    if seq < from_seq: continue
                    yield T.Tick(T.NormalizedTick(int(r["ts"]), seq, float(r["price"]), float(r["qty"])))
            return
        rng = random.Random(self.seed)
        for b in self.bars:
            prints = self._prints(b, rng)
            qty = b.v / len(prints) if prints else 0.0
            for offset_ms, price in prints:
                seq += 1
                if seq < from_seq: continue
                yield T.Tick(T.NormalizedTick(b.ts_open + offset_ms, seq, price, qty))
