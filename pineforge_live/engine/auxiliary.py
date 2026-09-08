"""Immutable 1m warmup plus observed minutes for C++ request.security feeds."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import struct

from pineforge_live import types as T
from pineforge_live.bars.minute import validate_minute

_BAR=struct.Struct('=dddddq')


def pack_bar(bar):
    validate_minute(bar)
    return _BAR.pack(bar.o,bar.h,bar.l,bar.c,bar.v,bar.ts_open)


@dataclass(frozen=True)
class AuxiliaryHistory:
    sha256: str
    data: bytes
    count: int
    first_ms: int
    last_ms: int

    @classmethod
    def read(cls,path,sha256,*,start_ms=0):
        raw=Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha256:
            raise ValueError('auxiliary history changed after configuration was loaded')
        reader=csv.DictReader(io.StringIO(raw.decode('utf-8')))
        if reader.fieldnames!=['timestamp','open','high','low','close','volume']:
            raise ValueError('auxiliary history requires canonical 1m OHLCV columns')
        data=bytearray();previous=None;first=None;count=0
        for row in reader:
            if None in row or any(value is None for value in row.values()):raise ValueError('malformed auxiliary history row')
            bar=T.NormalizedBar(int(row['timestamp']),*(float(row[k]) for k in ('open','high','low','close','volume')),0)
            validate_minute(bar)
            if previous is not None and bar.ts_open<=previous:raise ValueError('auxiliary history timestamps must increase')
            previous=bar.ts_open
            if bar.ts_open<start_ms:continue
            if first is None:first=bar.ts_open
            data.extend(pack_bar(bar));count+=1
        if not count:raise ValueError('auxiliary history has no bars in the configured history range')
        return cls(sha256,bytes(data),count,first,previous)

    def with_observed(self,minutes):
        """Only already-observed minutes may extend the immutable warmup."""
        data=bytearray(self.data);last=self.last_ms;count=self.count
        for bar in minutes:
            validate_minute(bar)
            if bar.ts_open<=last:raise ValueError('observed auxiliary minutes overlap or regress')
            data.extend(pack_bar(bar));last=bar.ts_open;count+=1
        if count>2**31-1:raise ValueError('auxiliary feed exceeds C ABI length range')
        return data,count
