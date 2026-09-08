"""Public minute-input plumbing with a fake engine and synthetic bars only.

These tests prove runtime/checkpoint/webhook behavior, not campaign parity.
No compiled strategy or corpus fixture is loaded on the local machine.
"""
import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from pineforge_live import types as T
from pineforge_live.adapters.synthetic import SyntheticMinuteTicks
from pineforge_live.bars.minute_stream import MinuteStream
from pineforge_live.config import ConfigError, load_signal_config
from pineforge_live.engine.report import RunResult
from pineforge_live.journal import Journal
from pineforge_live.signals.engine import SignalEngine
from pineforge_live.signals.runtime import run_signals
from pineforge_live.webhooks.delivery import HttpResponse
from pineforge_live.webhooks.store import Outbox
from tests.test_webhook_config import config_document, write_config


def minute(index,v=1):
    return T.NormalizedBar(index*60_000,100.+index,103.+index,98.+index,101.+index,float(v),0)


class FakeHandle:
    def __init__(self,*args):pass
    def close(self):pass
    def request_abort(self):pass
    def run_full(self,bars,*args,**kwargs):
        # Stable prefix hashes let the real Python ledger exercise its G1 and
        # restart checks without executing any backtest or loading a library.
        hashes=[int(T.canonical_sha256(row)[:15],16) for row in bars]
        return RunResult(0,[],0,len(bars),hashes,0,float('nan'),0,float('nan'),10000,0)


class Transport:
    def __init__(self):self.received=[]
    async def post(self,url,body,headers,timeout_ms):
        self.received.append(json.loads(body))
        return HttpResponse(200)


@pytest.fixture
def fake_runtime(monkeypatch):
    import pineforge_live.signals.runtime as runtime
    import pineforge_live.signals.engine as signals
    monkeypatch.setattr(runtime,'EngineHandle',FakeHandle)
    monkeypatch.setattr(runtime,'apply_epoch',lambda *args:None)
    # Explicit fixture action: one callback per parent settlement. This is
    # only a delivery/idempotency stimulus, never reported as engine evidence.
    monkeypatch.setattr(signals,'emulated_from_settle',lambda result,*args:[SimpleNamespace(
        bar_index=result.bar_index,intent='fixture',leg='ENTRY',is_long=True,qty=1,price=result.bar.c)])


