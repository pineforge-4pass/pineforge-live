"""Session labels can precede trading; quiet time is declared, never invented."""
import asyncio
from dataclasses import replace
from datetime import datetime,timedelta
import json
from zoneinfo import ZoneInfo

import pytest
from pineforge_live import types as T
from pineforge_live.adapters.synthetic import SyntheticMinuteTicks
from pineforge_live.bars.calendar import ParentWindows,normalize_windows
from pineforge_live.bars.minute import MinuteBarAggregator
from pineforge_live.config import load_signal_config
from pineforge_live.signals.runtime import run_signals
from pineforge_live.verification.probe_case import first_session_minute,replay_calendar,choose_window
from tests.test_minute_runtime import fake_runtime,config,minute,direct_events,tick_events,Transport,recorded


def test_label_prefix_is_not_a_missing_minute_and_remains_bound_to_identity():
    raw=[{'open_ms':0,'close_ms':180000,'first_minute_ms':60000},[180000,360000,240000]]
    calendar=ParentWindows(raw)
    assert calendar.windows==((0,180000),(180000,360000))
    assert calendar.first_minutes==(60000,240000)
    assert calendar.next_minute(120000)==240000
    with pytest.raises(ValueError):calendar.containing(0)
    assert ParentWindows([[0,180000,0]]).sha256==ParentWindows([[0,180000]]).sha256
    assert calendar.sha256!=ParentWindows(calendar.windows).sha256
    generator=SyntheticMinuteTicks(parent_windows=calendar)
    agg=MinuteBarAggregator('3',parent_windows=calendar)
    assert not agg.push(minute(1))
    restored=MinuteBarAggregator.from_state(agg.export_state())
    compact=MinuteBarAggregator.from_state(agg.export_state(compact=True),parent_windows=calendar)
    for instance in (agg,restored,compact):
        closed=instance.push(minute(2))
        assert len(closed)==1 and closed[0].ts_open==0 and closed[0].o==101
    generator.push(minute(1));generator.push(minute(2))
    restored_ticks=SyntheticMinuteTicks.from_state(generator.export_state())
    assert restored_ticks.push(minute(4)).ticks[0].seq==9
    with pytest.raises(ValueError,match='first minute'):
        MinuteBarAggregator('3',parent_windows=calendar).push(minute(2))


@pytest.mark.parametrize('first',[True,-60000,1,180000,240000])
def test_first_minute_must_be_aligned_inside_its_parent(first):
    with pytest.raises(ValueError):normalize_windows([{'open_ms':0,'close_ms':180000,'first_minute_ms':first}])


def test_session_hours_not_observed_data_define_daily_first_minute():
    zone=ZoneInfo('America/New_York')
    labels=[int(datetime(2026,3,6+i,17,tzinfo=zone).timestamp())*1000 for i in range(5)]
    chart=[replace(minute(i),ts_open=t) for i,t in enumerate(labels)]
    first=[first_session_minute(t,'1800-1700','America/New_York') for t in labels]
    stamps=[t for a,b in zip(first,labels[1:]) for t in range(a,b,60000)]
    calendar=replay_calendar(chart,stamps,'1D',session='1800-1700',timezone='America/New_York')
    assert all(row[2]==start for row,start in zip(calendar,first))
    assert (labels[2]-first[1])//60000==22*60  # DST spring transition.
    assert choose_window(chart,stamps,calendar,[],2)==(1,3)
    # Removing actual session-open minutes remains a refusal, never a later
    # inferred opening chosen from the remaining data.
    with pytest.raises(ValueError,match='no complete'):
        choose_window(chart,[t for t in stamps if t not in first],calendar,[],2)


@pytest.mark.parametrize('input_mode',['bars','ticks'])
def test_public_runner_and_restart_accept_declared_late_first_minute(tmp_path,fake_runtime,input_mode):
    events=(direct_events if input_mode=='bars' else tick_events)([minute(7),minute(8)])
    c=config(tmp_path,events,input_mode=input_mode)
    path=tmp_path/'calendar.json';path.write_text(json.dumps([[0,180000],[180000,360000],[360000,540000,420000],[540000,720000,600000]]))
    doc=json.loads(c.path.read_text());doc['parent_windows_path']=str(path);c.path.write_text(json.dumps(doc));c=load_signal_config(c.path)
    transport=Transport();report=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert report['error'] is None,report
    assert report['settled_bars']==1 and report['delivered']==1
    state,minutes,bars=recorded(c)
    assert bars[-1]['ts_open']==360000 and len(minutes)==2
    second=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert second['error'] is None and second['delivered']==0 and len(transport.received)==1


def test_special_session_keeps_native_label_and_does_not_hide_missing_open():
    zone=ZoneInfo('Asia/Kolkata')
    labels=[int(datetime(2021,11,day,hour,minute_,tzinfo=zone).timestamp())*1000
            for day,hour,minute_ in [(3,9,15),(4,18,0),(8,9,15),(9,9,15)]]
    chart=[replace(minute(i),ts_open=t) for i,t in enumerate(labels)]
    stamps=[t for i,start in enumerate(labels) for t in range(start+(7*60000 if i==1 else 0),start+(60 if i==1 else 375)*60000,60000)]
    calendar=replay_calendar(chart,stamps,'1D',session='0915-1530',timezone='Asia/Kolkata')
    assert calendar[1][0]==labels[1] and len(calendar[1])==2
    assert calendar.provenance[1]['close_ms'] is None
    assert calendar.unknown_close_indices==frozenset({1})
    trade=type('ClosedTrade',(),{'exit_bar_index':1,'open_at_end':False})()
    assert choose_window(chart,stamps,calendar,[trade],1,gap_policy='observed')==(2,3)
    assert choose_window(chart,stamps,calendar,[trade],1)==(2,3)
