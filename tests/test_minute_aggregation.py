import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from pineforge_live import types as T
from pineforge_live.bars.minute import MinuteBarAggregator


def minute(index, *, o=100.0, h=103.0, l=98.0, c=101.0, v=1.0, synthesized=False):
    return T.NormalizedBar(index * 60_000, o, h, l, c, v, 0, synthesized=synthesized)


def test_closes_on_last_constituent_and_keeps_remainder_forming():
    agg = MinuteBarAggregator("3")
    assert agg.push(minute(0)) == []
    assert agg.push(minute(1, o=102, h=109, l=97, c=108, v=2)) == []
    done = agg.push(minute(2, c=99, v=4))
    assert len(done) == 1
    assert done[0].ohlcv() == (0, 100, 109, 97, 99, 7)
    assert done[0].is_forming is False
    assert agg.forming() is None
    assert agg.push(minute(3)) == []
    assert agg.forming().ts_open == 180_000
    assert agg.forming().is_forming is True


def test_zero_volume_prefix_keeps_extreme_but_open_is_first_positive():
    # The campaign derivation includes quotes from zero-volume rows in HLC.
    agg = MinuteBarAggregator("3")
    agg.push(minute(0, o=90, h=91, l=89, c=90, v=0))
    assert agg.forming().o == 90
    agg.push(minute(1, o=100, h=101, l=99, c=100, v=4))
    done = agg.push(minute(2, o=105, h=106, l=104, c=105, v=2))
    assert done[0].ohlcv() == (0, 100, 106, 89, 105, 6)


def test_later_empty_minute_preserves_quoted_hlc_without_resetting_open():
    agg = MinuteBarAggregator("3")
    agg.push(minute(0, o=110, h=111, l=109, c=110, v=0))
    agg.push(minute(1, o=100, h=101, l=99, c=100, v=4))
    done = agg.push(minute(2, o=95, h=96, l=94, c=95, v=0))
    assert done[0].ohlcv() == (0, 100, 111, 94, 95, 4)


def test_all_empty_bucket_uses_first_row_open_and_all_row_hlc():
    agg = MinuteBarAggregator("2")
    agg.push(minute(0, o=90, h=91, l=89, c=90, v=0))
    done = agg.push(minute(1, o=110, h=111, l=109, c=110, v=0))
    assert done[0].ohlcv() == (0, 90, 111, 89, 110, 0)


def test_missing_minutes_refused_without_mutation_and_carry_is_explicit():
    strict = MinuteBarAggregator("2")
    strict.push(minute(0))
    before = strict.export_state()
    with pytest.raises(ValueError, match="minute gap"):
        strict.push(minute(4))
    assert strict.export_state() == before
    agg = MinuteBarAggregator("2", gap_policy="carry-forward")
    agg.push(minute(0))
    done = agg.push(minute(4))
    assert len(done) == 2
    assert done[0].ohlcv() == (0, 100, 103, 98, 101, 1)
    assert done[1].ohlcv() == (120_000, 101, 101, 101, 101, 0)
    assert done[1].synthesized is True
    assert agg.forming().ts_open == 240_000


def test_gap_carry_preserves_empty_prefix_extreme_until_first_trade():
    agg = MinuteBarAggregator("2", gap_policy="carry-forward")
    agg.push(minute(0, o=90, h=90, l=90, c=90))
    agg.push(minute(1, o=90, h=90, l=90, c=90))
    done = agg.push(minute(3, o=100, h=103, l=98, c=101))
    assert done[0].ohlcv() == (120_000, 100, 103, 90, 101, 1)
    assert not done[0].synthesized


def test_duplicate_identity_and_regression_survive_restart():
    agg = MinuteBarAggregator("2")
    agg.push(minute(0))
    closed = agg.push(minute(1))
    agg = MinuteBarAggregator.from_state(json.loads(json.dumps(agg.export_state())))
    assert agg.push(minute(1)) == []
    assert closed and agg.forming() is None
    with pytest.raises(ValueError, match="changed duplicate"):
        agg.push(minute(1, v=2))
    with pytest.raises(ValueError, match="regressed"):
        agg.push(minute(0))


