"""Session-calendar integration with synthetic inputs and fake engine only."""
import asyncio
import json
from dataclasses import replace

import pytest

from pineforge_live import types as T
from pineforge_live.adapters.synthetic import SyntheticMinuteTicks
from pineforge_live.bars.calendar import ParentWindows,normalize_windows
from pineforge_live.bars.minute import MinuteBarAggregator
from pineforge_live.bars.minute_stream import MinuteStream
from pineforge_live.config import ConfigError,load_signal_config
from pineforge_live.signals.runtime import run_signals
from tests.test_minute_runtime import fake_runtime,minute,Transport,direct_events,recorded
from tests.test_webhook_config import config_document,write_config


WINDOWS=((60_000,240_000),(900_000,1_020_000),(1_500_000,1_680_000),(3_000_000,3_120_000))


def scheduled_config(tmp_path,events,*,mode='bars',windows=WINDOWS):
    document=config_document(tmp_path)
    document.update(script_tf='1D',input_tf='1',input_mode=mode,parent_windows_path='calendar.json',
                    source={'kind':'jsonl','path':'events.jsonl'})
    (tmp_path/'calendar.json').write_text(json.dumps([{'open_ms':a,'close_ms':b} for a,b in windows]))
    (tmp_path/'history.csv').write_text('timestamp,open,high,low,close,volume\n60000,100,102,99,101,4\n900000,101,103,100,102,5\n')
    (tmp_path/'events.jsonl').write_text('\n'.join(map(json.dumps,events))+'\n')
    return load_signal_config(write_config(tmp_path,document))


def test_aggregation_skips_between_sessions_closes_at_explicit_end_and_restores():
    agg=MinuteBarAggregator('1D',parent_windows=WINDOWS)
    for index in (1,2):assert agg.push(minute(index))==[]
    done=agg.push(minute(3))
    assert len(done)==1 and done[0].ts_open==60_000
    agg=MinuteBarAggregator.from_state(json.loads(json.dumps(agg.export_state())))
    assert agg.push(minute(15))==[]
    done=agg.push(minute(16))
    assert len(done)==1 and done[0].ts_open==900_000
    assert done[0].v==2 and agg.forming() is None


def test_holes_inside_session_and_outside_window_rows_refuse():
    agg=MinuteBarAggregator('1D',parent_windows=WINDOWS)
    agg.push(minute(1))
    before=agg.export_state()
    with pytest.raises(ValueError,match='minute gap'):agg.push(minute(3))
    with pytest.raises(ValueError,match='outside supplied'):agg.push(minute(10))
    assert agg.export_state()==before


def test_explicit_carry_fills_only_scheduled_minutes():
    agg=MinuteBarAggregator('1D',parent_windows=WINDOWS,gap_policy='carry-forward')
    agg.push(minute(1))
    done=agg.push(minute(16))
    assert [b.ts_open for b in done]==[60_000,900_000]
    assert [b.v for b in done]==[1,1]


def test_synthetic_seq_crosses_session_gap_and_checkpoint():
    generator=SyntheticMinuteTicks('seeded',seed=4,parent_windows=WINDOWS)
    packets=[generator.push(minute(i)) for i in (1,2,3)]
    generator=SyntheticMinuteTicks.from_state(json.loads(json.dumps(generator.export_state())))
    packets.extend(generator.push(minute(i)) for i in (15,16))
    assert [t.seq for p in packets for t in p.ticks]==list(range(1,21))


@pytest.mark.parametrize('bad',[[],None,[{'open_ms':0,'close_ms':0}],[(0,60_001)],[(True,60_000)],
                                [(0,120_000),(60_000,180_000)],[(120_000,180_000),(0,60_000)],
                                [{'open_ms':0,'close_ms':60_000,'price':100}]])
def test_invalid_public_calendars_refused(tmp_path,bad):
    c=scheduled_config(tmp_path,[])
    (tmp_path/'calendar.json').write_text(json.dumps(bad))
    with pytest.raises(ConfigError):load_signal_config(c.path)


def test_schedule_changes_bind_epoch_and_config_identity_and_input_alias_is_protected(tmp_path):
    first=scheduled_config(tmp_path,[])
    changed=WINDOWS[:-1]+((3_060_000,3_180_000),)
    second=scheduled_config(tmp_path,[],windows=changed)
    assert first.config_hash!=second.config_hash
    assert first.epoch.epoch_hash()!=second.epoch.epoch_hash()
    document=json.loads(second.path.read_text());document['journal_path']='calendar.json'
    with pytest.raises(ConfigError):load_signal_config(write_config(tmp_path,document))


