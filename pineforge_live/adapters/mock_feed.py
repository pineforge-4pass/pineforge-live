"""Turn original 1m CSV rows into public runtime events without an engine."""
from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path

from pineforge_live.bars.calendar import ParentWindows
from pineforge_live.bars.minute import MINUTE_MS,_integer
from pineforge_live.sources.base import SourceError,parse_bar
from .synthetic import SyntheticMinuteTicks


def iter_minutes(path: str | Path, *, start_ms=None, end_ms=None, parent_windows=None,gap_policy='reject'):
    """Read original OHLCV rows; selected input must have complete 1m coverage.

    Bounds are inclusive start, exclusive end. A calendar permits gaps between
    windows; minutes inside each selected window remain contiguous. No OHLCV
    values are generated, normalized across sessions, or filled from parents.
    """
    if gap_policy not in ('reject','observed'):raise ValueError('mock gap policy must be reject or observed')
    for name,value in (('start_ms',start_ms),('end_ms',end_ms)):
        if value is not None:
            _integer(value,name)
            if value%MINUTE_MS:raise ValueError(name+' must align to a UTC minute')
    if start_ms is not None and end_ms is not None and end_ms<=start_ms:
        raise ValueError('end_ms must be greater than start_ms')
    calendar=(parent_windows if isinstance(parent_windows,ParentWindows)
              else ParentWindows(parent_windows) if parent_windows is not None else None)
    previous=None
    with Path(path).open(newline='',encoding='utf-8') as stream:
        reader=csv.DictReader(stream)
        header=['timestamp','open','high','low','close','volume']
        if reader.fieldnames!=header:raise SourceError('mock feed: expected timestamp,open,high,low,close,volume CSV header')
        for number,row in enumerate(reader,2):
            try:
                if None in row or any(value is None for value in row.values()):raise ValueError('malformed row')
                stamp=int(row['timestamp'])
                if start_ms is not None and stamp<start_ms:continue
                if end_ms is not None and stamp>=end_ms:break
                bar=parse_bar({'ts_open':stamp,**{key:float(row[column]) for key,column in
                              zip(('o','h','l','c','v'),header[1:])}},'1')
            except (ValueError,TypeError):
                raise SourceError(f'mock feed: invalid minute row {number}') from None
            if calendar is not None:calendar.containing(stamp)
            if previous is not None:
                expected=calendar.next_minute(previous.ts_open) if calendar else previous.ts_open+MINUTE_MS
                if stamp!=expected and not (gap_policy=='observed' and stamp>expected):
                    raise SourceError(f'mock feed: missing, repeated or regressed minute at row {number}')
            previous=bar
            yield bar
    if previous is None:raise SourceError('mock feed: selected range contains no minute rows')


def mock_events(path: str | Path, *, mode='ticks',policy='high-first',seed=0,start_seq=1,
                start_ms=None,end_ms=None,parent_windows=None,gap_policy='reject'):
    """Yield JSON-compatible public events for ``pineforge-live run``.

    Ticks mode emits O/H/L/C trades, then the original minute's confirmed
    boundary. Bars mode emits only that boundary. Empty minutes emit no ticks.
    """
    if mode not in ('ticks','bars'):raise ValueError('mock feed mode must be ticks or bars')
    calendar=(parent_windows if isinstance(parent_windows,ParentWindows)
              else ParentWindows(parent_windows) if parent_windows is not None else None)
    generator=SyntheticMinuteTicks(policy,seed=seed,start_seq=start_seq,parent_windows=calendar,gap_policy=gap_policy) if mode=='ticks' else None
    for bar in iter_minutes(path,start_ms=start_ms,end_ms=end_ms,parent_windows=calendar,gap_policy=gap_policy):
        if generator is not None:
            for tick in generator.push(bar).ticks:
                yield {'type':'tick','ts':tick.ts,'seq':tick.seq,'price':tick.price,'qty':tick.qty}
        yield {'type':'bar','bar':asdict(bar)}
