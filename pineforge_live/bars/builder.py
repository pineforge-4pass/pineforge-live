# pineforge_live/bars/builder.py
from __future__ import annotations
import struct
from dataclasses import replace
from pineforge_live import types as T
from .policy import bucket_start, tf_ms

VOLUME_TOL = 1e-6
_FNV_OFFSET, _FNV_PRIME, _MASK = 0xcbf29ce484222325, 0x100000001b3, (1 << 64) - 1

def carry_forward(prev: T.NormalizedBar, ts_open: int) -> T.NormalizedBar:
    return T.NormalizedBar(ts_open, prev.c, prev.c, prev.c, prev.c, 0.0, 0, is_forming=False, synthesized=True)

class FormingBarBuilder:
    """Builds script-TF bars from ticks: open = first print, H/L/C/V/trade_count fold
    every print; a skipped bucket becomes a carry-forward zero-volume bar."""
    def __init__(self, tf: str):
        self.tf = tf; self._tf_ms = tf_ms(tf); self._cur: T.NormalizedBar | None = None; self._last_closed: T.NormalizedBar | None = None
    def forming(self) -> T.NormalizedBar | None:
        return self._cur
    def push(self, t: T.NormalizedTick) -> list[T.NormalizedBar]:
        start = bucket_start(t.ts, self.tf)
        out: list[T.NormalizedBar] = []
        if self._cur is None:
            self._cur = T.NormalizedBar(start, t.price, t.price, t.price, t.price, t.qty, 1, is_forming=True)
            return out
        if start < self._cur.ts_open:
            raise ValueError(f"tick {t.ts} precedes forming bar {self._cur.ts_open}")
        while start > self._cur.ts_open:
            closed = replace(self._cur, is_forming=False); out.append(closed); self._last_closed = closed
            nxt = self._cur.ts_open + self._tf_ms
            if nxt == start:
                self._cur = T.NormalizedBar(start, t.price, t.price, t.price, t.price, t.qty, 1, is_forming=True)
                return out
            self._cur = replace(carry_forward(closed, nxt), is_forming=True)
        c = self._cur
        self._cur = T.NormalizedBar(c.ts_open, c.o, max(c.h, t.price), min(c.l, t.price), t.price, c.v + t.qty,
                                    c.trade_count + 1, is_forming=True, synthesized=False)
        return out

def compare_bar(a: T.NormalizedBar, b: T.NormalizedBar) -> list[str]:
    diff = [k for k in ("ts_open", "o", "h", "l", "c") if getattr(a, k) != getattr(b, k)]
    if abs(a.v - b.v) > VOLUME_TOL:
        diff.append("v")
    return diff

def bars_hash(prev: int, bar: T.NormalizedBar) -> int:
    h = prev if prev else _FNV_OFFSET
    payload = struct.pack("<qddddd?", bar.ts_open, bar.o, bar.h, bar.l, bar.c, bar.v, bar.synthesized)
    for byte in payload:
        h ^= byte; h = (h * _FNV_PRIME) & _MASK
    return h

def bars_hash_all(bars) -> int:
    h = 0
    for b in bars:
        h = bars_hash(h, b)
    return h
