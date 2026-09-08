"""Minute input adapter shared by direct candles and mock/real trade streams.

In tick mode every minute ends with a confirmed 1m boundary. Nonempty
boundaries verify the ticks' OHLCV; the aggregation consumes the reconstructed
ticks, not a replacement parent candle. Empty boundaries carry quote metadata
without inventing trades. Parent bars close at the final constituent minute.
"""
from __future__ import annotations

from dataclasses import asdict, replace

from pineforge_live import types as T
from .builder import compare_bar
from .minute import MINUTE_MS, MinuteBarAggregator, _integer, _number, validate_minute


class MinuteStream:
    def __init__(self, script_tf: str, *, mode: str = 'mixed', parent_windows=None):
        if mode not in ('mixed','ticks','bars'):
            raise ValueError('minute stream mode must be mixed, ticks or bars')
        self.mode = mode
        self.aggregator = MinuteBarAggregator(script_tf,parent_windows=parent_windows)
        self.pending: T.NormalizedBar | None = None
        self.last_input: T.NormalizedBar | None = None

    def forming(self):
        if self.pending is None:
            return self.aggregator.forming()
        # A private copy previews the partial minute without committing it or
        # prematurely confirming the parent on the last minute's first tick.
        preview = self.aggregator.clone()
        closed = preview.push(self.pending)
        return replace(closed[0], is_forming=True) if closed else preview.forming()

    def push(self, event: T.Tick | T.Confirmed) -> list[T.BarEvent]:
        if isinstance(event, T.Tick):
            if self.mode == 'bars':
                raise ValueError('bars input mode refuses ticks')
            tick = event.tick
            _integer(tick.ts, 'tick.ts', maximum=2**63 - 1 - MINUTE_MS)
            _integer(tick.seq, 'tick.seq')
            _number(tick.price, 'tick.price')
            _number(tick.qty, 'tick.qty')
            if tick.qty <= 0:
                raise ValueError('minute stream ticks require positive quantity; use an empty-minute boundary')
            stamp = tick.ts - tick.ts % MINUTE_MS
            old = self.pending
            if old is not None and old.ts_open != stamp:
                raise ValueError('confirmed minute boundary required before ticks of another minute')
            if old is None:
                pending = T.NormalizedBar(stamp,tick.price,tick.price,tick.price,tick.price,tick.qty,1)
            else:
                pending = T.NormalizedBar(stamp,old.o,max(old.h,tick.price),min(old.l,tick.price),
                                          tick.price,_number(old.v+tick.qty,'minute volume'),old.trade_count+1)
            self.pending = pending
            try:
                forming = self.forming()
            except Exception:
                self.pending = old
                raise
            return [T.Forming(forming)]
        if not isinstance(event, T.Confirmed):
            raise ValueError('minute input accepts ticks and confirmed 1m boundaries only')
        bar = event.bar
        validate_minute(bar)
        if self.last_input is not None and bar.ts_open == self.last_input.ts_open:
            if bar != self.last_input:
                raise ValueError('changed duplicate input minute')
            return []
        folded = bar
        if self.mode == 'ticks' and self.pending is None and bar.v > 0:
            raise ValueError('ticks input mode requires trades for every positive-volume minute')
        if self.pending is not None:
            if compare_bar(self.pending,bar):
                raise ValueError('tick-built minute disagrees with confirmed minute boundary')
            # Metadata can carry provenance, but cannot replace tick prices.
            folded = replace(self.pending,synthesized=bar.synthesized)
        closed = self.aggregator.push(folded)
        self.pending = None
        self.last_input = bar
        if closed:
            return [T.Confirmed(b) for b in closed]
        return [T.Forming(self.forming())]

    def export_state(self, *, compact=False):
        return {'version':1,'mode':self.mode,'aggregator':self.aggregator.export_state(compact=compact),
                'pending':asdict(self.pending) if self.pending else None,
                'last_input':asdict(self.last_input) if self.last_input else None}

    @classmethod
    def from_state(cls,state, *, parent_windows=None):
        if (not isinstance(state,dict) or set(state) != {'version','mode','aggregator','pending','last_input'}
                or type(state['version']) is not int or state['version'] != 1):
            raise ValueError('invalid minute stream checkpoint')
        aggregator = MinuteBarAggregator.from_state(state['aggregator'],parent_windows=parent_windows)
        result = cls(aggregator.script_tf,mode=state['mode'])
        result.aggregator = aggregator
        try:
            result.pending = T.NormalizedBar(**state['pending']) if state['pending'] else None
            result.last_input = T.NormalizedBar(**state['last_input']) if state['last_input'] else None
        except (TypeError,KeyError):
            raise ValueError('invalid minute stream checkpoint bars') from None
        if result.last_input is not None:
            validate_minute(result.last_input)
            if aggregator.last_minute is None or compare_bar(result.last_input,aggregator.last_minute):
                raise ValueError('minute stream checkpoint last-input mismatch')
        elif aggregator.last_minute is not None:
            raise ValueError('minute stream checkpoint missing last input')
        if result.pending is not None:
            if result.mode == 'bars':
                raise ValueError('bars-mode checkpoint cannot contain pending ticks')
            validate_minute(result.pending)
            if result.pending.v <= 0 or result.pending.trade_count == 0:
                raise ValueError('minute stream checkpoint has invalid pending ticks')
            result.forming()  # Checks boundary/continuity without mutation.
        return result
