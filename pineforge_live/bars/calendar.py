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
            if set(row)!={'open_ms','close_ms'}:
                raise ValueError('parent_windows: expected open_ms/close_ms only')
            start,end=row['open_ms'],row['close_ms']
        elif isinstance(row,(list,tuple)) and len(row)==2:
            start,end=row
        else:raise ValueError('parent_windows: invalid window')
        if any(type(value) is not int or not 0<=value<=2**63-1 or value%60_000 for value in (start,end)):
            raise ValueError('parent_windows: bounds must be nonnegative minute-aligned UTC milliseconds')
        if end<=start or (normalized and start<normalized[-1][1]):
            raise ValueError('parent_windows: windows must have positive duration, be ordered and nonoverlapping')
        normalized.append((start,end))
    return tuple(normalized)


@dataclass(frozen=True,init=False)
class ParentWindows:
    windows: tuple[tuple[int,int], ...]
    opens: tuple[int, ...]
    sha256: str

    def __init__(self,windows):
        if isinstance(windows,ParentWindows):
            object.__setattr__(self,'windows',windows.windows)
            object.__setattr__(self,'opens',windows.opens)
            object.__setattr__(self,'sha256',windows.sha256)
            return
        object.__setattr__(self,'windows',normalize_windows(windows))
        if self.windows is None:
            raise ValueError('parent_windows: schedule required')
        object.__setattr__(self,'opens',tuple(row[0] for row in self.windows))
        object.__setattr__(self,'sha256',canonical_sha256(self.windows))

    def containing(self,ts):
        index=bisect_right(self.opens,ts)-1
        if index<0 or ts>=self.windows[index][1]:
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

    def next_minute(self,minute_open):
        start,end=self.containing(minute_open)
        return minute_open+60_000 if minute_open+60_000<end else self.next_open(start)

    def validate_prefix(self,bars):
        if len(bars)>len(self.windows) or any(b.ts_open!=self.windows[i][0] for i,b in enumerate(bars)):
            raise ValueError('history must match the parent window schedule prefix')