def test_history_must_match_schedule_prefix(tmp_path):
    c=scheduled_config(tmp_path,[])
    c.history_path.write_text('timestamp,open,high,low,close,volume\n900000,100,102,99,101,4\n')
    with pytest.raises(ConfigError,match='schedule prefix'):load_signal_config(c.path)


@pytest.mark.parametrize('mode',['bars','ticks'])
def test_public_runtime_session_parent_history_gap_restart_and_close(tmp_path,fake_runtime,mode):
    minutes=[minute(i,0 if i==25 else 0.1) for i in (25,26,27,50,51)]
    if mode=='bars':events=direct_events(minutes)
    else:
        events=[];generator=SyntheticMinuteTicks('low-first',parent_windows=WINDOWS)
        for bar in minutes:
            packet=generator.push(bar)
            events.extend({'type':'tick','ts':t.ts,'seq':t.seq,'price':t.price,'qty':t.qty} for t in packet.ticks)
            events.extend(direct_events([bar]))
    c=scheduled_config(tmp_path,events,mode=mode)
    transport=Transport()
    first=asyncio.run(run_signals(c,mode='check',transport=transport,max_events=2))
    assert first['error'] is None,first
    second=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert second['error'] is None,second
    assert len(transport.received)==2
    assert [p['timestamp'] for p in transport.received]==[1_680_000,3_120_000]
    assert [p['bar']['time'] for p in transport.received]==[1_500_000,3_000_000]
    assert transport.received[0]['bar']['open']==126
    state,rows,bars=recorded(c)
    assert state['forming'] is None and len(rows)==5
    assert sorted(row['ts_open'] for row in bars)==[900_000,1_500_000,3_000_000]
    third=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert third['error'] is None,third
    assert third['delivered']==0


def test_schedule_exhaustion_refuses_new_input_without_extra_webhook(tmp_path,fake_runtime):
    bars=[minute(i) for i in (25,26,27,50,51,52)]
    c=scheduled_config(tmp_path,direct_events(bars));transport=Transport()
    result=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert 'schedule exhausted' in result['error']
    assert len(transport.received)==2


def test_tick_preview_and_compact_checkpoint_do_not_copy_or_serialize_whole_calendar(monkeypatch):
    import pineforge_live.bars.calendar as module
    short=MinuteStream('3',parent_windows=[(0,180_000),(180_000,360_000)])
    long=MinuteStream('3',parent_windows=[(i*180_000,(i+1)*180_000) for i in range(5000)])
    # Validate the shape/work contract, not wall time: after initial calendar
    # construction neither a tick preview nor compact restoration can revisit
    # normalize_windows, regardless of schedule length.
    monkeypatch.setattr(module,'normalize_windows',lambda *args:(_ for _ in ()).throw(AssertionError('calendar copied per tick')))
    for stream in (short,long):
        calendar=stream.aggregator.calendar
        for sequence in (1,2,3):stream.push(T.Tick(T.NormalizedTick(sequence,sequence,100,1)))
        assert stream.aggregator.clone().calendar is calendar
        state=json.loads(json.dumps(stream.export_state(compact=True)))
        assert state['aggregator']['parent_windows'] is None
        restored=MinuteStream.from_state(state,parent_windows=calendar)
        assert restored.aggregator.calendar is calendar
        assert restored.forming()==stream.forming()
    assert len(json.dumps(long.export_state(compact=True)))==len(json.dumps(short.export_state(compact=True)))
    assert len(json.dumps(long.export_state(compact=True)))<1500


def test_compact_checkpoint_requires_exact_supplied_calendar():
    stream=MinuteStream('1D',parent_windows=WINDOWS)
    stream.push(T.Confirmed(minute(1)))
    compact=stream.export_state(compact=True)
    with pytest.raises(ValueError,match='calendar digest'):
        MinuteStream.from_state(compact)
    with pytest.raises(ValueError,match='calendar digest'):
        MinuteStream.from_state(compact,parent_windows=WINDOWS[:-1])
    restored=MinuteStream.from_state(compact,parent_windows=ParentWindows(WINDOWS))
    assert restored.forming()==stream.forming()


def test_runtime_checkpoint_only_stores_calendar_digest(tmp_path,fake_runtime):
    c=scheduled_config(tmp_path,direct_events([minute(25)]))
    report=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert report['error'] is None,report
    state,_,_=recorded(c)
    aggregation=state['input_state']['aggregator']
    assert aggregation['parent_windows'] is None
    assert aggregation['calendar_sha256']==ParentWindows(WINDOWS).sha256
