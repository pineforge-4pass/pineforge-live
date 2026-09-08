"""Price-independent finite schedules for exchange/session parent candles."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pineforge_live.types import canonical_sha256


def normalize_windows(windows):
    if windows is None:
        return None
    if not isinstance(windows,(list,tuple)) or not windows or len(windows)>1_000_000:
        raise ValueError('parent_windows: expected 1 to 1000000 windows')
    normalized=[]
    for row in windows:
        if isinstance(row,dict):
            if not {'open_ms','close_ms'}<=set(row) or set(row)-{'open_ms','close_ms','first_minute_ms'}:
                raise ValueError('parent_windows: expected open_ms/close_ms and optional first_minute_ms')
            start,end=row['open_ms'],row['close_ms']
            first=row.get('first_minute_ms',start)
        elif isinstance(row,(list,tuple)) and len(row) in (2,3):
            start,end=row[:2]
            first=row[2] if len(row)==3 else start
        else:raise ValueError('parent_windows: invalid window')
        if any(type(value) is not int or not 0<=value<=2**63-1 or value%60_000 for value in (start,end,first)):
            raise ValueError('parent_windows: bounds must be nonnegative minute-aligned UTC milliseconds')
        if not start<=first<end or (normalized and start<normalized[-1][1]):
            raise ValueError('parent_windows: windows must have positive duration, be ordered and nonoverlapping')
        # Two-item records retain their original identity when the chart
        # label and first accepted input minute coincide.
        normalized.append((start,end) if first==start else (start,end,first))
    return tuple(normalized)


@dataclass(frozen=True,init=False)
class ParentWindows:
    records: tuple
    windows: tuple[tuple[int,int], ...]
    opens: tuple[int, ...]
    first_minutes: tuple[int, ...]
    sha256: str

    def __init__(self,windows):
        if isinstance(windows,ParentWindows):
            object.__setattr__(self,'records',windows.records)
            object.__setattr__(self,'windows',windows.windows)
            object.__setattr__(self,'opens',windows.opens)
            object.__setattr__(self,'first_minutes',windows.first_minutes)
            object.__setattr__(self,'sha256',windows.sha256)
            return
        object.__setattr__(self,'records',normalize_windows(windows))
        if self.records is None:
            raise ValueError('parent_windows: schedule required')
        object.__setattr__(self,'windows',tuple(row[:2] for row in self.records))
        object.__setattr__(self,'opens',tuple(row[0] for row in self.windows))
        object.__setattr__(self,'first_minutes',tuple(row[2] if len(row)==3 else row[0] for row in self.records))
        object.__setattr__(self,'sha256',canonical_sha256(self.records))

    def containing(self,ts):
        index=bisect_right(self.opens,ts)-1
        if index<0 or ts<self.first_minutes[index] or ts>=self.windows[index][1]:
            raise ValueError('timestamp outside supplied parent windows')
        return self.windows[index]

    def index(self,open_ms):
        index=bisect_right(self.opens,open_ms)-1
        if index<0 or self.opens[index]!=open_ms:
            raise ValueError('bar timestamp absent from parent windows')
        return index

    def next_open(self,open_ms):
        index=self.index(open_ms)+1
        if index>=len(self.windows):
            raise ValueError('parent window schedule exhausted')
        return self.windows[index][0]

    def close(self,open_ms):
        return self.windows[self.index(open_ms)][1]

    def first_minute(self,open_ms):
        return self.first_minutes[self.index(open_ms)]

    def next_minute(self,minute_open):
        start,end=self.containing(minute_open)
        if minute_open+60_000<end:return minute_open+60_000
        return self.first_minute(self.next_open(start))

    def validate_prefix(self,bars):
        if len(bars)>len(self.windows) or any(b.ts_open!=self.windows[i][0] for i,b in enumerate(bars)):
            raise ValueError('history must match the parent window schedule prefix')