def config(tmp_path,events,*,input_mode='mixed'):
    d=config_document(tmp_path)
    d.update(script_tf='3',input_tf='1',input_mode=input_mode,source={'kind':'jsonl','path':'events.jsonl'})
    (tmp_path/'history.csv').write_text('timestamp,open,high,low,close,volume\n0,100,102,99,101,4\n180000,101,103,100,102,5\n')
    (tmp_path/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    return load_signal_config(write_config(tmp_path,d))


def direct_events(bars):return [{'type':'bar','bar':asdict(b)} for b in bars]


def tick_events(bars,policy='high-first'):
    generator=SyntheticMinuteTicks(policy)
    events=[]
    for bar in bars:
        packet=generator.push(bar)
        events.extend({'type':'tick','ts':t.ts,'seq':t.seq,'price':t.price,'qty':t.qty} for t in packet.ticks)
        events.append({'type':'bar','bar':asdict(bar)})
    return events


def recorded(c):
    j=Journal.open(c.journal_path)
    try:
        checkpoint=json.loads(j._exec('SELECT payload_json FROM signal_checkpoints').fetchone()[0])
        minutes=[dict(r) for r in j._exec('SELECT * FROM signal_input_minutes ORDER BY ts_open').fetchall()]
        bars=j.rows('bars','epoch_hash=?',(c.epoch.epoch_hash(),))
        return checkpoint,minutes,bars
    finally:j.close()


def test_default_input_tf_keeps_existing_configuration_identity(tmp_path):
    d=config_document(tmp_path)
    implicit=load_signal_config(write_config(tmp_path,d))
    d['input_tf']=d['script_tf']
    explicit=load_signal_config(write_config(tmp_path,d))
    assert implicit.input_tf=='1'
    assert implicit.config_hash==explicit.config_hash
    assert implicit.epoch.epoch_hash()==explicit.epoch.epoch_hash()


def test_existing_unscheduled_epoch_document_remains_compatible(tmp_path,fake_runtime):
    c=config(tmp_path,direct_events([minute(6)]))
    c.journal_path.parent.mkdir(parents=True,exist_ok=True)
    old=asdict(c.epoch);old.pop('parent_windows')
    j=Journal.open(c.journal_path)
    j.append_epoch(c.epoch.epoch_hash(),json.dumps(old,sort_keys=True,separators=(',',':'),
                                                 ensure_ascii=False,default=T._canon))
    j.close()
    report=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert report['error'] is None,report


@pytest.mark.parametrize('input_tf',['2','0','1m',True,None])
def test_input_tf_rejects_unsupported_modes(tmp_path,input_tf):
    d=config_document(tmp_path);d['input_tf']=input_tf
    with pytest.raises(ConfigError):load_signal_config(write_config(tmp_path,d))


def test_direct_minutes_form_evaluate_close_and_survive_partial_restart(tmp_path,fake_runtime):
    c=config(tmp_path,direct_events([minute(i,0 if i==6 else 1) for i in range(6,12)]))
    transport=Transport()
    first=asyncio.run(run_signals(c,mode='check',transport=transport,max_events=2))
    assert first['error'] is None,first
    assert first['settled_bars']==0 and first['evaluations']==2
    state,rows,_=recorded(c)
    assert state['last_input_minute']['ts_open']==420_000 and len(rows)==2
    assert state['forming']['o']==107 and state['forming']['l']==104
    second=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert second['error'] is None,second
    assert second['settled_bars']==2 and second['delivered']==2
    state,rows,bars=recorded(c)
    assert state['forming'] is None and len(rows)==6
    assert len(transport.received)==2
    assert [p['bar']['time'] for p in transport.received]==[360_000,540_000]
    third=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert third['error'] is None and third['settled_bars']==third['delivered']==0
    assert len(transport.received)==2


@pytest.mark.parametrize('policy',['high-first','low-first','seeded'])
def test_tick_boundaries_match_direct_minute_parent_bars_and_restart(tmp_path,fake_runtime,policy):
    bars=[minute(i,0 if i in (6,10) else 0.1) for i in range(6,12)]
    direct=tmp_path/'direct';direct.mkdir()
    tickdir=tmp_path/'tick';tickdir.mkdir()
    c1=config(direct,direct_events(bars),input_mode='bars')
    c2=config(tickdir,tick_events(bars,policy),input_mode='ticks')
    t1,t2=Transport(),Transport()
    result=asyncio.run(run_signals(c1,mode='check',transport=t1))
    assert result['error'] is None,result
    partial=asyncio.run(run_signals(c2,mode='check',transport=t2,max_events=3))
    assert partial['error'] is None,partial
    state,_,_=recorded(c2)
    assert state['input_state']['pending']['trade_count']==2
    result=asyncio.run(run_signals(c2,mode='check',transport=t2))
    assert result['error'] is None,result
    assert result['delivered']==2
    a=[p['bar'] for p in t1.received];b=[p['bar'] for p in t2.received]
    assert a==b
    again=asyncio.run(run_signals(c2,mode='check',transport=t2))
    assert again['error'] is None and again['delivered']==0
    assert len(t2.received)==2


def test_changed_old_minute_replay_refuses_before_new_webhook(tmp_path,fake_runtime):
    bars=[minute(i) for i in range(6,12)]
    c=config(tmp_path,direct_events(bars));transport=Transport()
    assert asyncio.run(run_signals(c,mode='check',transport=transport))['error'] is None
    changed=direct_events([replace(bars[0],v=2),*bars[1:]])
    c.source.path.write_text('\n'.join(map(json.dumps,changed))+'\n')
    result=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert 'historical input minute changed' in result['error']
    assert len(transport.received)==2


def test_tick_boundary_mismatch_refuses_parent_replacement(tmp_path,fake_runtime):
    events=tick_events([minute(6)])
    events[-1]['bar']['h']+=10
    c=config(tmp_path,events)
    result=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert 'tick-built minute disagrees' in result['error']
    state,rows,_=recorded(c)
    assert not rows and state['input_state']['pending']['trade_count']==4


def test_minute_commit_failure_rolls_back_input_parent_and_outbox(tmp_path,fake_runtime,monkeypatch):
    c=config(tmp_path,direct_events([minute(i) for i in range(6,9)]))
    original=SignalEngine._save
    def fail_final(self):
        if self.last_input_minute is not None and self.last_input_minute.ts_open==480_000:
            raise RuntimeError('injected checkpoint failure')
        return original(self)
    monkeypatch.setattr(SignalEngine,'_save',fail_final)
    result=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert 'injected checkpoint failure' in result['error']
    state,rows,bars=recorded(c)
    assert state['last_input_minute']['ts_open']==420_000
    assert len(rows)==2 and all(r['ts_open']!=480_000 for r in rows)
    assert all(r['ts_open']!=360_000 for r in bars)
    j=Journal.open(c.journal_path)
    try:assert Outbox(j,c.epoch.epoch_hash(),c.webhook.target_url).inspect()==[]
    finally:j.close()


def test_minute_stream_requires_boundary_and_retains_zero_volume_quote_extremes():
    stream=MinuteStream('3')
    stream.push(T.Confirmed(minute(0,0)))
    generator=SyntheticMinuteTicks()
    for t in generator.push(minute(1)).ticks:stream.push(T.Tick(t))
    with pytest.raises(ValueError,match='boundary required'):
        stream.push(T.Tick(T.NormalizedTick(120_000,5,102,1)))
    stream=MinuteStream.from_state(json.loads(json.dumps(stream.export_state())))
    stream.push(T.Confirmed(minute(1)))
    final=stream.push(T.Confirmed(minute(2)))[0].bar
    assert final.o==101 and final.l==98 and final.c==103


def test_strict_tick_mode_refuses_an_entire_missing_trade_minute(tmp_path,fake_runtime):
    c=config(tmp_path,direct_events([minute(6)]),input_mode='ticks')
    report=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert 'requires trades for every positive-volume minute' in report['error']
    assert report['settled_bars']==0


def test_strict_bar_mode_refuses_trade_ticks(tmp_path,fake_runtime):
    c=config(tmp_path,tick_events([minute(6)]),input_mode='bars')
    report=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert 'bars input mode refuses ticks' in report['error']