def test_restore_partial_keeps_first_positive_open_and_unrounded_volume():
    agg = MinuteBarAggregator("3")
    agg.push(minute(0, o=90, h=91, l=89, c=90, v=0))
    agg.push(minute(1, v=0.0000002))
    assert agg.forming().v == 0
    restored = MinuteBarAggregator.from_state(json.loads(json.dumps(agg.export_state())))
    final = minute(2, o=102, h=109, l=97, c=108, v=0.0000004)
    expected = agg.push(final)
    assert restored.push(final) == expected
    assert expected[0].o == 100
    assert expected[0].v == 0.000001


def test_unrounded_volume_option():
    agg = MinuteBarAggregator("2", volume_decimals=None)
    agg.push(minute(0, v=0.0000002))
    assert agg.push(minute(1, v=0.0000004))[0].v == pytest.approx(0.0000006, abs=1e-20)


@pytest.mark.parametrize("tf", ["1", "0", "1.5", "1m", "١٥", "1S", "24856D", 15, None])
def test_rejects_invalid_timeframes(tf):
    with pytest.raises(ValueError):
        MinuteBarAggregator(tf)


@pytest.mark.parametrize("change", [
    {"ts_open": 1}, {"ts_open": -60_000}, {"ts_open": True}, {"ts_open": 0.0},
    {"ts_open": 2**63}, {"v": -1}, {"h": float("inf")}, {"v": float("nan")},
    {"o": 110}, {"l": 104}, {"trade_count": -1}, {"trade_count": True},
    {"is_forming": True}, {"is_forming": 0}, {"synthesized": 1}, {"c": True},
])
def test_rejects_malformed_minutes(change):
    agg = MinuteBarAggregator("3")
    with pytest.raises(ValueError):
        agg.push(replace(minute(0), **change))
    assert agg.forming() is None


def test_first_partial_bucket_refused_and_weekly_anchor_is_monday():
    agg = MinuteBarAggregator("1W")
    monday = int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp() * 1000)
    with pytest.raises(ValueError, match="first minute"):
        agg.push(replace(minute(0), ts_open=monday + 2 * 86_400_000))
    agg.push(replace(minute(0), ts_open=monday))
    assert agg.forming().ts_open == monday
    # Explicit carry reaches Sunday's final minute and confirms the week.
    agg = MinuteBarAggregator.from_state({**agg.export_state(), "gap_policy": "carry-forward"})
    done = agg.push(replace(minute(0), ts_open=monday + 7 * 86_400_000 - 60_000))
    assert len(done) == 1 and done[0].ts_open == monday
    assert agg.forming() is None
    agg.push(replace(minute(0), ts_open=monday + 7 * 86_400_000))
    assert agg.forming().ts_open == monday + 7 * 86_400_000


def test_gap_limit_and_overflow_failure_leave_state_unchanged():
    agg = MinuteBarAggregator("3", gap_policy="carry-forward", max_gap_minutes=1)
    agg.push(minute(0, v=1e308))
    before = agg.export_state()
    with pytest.raises(ValueError, match="max_gap_minutes"):
        agg.push(minute(3))
    with pytest.raises(ValueError, match="aggregated volume"):
        agg.push(minute(2, v=1e308))
    assert agg.export_state() == before


@pytest.mark.parametrize("corrupt", [
    lambda s: s.update(version="future"),
    lambda s: s.update(extra=True),
    lambda s: s.update(has_positive_volume=False),
    lambda s: s.update(forming=None),
    lambda s: s["forming"].update(c=102),
    lambda s: s["forming"].update(v=0),
    lambda s: s["last_minute"].update(ts_open=1),
])
def test_restore_rejects_corrupt_state(corrupt):
    agg = MinuteBarAggregator("3")
    agg.push(minute(0))
    state = agg.export_state()
    corrupt(state)
    with pytest.raises(ValueError):
        MinuteBarAggregator.from_state(state)


def test_empty_checkpoint_roundtrip():
    agg = MinuteBarAggregator("5")
    assert MinuteBarAggregator.from_state(agg.export_state()).export_state() == agg.export_state()
