"""Explicit sparse input keeps original rows; strict input remains default."""
import asyncio
from dataclasses import asdict
import json

import pytest
from pineforge_live.adapters.synthetic import SyntheticMinuteTicks
from pineforge_live.adapters.mock_feed import mock_events
from pineforge_live.bars.minute import MinuteBarAggregator
from pineforge_live.bars.minute_stream import MinuteStream
from pineforge_live.config import load_signal_config,ConfigError
from pineforge_live.signals.runtime import run_signals
from pineforge_live.verification.probe_case import write_bars
from tests.test_minute_runtime import fake_runtime,config,minute,Transport,recorded


def sparse_minutes():return [minute(11,0),minute(13,2),minute(14,3)]


def test_observed_aggregation_never_invents_prices_or_volume():
    rows=sparse_minutes();agg=MinuteBarAggregator('5',gap_policy='observed')
    for row in rows[:-1]:assert not agg.push(row)
    state=agg.export_state();restored=MinuteBarAggregator.from_state(state)
    for a in (agg,restored):
        bars=a.push(rows[-1]);assert len(bars)==1
        assert bars[0].ohlcv()==(600000,113.,117.,109.,115.,5.)
    with pytest.raises(ValueError,match='first minute'):
        MinuteBarAggregator('5').push(rows[0])


def test_observed_cannot_hide_missing_parent_close_or_entire_parent():
    agg=MinuteBarAggregator('5',gap_policy='observed');agg.push(minute(1));state=agg.export_state()
    with pytest.raises(ValueError,match='closing minute'):agg.push(minute(6))
    assert agg.export_state()==state
    agg.push(minute(4));state=agg.export_state()
    with pytest.raises(ValueError,match='entire parent'):agg.push(minute(11))
    assert agg.export_state()==state


def test_mock_sparse_input_is_explicit_and_restores_its_gap_policy(tmp_path):
    path=tmp_path/'minutes.csv';write_bars(path,sparse_minutes())
    with pytest.raises(ValueError):list(mock_events(path))
    events=list(mock_events(path,gap_policy='observed'))
    assert len(events)==11  # Original3 bars +8 ticks, no padded minutes.
    assert [e['bar']['ts_open'] for e in events if e['type']=='bar']==[660000,780000,840000]
    gen=SyntheticMinuteTicks(gap_policy='observed');gen.push(minute(11,0))
    restored=SyntheticMinuteTicks.from_state(gen.export_state())
    assert restored.push(minute(13,2)).ticks[0].seq==1
    old=SyntheticMinuteTicks().export_state();assert 'gap_policy' not in old
    assert SyntheticMinuteTicks.from_state(old).gap_policy=='reject'


@pytest.mark.parametrize('mode',['bars','ticks'])
def test_public_sparse_stream_partial_restart_and_exact_input_rows(tmp_path,fake_runtime,mode):
    feed=tmp_path/'minutes.csv';write_bars(feed,sparse_minutes())
    events=list(mock_events(feed,mode=mode,gap_policy='observed'))
    c=config(tmp_path,events,input_mode=mode)
    (tmp_path/'history.csv').write_text('timestamp,open,high,low,close,volume\n0,100,102,99,101,4\n300000,101,103,100,102,5\n')
    doc=json.loads(c.path.read_text());doc.update(script_tf='5',input_gap_policy='observed');c.path.write_text(json.dumps(doc));c=load_signal_config(c.path)
    transport=Transport();first=asyncio.run(run_signals(c,mode='check',transport=transport,max_events=2))
    assert first['error'] is None,first
    second=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert second['error'] is None and second['delivered']==1,second
    state,minutes,bars=recorded(c)
    assert len(minutes)==3 and [r['ts_open'] for r in minutes]==[660000,780000,840000]
    assert tuple(bars[-1][k] for k in ('ts_open','o','h','l','c','v'))==(600000,113.,117.,109.,115.,5.)
    assert state['input_state']['aggregator']['gap_policy']=='observed'
    again=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert again['error'] is None and again['delivered']==0


def test_gap_policy_is_hashed_and_cannot_request_implicit_padding(tmp_path):
    c=config(tmp_path,[],input_mode='bars');document=json.loads(c.path.read_text())
    document['input_gap_policy']='reject';c.path.write_text(json.dumps(document));explicit=load_signal_config(c.path)
    assert explicit.epoch.epoch_hash()==c.epoch.epoch_hash()
    document['input_gap_policy']='observed';c.path.write_text(json.dumps(document));observed=load_signal_config(c.path)
    assert observed.epoch.epoch_hash()!=c.epoch.epoch_hash()
    document['input_gap_policy']='carry-forward';c.path.write_text(json.dumps(document))
    with pytest.raises(ConfigError):load_signal_config(c.path)
