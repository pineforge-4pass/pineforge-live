"""Confirmed minute bars aggregated with the campaign chart-feed rule.

Open is the first positive-volume minute's open (the first minute's open
when the whole bucket is empty). High, low and close include *all* supplied
minutes, including zero-volume quotes. This is the rule documented by the
engine's ``scripts/derive_corpus_feeds.py``. Missing minutes are different
from supplied empty minutes and fail closed unless carry-forward is chosen.
"""
from __future__ import annotations

import math
import copy
from dataclasses import asdict, replace

from pineforge_live import types as T
from .policy import bucket_start, tf_ms
from .calendar import ParentWindows

MINUTE_MS = 60_000
MINUTE_POLICY_VERSION = "v1:first-positive-volume-open/all-minute-hlc"
_MAX_TS = 2**63 - 1


def _integer(value, name, *, maximum=_MAX_TS):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name}: expected bounded nonnegative integer")
    return value


def _number(value, name):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name}: expected finite number")
    return float(value)


def validate_minute(bar: T.NormalizedBar) -> None:
    """Validate a confirmed 1m row without modifying or rounding its prices."""
    if not isinstance(bar, T.NormalizedBar):
        raise ValueError("minute: expected NormalizedBar")
    _integer(bar.ts_open, "minute.ts_open", maximum=_MAX_TS - MINUTE_MS)
    if bar.ts_open % MINUTE_MS:
        raise ValueError("minute.ts_open: must align to UTC minute")
    for key in ("o", "h", "l", "c", "v"):
        _number(getattr(bar, key), "minute." + key)
    if bar.v < 0:
        raise ValueError("minute.v: negative volume")
    if bar.h < max(bar.o, bar.l, bar.c) or bar.l > min(bar.o, bar.h, bar.c):
        raise ValueError("minute: inconsistent OHLC range")
    _integer(bar.trade_count, "minute.trade_count")
    if type(bar.is_forming) is not bool or bar.is_forming:
        raise ValueError("minute: expected confirmed bar")
    if type(bar.synthesized) is not bool:
        raise ValueError("minute.synthesized: expected boolean")


