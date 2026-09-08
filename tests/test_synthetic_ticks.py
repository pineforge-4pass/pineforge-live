import json
from dataclasses import replace

import pytest

from pineforge_live import types as T
from pineforge_live.adapters.synthetic import SyntheticMinuteTicks
from pineforge_live.bars.builder import FormingBarBuilder, compare_bar
from pineforge_live.bars.minute import MinuteBarAggregator


def minute(index, *, v=1.0):
    return T.NormalizedBar(index * 60_000, 100.0 + index, 110.0 + index,
                           90.0 + index, 101.0 + index, v, 0)


@pytest.mark.parametrize("policy,prices", [
    ("high-first", [100, 110, 90, 101]),
    ("low-first", [100, 90, 110, 101]),
])
def test_explicit_paths_exact_ohlcv_within_minute(policy, prices):
    packet = SyntheticMinuteTicks(policy, start_seq=42).push(minute(0, v=0.1))
    assert [t.price for t in packet.ticks] == prices
    assert [t.ts for t in packet.ticks] == [0, 20_000, 40_000, 59_999]
    assert [t.seq for t in packet.ticks] == [42, 43, 44, 45]
    assert packet.ts_close == 60_000
    assert sum(t.qty for t in packet.ticks) == packet.bar.v
    assert all(t.qty > 0 for t in packet.ticks)
    assert compare_bar(packet.reconstructed_bar(), packet.bar) == []


def test_empty_minute_emits_boundary_metadata_and_no_fabricated_trades():
    generator = SyntheticMinuteTicks(start_seq=7)
    first = generator.push(minute(0))
    empty = generator.push(minute(1, v=0))
    last = generator.push(minute(2))
    assert empty.ticks == () and empty.ts_close == 120_000
    assert empty.reconstructed_bar() == empty.bar
    assert first.ticks[-1].seq + 1 == last.ticks[0].seq
    assert generator.next_seq == 15


@pytest.mark.parametrize("policy", ["high-first", "low-first", "seeded"])
def test_tick_and_minute_modes_produce_same_final_script_bars(policy):
    direct, derived = MinuteBarAggregator("3"), MinuteBarAggregator("3")
    generator = SyntheticMinuteTicks(policy, seed=8)
    reference, actual = [], []
    # Include a zero-volume prefix, interior empty minute, and all-empty bucket.
    volumes = [0, 0.1, 3.3, 4.7, 0, 5.3, 0, 0, 0, 9.9, 1.1, 2.2]
    for index, volume in enumerate(volumes):
        bar = minute(index, v=volume)
        packet = generator.push(bar)
        reference.extend(direct.push(bar))
        actual.extend(derived.push(packet.reconstructed_bar()))
    assert len(reference) == len(actual) == 4
    assert all(compare_bar(a, b) == [] for a, b in zip(reference, actual))
    assert reference[0].o == 101 and reference[0].l == 90
    assert reference[2].ohlcv() == (360_000, 106, 118, 96, 109, 0)


def test_nonempty_ticks_also_match_existing_tick_builder():
    generator = SyntheticMinuteTicks("low-first")
    ticks, minutes = FormingBarBuilder("3"), MinuteBarAggregator("3")
    actual, expected = [], []
    for index in range(6):
        bar = minute(index, v=0.1)
        for tick in generator.push(bar).ticks:
            actual.extend(ticks.push(tick))
        expected.extend(minutes.push(bar))
    actual.append(ticks.forming())
    assert all(compare_bar(a, b) == [] for a, b in zip(actual, expected))


def test_seeded_replay_and_resume_reproduce_every_tick_and_seq():
    uninterrupted = SyntheticMinuteTicks("seeded", seed=71)
    expected = [uninterrupted.push(minute(index, v=0 if index == 4 else 0.1)) for index in range(20)]
    generator = SyntheticMinuteTicks("seeded", seed=71)
    actual = []
    for index in range(20):
        if index in (4, 5, 13):
            generator = SyntheticMinuteTicks.from_state(json.loads(json.dumps(generator.export_state())))
        actual.append(generator.push(minute(index, v=0 if index == 4 else 0.1)))
    assert actual == expected
    assert any(p.ticks[1].price == p.bar.h for p in actual if p.ticks)
    assert any(p.ticks[1].price == p.bar.l for p in actual if p.ticks)


def test_duplicate_is_stable_after_resume_but_changes_and_gaps_refused():
    generator = SyntheticMinuteTicks("seeded", seed=1)
    packet = generator.push(minute(0))
    generator = SyntheticMinuteTicks.from_state(generator.export_state())
    assert generator.push(minute(0)) == packet
    assert generator.next_seq == 5
    with pytest.raises(ValueError, match="changed duplicate"):
        generator.push(minute(0, v=2))
    with pytest.raises(ValueError, match="contiguous"):
        generator.push(minute(2))
    assert generator.next_seq == 5


@pytest.mark.parametrize("volume", [0.1, 1.0000001, 1e-200, 1e200, 173221.398103])
def test_volume_remainder_preserves_input_with_float_tolerance(volume):
    packet = SyntheticMinuteTicks().push(minute(0, v=volume))
    assert sum(t.qty for t in packet.ticks) == pytest.approx(volume, rel=2e-16, abs=0)
    assert all(t.qty > 0 for t in packet.ticks)


def test_unrepresentable_trade_quantities_refused_without_advancing():
    generator = SyntheticMinuteTicks()
    with pytest.raises(ValueError, match="too small"):
        generator.push(minute(0, v=5e-324))
    assert generator.next_seq == 1
    assert generator.export_state()["last_minute"] is None


@pytest.mark.parametrize("kwargs", [{"policy": "random"}, {"seed": True}, {"start_seq": -1}, {"start_seq": 0.5}])
def test_generator_configuration_is_strict(kwargs):
    with pytest.raises(ValueError):
        SyntheticMinuteTicks(**kwargs)


def test_timestamp_and_sequence_bounds_are_checked_before_generation():
    generator = SyntheticMinuteTicks(start_seq=2**63 - 3)
    with pytest.raises(ValueError, match="next_seq"):
        generator.push(minute(0))
    with pytest.raises(ValueError, match="UTC minute"):
        SyntheticMinuteTicks().push(replace(minute(0), ts_open=1))


def test_synthetic_checkpoint_cannot_forge_negative_prior_sequence():
    generator = SyntheticMinuteTicks()
    generator.push(minute(0))
    with pytest.raises(ValueError):
        SyntheticMinuteTicks.from_state({**generator.export_state(), "next_seq": 2})
