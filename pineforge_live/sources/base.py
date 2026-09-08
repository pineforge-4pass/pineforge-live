"""Generic feed contract. No exchange endpoints, subscriptions or account state."""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pineforge_live import types as T
from pineforge_live.bars.policy import bucket_start, tf_ms
from pineforge_live.bars.calendar import ParentWindows

MAX_FRAME_BYTES = 1_048_576


class SourceError(ValueError):
    """A malformed or unavailable feed; details never include URL credentials/payloads."""


def validate_url(url, *, schemes=('https', 'http'), allow_insecure=False):
    if not isinstance(allow_insecure, bool) or not isinstance(url, str) or not url or any(ord(c) < 33 for c in url):
        raise SourceError('source URL: invalid URL')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise SourceError('source URL: invalid URL') from None
    if parts.scheme not in schemes or not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment:
        raise SourceError('source URL: unsupported scheme, credentials, host or fragment')
    if port is not None and not 1 <= port <= 65535:
        raise SourceError('source URL: invalid port')
    local = parts.hostname.lower() in {'localhost', '127.0.0.1', '::1'}
    if parts.scheme in {'http', 'ws'} and not (local or allow_insecure):
        raise SourceError('source URL: plaintext requires local host or explicit insecure opt-in')
    return url


def number(value, name, *, integer=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise SourceError(name + ': expected a finite numeric value')
    try:
        ok = math.isfinite(value) and (not nonnegative or value >= 0)
    except OverflowError:
        ok = False
    if not ok or (integer and value > 2**63 - 1):
        raise SourceError(name + ': numeric bound violated')
    return value


@dataclass(frozen=True)
class SourceConfig:
    kind: str
    path: Path | None = None
    url: str | None = None
    poll_interval_ms: int = 1_000
    reconnect_initial_ms: int = 1_000
    reconnect_max_ms: int = 30_000
    timeout_ms: int = 10_000
    allow_insecure: bool = False

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in {'stdin', 'jsonl', 'websocket', 'http'}:
            raise SourceError('source.kind: expected stdin, jsonl, websocket or http')
        if not isinstance(self.allow_insecure, bool):
            raise SourceError('source.allow_insecure: expected boolean')
        for key in ('poll_interval_ms', 'reconnect_initial_ms', 'reconnect_max_ms', 'timeout_ms'):
            number(getattr(self, key), 'source.' + key, integer=True, nonnegative=True)
            if getattr(self, key) == 0:
                raise SourceError('source.' + key + ': must be positive')
        if self.reconnect_initial_ms > self.reconnect_max_ms:
            raise SourceError('source.reconnect_initial_ms: exceeds maximum backoff')
        if self.kind == 'jsonl':
            if self.path is None or not Path(self.path).is_file() or self.url is not None:
                raise SourceError('source.path: jsonl requires an existing file and no URL')
            object.__setattr__(self, 'path', Path(self.path).resolve())
        elif self.kind == 'stdin':
            if self.path is not None or self.url is not None:
                raise SourceError('source: stdin accepts neither path nor URL')
        else:
            if self.path is not None:
                raise SourceError('source.path: network source does not accept a path')
            validate_url(self.url, schemes=('wss', 'ws') if self.kind == 'websocket' else ('https', 'http'),
                         allow_insecure=self.allow_insecure)


def _object(value, name, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise SourceError(name + ': missing or unknown fields')
    return value


def parse_bar(value, script_tf, *, forming=False):
    bar = _object(value, 'bar', ('ts_open', 'o', 'h', 'l', 'c', 'v'), ('trade_count', 'is_forming', 'synthesized'))
    stamp = number(bar['ts_open'], 'bar.ts_open', integer=True, nonnegative=True)
    for key in ('o', 'h', 'l', 'c', 'v'):
        number(bar[key], 'bar.' + key, nonnegative=key == 'v')
    if bar['h'] < max(bar['o'], bar['l'], bar['c']) or bar['l'] > min(bar['o'], bar['h'], bar['c']):
        raise SourceError('bar: inconsistent OHLC range')
    count = number(bar.get('trade_count', 0), 'bar.trade_count', integer=True, nonnegative=True)
    for key in ('is_forming', 'synthesized'):
        if key in bar and not isinstance(bar[key], bool):
            raise SourceError('bar.' + key + ': expected boolean')
    if 'is_forming' in bar and bar['is_forming'] != forming:
        raise SourceError('bar.is_forming: contradicts event type')
    try:
        aligned = bucket_start(stamp, script_tf) == stamp
    except (ValueError, TypeError):
        raise SourceError('script_tf: unsupported timeframe') from None
    if not aligned:
        raise SourceError('bar.ts_open: must align to script timeframe')
    return T.NormalizedBar(stamp, *(float(bar[key]) for key in ('o', 'h', 'l', 'c', 'v')),
                           count, is_forming=forming, synthesized=bar.get('synthesized', False))


def parse_event(value, script_tf):
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise SourceError('event.type: required string')
    if value['type'] == 'tick':
        _object(value, 'tick event', ('type', 'ts', 'seq', 'price', 'qty'))
        for key in ('ts', 'seq', 'price', 'qty'):
            number(value[key], 'tick.' + key, integer=key in {'ts', 'seq'}, nonnegative=key != 'price')
        return T.Tick(T.NormalizedTick(value['ts'], value['seq'], float(value['price']), float(value['qty'])))
    if value['type'] in {'bar', 'forming'}:
        _object(value, 'bar event', ('type', 'bar'))
        forming = value['type'] == 'forming'
        bar = parse_bar(value['bar'], script_tf, forming=forming)
        return T.Forming(bar) if forming else T.Confirmed(bar)
    raise SourceError('event.type: unsupported event')


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SourceError('feed JSON: duplicate key')
        value[key] = item
    return value


def parse_frame(frame, script_tf):
    if not isinstance(frame, (str, bytes)):
        raise SourceError('feed frame: expected text or bytes')
    if len(frame.encode('utf-8') if isinstance(frame, str) else frame) > MAX_FRAME_BYTES:
        raise SourceError('feed frame: exceeds one MiB limit')
    try:
        value = json.loads(frame, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(SourceError('feed JSON: nonfinite number')))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError('feed frame: invalid JSON') from None
    rows = value if isinstance(value, list) else [value]
    return tuple(parse_event(row, script_tf) for row in rows)


def load_history(path, script_tf, *, parent_windows=None):
    """Read the existing six-column CSV contract, validating range and continuity."""
    try:
        width = tf_ms(script_tf)
        calendar=ParentWindows(parent_windows) if parent_windows is not None else None
        with Path(path).open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            expected = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            if reader.fieldnames != expected:
                raise SourceError('history: expected timestamp,open,high,low,close,volume CSV header')
            result = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise SourceError('history: malformed CSV row')
                parsed = {'ts_open': int(row['timestamp']), **{k: float(row[col]) for k, col in
                          zip(('o', 'h', 'l', 'c', 'v'), expected[1:])}}
                bar = parse_bar(parsed, '1' if calendar else script_tf)
                if calendar and (len(result)>=len(calendar.windows) or bar.ts_open!=calendar.windows[len(result)][0]):
                    raise SourceError('history: bars must match parent window schedule prefix')
                if not calendar and result and bar.ts_open != result[-1].ts_open + width:
                    raise SourceError('history: bars must be increasing and contiguous')
                result.append(bar)
            if not result:
                raise SourceError('history: at least one confirmed bar is required')
            return result
    except SourceError:
        raise
    except (OSError, UnicodeError, ValueError, csv.Error):
        raise SourceError('history: unreadable or invalid CSV') from None


class SequenceTracker:
    """Local duplicate filtering only. It does not assert remote replay coverage."""
    def __init__(self, from_seq=None):
        if from_seq is not None:
            number(from_seq, 'from_seq', integer=True, nonnegative=True)
        self.last_seq = from_seq
        self.last_tick = None

    def accept(self, event):
        if not isinstance(event, T.Tick):
            return (event,)
        tick = event.tick
        if self.last_seq is not None and tick.seq <= self.last_seq:
            if tick.seq == self.last_seq and self.last_tick is not None and tick != self.last_tick:
                raise SourceError('tick.seq: conflicting duplicate')
            return ()
        gap = ()
        if self.last_seq is not None and tick.seq > self.last_seq + 1:
            gap = (T.TickGap(self.last_seq + 1, tick.seq - 1, False),)
        self.last_seq = tick.seq
        self.last_tick = tick
        return gap + (event,)