class MinuteBarAggregator:
    """Stateful confirmed-1m → script-TF aggregation, for script TF > 1m.

    The first row must begin a script bucket. A trailing partial bucket is
    available through :meth:`forming`, never mislabeled confirmed. Replaying
    the latest identical row is idempotent; older rows and changed duplicates
    are refused. A durable caller can resume mid-bucket with ``from_state``.

    ``volume_decimals=6`` matches the corpus CSV export. ``None`` retains the
    unrounded sum. Missing-minute carry is opt-in and bounded per push.
    """

    def __init__(self, script_tf: str, *, gap_policy: str = "reject",
                 volume_decimals: int | None = 6, max_gap_minutes: int = 10_080,
                 parent_windows=None):
        self.script_tf = script_tf
        self._width = tf_ms(script_tf)
        if self._width <= MINUTE_MS or self._width % MINUTE_MS:
            raise ValueError("script_tf must be an integer multiple of 1m greater than 1m")
        if gap_policy not in ("reject", "carry-forward", "observed"):
            raise ValueError("gap_policy must be reject, carry-forward or observed")
        if volume_decimals is not None:
            _integer(volume_decimals, "volume_decimals", maximum=15)
        _integer(max_gap_minutes, "max_gap_minutes", maximum=1_000_000)
        self.gap_policy = gap_policy
        self.volume_decimals = volume_decimals
        self.max_gap_minutes = max_gap_minutes
        self.calendar=(parent_windows if isinstance(parent_windows,ParentWindows)
                       else ParentWindows(parent_windows) if parent_windows is not None else None)
        self._cur: T.NormalizedBar | None = None
        self._last: T.NormalizedBar | None = None
        self._has_positive_volume = False

    def _output(self, bar):
        return replace(bar, v=round(bar.v, self.volume_decimals)) if self.volume_decimals is not None else bar

    def forming(self) -> T.NormalizedBar | None:
        return self._output(self._cur) if self._cur is not None else None

    @property
    def last_minute(self) -> T.NormalizedBar | None:
        return self._last

    def push(self, bar: T.NormalizedBar) -> list[T.NormalizedBar]:
        validate_minute(bar)
        start,end=self.bounds(bar.ts_open)
        if end > _MAX_TS:
            raise ValueError("minute: script bucket exceeds timestamp bound")
        if self._last is None:
            first=self.calendar.first_minute(start) if self.calendar is not None else start
            if bar.ts_open != first and self.gap_policy!='observed':
                raise ValueError("first minute must begin script bucket; restore state for a partial bucket")
            missing = []
        else:
            if bar.ts_open == self._last.ts_open:
                if bar == self._last:
                    return []
                raise ValueError("changed duplicate minute")
            if bar.ts_open < self._last.ts_open:
                raise ValueError("minute timestamp regressed")
            expected=self._next_minute(self._last.ts_open)
            missing=[]
            if expected<bar.ts_open and self.gap_policy=='observed':
                previous_start,_=self.bounds(self._last.ts_open)
                expected_start,_=self.bounds(expected)
                if self._cur is not None and start!=previous_start:
                    raise ValueError('observed input cannot skip a parent closing minute')
                if self._cur is None and start!=expected_start:
                    raise ValueError('observed input cannot skip an entire parent')
                # Explicit sparse-input contract: fold only supplied rows.
                # No volume, price, quote, or auxiliary minute is fabricated.
                expected=bar.ts_open
            while expected<bar.ts_open:
                if self.gap_policy == "reject":
                    raise ValueError("minute gap; supply every confirmed minute or configure carry-forward")
                if len(missing)>=self.max_gap_minutes:
                    raise ValueError("minute gap exceeds max_gap_minutes")
                missing.append(expected)
                expected=self._next_minute(expected)
            if expected!=bar.ts_open:
                raise ValueError('minute does not follow supplied parent windows')
        before = self._cur, self._last, self._has_positive_volume
        closed = []
        try:
            for stamp in missing:
                previous = self._last
                empty = T.NormalizedBar(stamp, previous.c, previous.c,
                                        previous.c, previous.c, 0.0, 0, synthesized=True)
                self._fold(empty, closed)
            self._fold(bar, closed)
        except Exception:
            self._cur, self._last, self._has_positive_volume = before
            raise
        return closed

    def _fold(self, bar, closed):
        start,end = self.bounds(bar.ts_open)
        if self._cur is None:
            self._cur = replace(bar, ts_open=start, is_forming=True)
            self._has_positive_volume = bar.v > 0
        else:
            cur = self._cur
            volume = _number(cur.v + bar.v, "aggregated volume")
            count = _integer(cur.trade_count + bar.trade_count, "aggregated trade_count")
            self._cur = T.NormalizedBar(
                start, bar.o if not self._has_positive_volume and bar.v > 0 else cur.o,
                max(cur.h, bar.h), min(cur.l, bar.l), bar.c, volume, count,
                is_forming=True, synthesized=cur.synthesized and bar.synthesized,
            )
            self._has_positive_volume |= bar.v > 0
        self._last = bar
        if bar.ts_open + MINUTE_MS == end:
            closed.append(self._output(replace(self._cur, is_forming=False)))
            self._cur = None
            self._has_positive_volume = False

    def bounds(self,ts):
        if self.calendar is not None:
            return self.calendar.containing(ts)
        start=bucket_start(ts,self.script_tf)
        return start,start+self._width

    def _next_minute(self,ts):
        return self.calendar.next_minute(ts) if self.calendar is not None else ts+MINUTE_MS

    def clone(self):
        """Copy mutable aggregation cursors; immutable bars/calendar are shared."""
        return copy.copy(self)

    def export_state(self, *, compact=False) -> dict:
        """JSON-safe state, including unrounded volume and duplicate identity."""
        return {
            "version": MINUTE_POLICY_VERSION, "script_tf": self.script_tf,
            "gap_policy": self.gap_policy, "volume_decimals": self.volume_decimals,
            "max_gap_minutes": self.max_gap_minutes,
            "parent_windows": self.calendar.records if self.calendar is not None and not compact else None,
            "calendar_sha256": self.calendar.sha256 if self.calendar is not None else None,
            "forming": asdict(self._cur) if self._cur is not None else None,
            "last_minute": asdict(self._last) if self._last is not None else None,
            "has_positive_volume": self._has_positive_volume,
        }

    @classmethod
    def from_state(cls, state: dict, *, parent_windows=None) -> "MinuteBarAggregator":
        expected = {"version", "script_tf", "gap_policy", "volume_decimals", "max_gap_minutes",
                    "forming", "last_minute", "has_positive_volume", "parent_windows", "calendar_sha256"}
        if not isinstance(state, dict) or set(state) != expected or state["version"] != MINUTE_POLICY_VERSION:
            raise ValueError("invalid minute aggregation checkpoint")
        calendar=(parent_windows if isinstance(parent_windows,ParentWindows)
                  else ParentWindows(parent_windows) if parent_windows is not None else None)
        if state['parent_windows'] is not None:
            embedded=ParentWindows(state['parent_windows'])
            if calendar is not None and calendar.sha256!=embedded.sha256:
                raise ValueError('minute checkpoint supplied calendar mismatch')
            calendar=embedded
        if state['calendar_sha256']!=(calendar.sha256 if calendar is not None else None):
            raise ValueError('minute checkpoint calendar digest mismatch or missing schedule')
        result = cls(state["script_tf"], gap_policy=state["gap_policy"],
                     volume_decimals=state["volume_decimals"], max_gap_minutes=state["max_gap_minutes"],
                     parent_windows=calendar)
        try:
            cur = T.NormalizedBar(**state["forming"]) if state["forming"] is not None else None
            last = T.NormalizedBar(**state["last_minute"]) if state["last_minute"] is not None else None
        except (TypeError, KeyError):
            raise ValueError("invalid minute aggregation checkpoint bars") from None
        positive = state["has_positive_volume"]
        if type(positive) is not bool:
            raise ValueError("invalid checkpoint volume flag")
        if last is not None:
            validate_minute(last)
            start,end = result.bounds(last.ts_open)
            if end > _MAX_TS:
                raise ValueError("checkpoint script bucket exceeds timestamp bound")
            partial = last.ts_open + MINUTE_MS < end
            if partial != (cur is not None):
                raise ValueError("checkpoint partial-bucket mismatch")
        if cur is not None:
            validate_minute(replace(cur, is_forming=False))
            if (last is None or cur.is_forming is not True or cur.ts_open != start
                    or cur.c != last.c or cur.h < last.h or cur.l > last.l
                    or cur.v < last.v or cur.trade_count < last.trade_count
                    or positive != (cur.v > 0)
                    or (cur.synthesized and not last.synthesized)):
                raise ValueError("inconsistent minute aggregation checkpoint")
        elif positive:
            raise ValueError("checkpoint volume flag without forming bar")
        result._cur, result._last, result._has_positive_volume = cur, last, positive
        return result
