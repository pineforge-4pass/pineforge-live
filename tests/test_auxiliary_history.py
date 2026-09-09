"""Auxiliary C ABI/feed plumbing, using fake decisions and synthetic data only."""
import asyncio
import ctypes
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from pineforge_live.engine.auxiliary import AuxiliaryHistory,_BAR
from pineforge_live.engine.handle import EngineHandle
from pineforge_live.config import ConfigError,load_signal_config
from pineforge_live.signals.runtime import run_signals
from pineforge_live.verification.probe_case import write_bars,copy_original_minute_range
from tests.test_minute_runtime import FakeHandle,fake_runtime,config,minute,direct_events,tick_events,Transport,recorded


def history(tmp_path,n=6):
    path=tmp_path/'aux.csv';write_bars(path,[minute(i) for i in range(n)])
    return path,hashlib.sha256(path.read_bytes()).hexdigest()


def test_auxiliary_history_pins_bytes_orders_and_preserves_native_layout(tmp_path):
    path,sha=history(tmp_path)
    h=AuxiliaryHistory.read(path,sha,start_ms=60000)
    assert h.count==5 and h.first_ms==60000 and h.last_ms==300000
    data,count=h.with_observed([minute(6)])
    assert count==6 and len(data)==count*48
    assert [tuple((r[-1],*r[:-1])) for r in _BAR.iter_unpack(data)][0]==minute(1).ohlcv()
    assert [tuple((r[-1],*r[:-1])) for r in _BAR.iter_unpack(data)][-1]==minute(6).ohlcv()
    with pytest.raises(ValueError,match='overlap'):h.with_observed([minute(5)])
    path.write_text(path.read_text()+'\n')
    with pytest.raises(ValueError,match='changed'):AuxiliaryHistory.read(path,sha)


def test_original_auxiliary_prefix_preserves_csv_row_bytes(tmp_path):
    path=tmp_path/'original.csv'
    path.write_bytes(b'timestamp,open,high,low,close,volume\r\n0,1.00,3.000,0.5,2,4.000\r\n60000,2,4,1,3,5\r\n120000,3,5,2,4,6\r\n')
    output=tmp_path/'prefix.csv'
    assert copy_original_minute_range(path,output,60000,120000)==1
    assert output.read_bytes()==path.read_bytes().splitlines(keepends=True)[0]+path.read_bytes().splitlines(keepends=True)[2]


def test_optional_c_abi_receives_original_and_observed_minute_arrays(tmp_path):
    path,sha=history(tmp_path);calls=[]
    class Function:
        def __call__(self,strategy,bars,count,tf):
            calls.append((strategy,count,tf,[(bars[i].timestamp,bars[i].open,bars[i].volume) for i in range(count)]))
            return 0
    handle=object.__new__(EngineHandle);handle.lib=SimpleNamespace(strategy_set_aux_security_feed=Function())
    handle.set_auxiliary_history(path,sha)
    handle.auxiliary_provider=lambda:[minute(6)]
    handle._apply_auxiliary(123)
    assert calls==[(123,7,b'1',[(i*60000,100.+i,1.) for i in range(7)])]


class FakeAuxHandle(FakeHandle):
    calls=[]
    def __init__(self,*args):
        self.auxiliary_history=None;self.auxiliary_provider=None
    def set_auxiliary_history(self,path,sha256,*,start_ms=0):
        self.auxiliary_history=AuxiliaryHistory.read(path,sha256,start_ms=start_ms)
    def run_full(self,bars,*args,**kwargs):
        observed=self.auxiliary_provider() if self.auxiliary_provider else ()
        data,count=self.auxiliary_history.with_observed(observed)
        self.calls.append((len(bars),tuple((row[-1],*row[:-1]) for row in _BAR.iter_unpack(data))))
        rows=[b.ohlcv() if hasattr(b,'ohlcv') else b for b in bars]
        return super().run_full(rows,*args,**kwargs)


def aux_config(tmp_path,events,*,input_mode='bars',trigger_mode='settled'):
    c=config(tmp_path,events,input_mode=input_mode);path,sha=history(tmp_path)
    document=json.loads(c.path.read_text());document.update(auxiliary_history_path=str(path),trigger_mode=trigger_mode)
    c.path.write_text(json.dumps(document));return load_signal_config(c.path)


@pytest.mark.parametrize('input_mode',['bars','ticks'])
@pytest.mark.parametrize('trigger_mode',['settled','intrabar'])
def test_one_runtime_restores_committed_auxiliary_minutes_and_current_input(tmp_path,monkeypatch,fake_runtime,input_mode,trigger_mode):
    import pineforge_live.signals.runtime as runtime
    monkeypatch.setattr(runtime,'EngineHandle',FakeAuxHandle);FakeAuxHandle.calls=[]
    events=(direct_events if input_mode=='bars' else tick_events)([minute(i) for i in range(6,9)])
    c=aux_config(tmp_path,events,input_mode=input_mode,trigger_mode=trigger_mode);transport=Transport()
    report=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert report['error'] is None,report
    assert report['settled_bars']==1 and report['delivered']==1
    assert len(FakeAuxHandle.calls[0][1])==6
    assert FakeAuxHandle.calls[-1][1]==tuple(minute(i).ohlcv() for i in range(9))
    if trigger_mode=='intrabar':
        assert any(n==3 and len(rows)==7 for n,rows in FakeAuxHandle.calls)
    before=len(FakeAuxHandle.calls)
    again=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert again['error'] is None and again['delivered']==0
    assert FakeAuxHandle.calls[before][1]==tuple(minute(i).ohlcv() for i in range(9))
    assert len(recorded(c)[1])==3


def test_auxiliary_warmup_refuses_unobserved_future_minutes(tmp_path,monkeypatch,fake_runtime):
    import pineforge_live.signals.runtime as runtime
    monkeypatch.setattr(runtime,'EngineHandle',FakeAuxHandle);FakeAuxHandle.calls=[]
    c=aux_config(tmp_path,direct_events([minute(6)]))
    history(tmp_path,n=7);c=load_signal_config(c.path)
    report=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert 'warmup must end' in report['error'] and not FakeAuxHandle.calls


def test_auxiliary_file_change_and_journal_alias_refuse_before_execution(tmp_path,monkeypatch,fake_runtime):
    import pineforge_live.signals.runtime as runtime
    monkeypatch.setattr(runtime,'EngineHandle',FakeAuxHandle);FakeAuxHandle.calls=[]
    c=aux_config(tmp_path,direct_events([minute(6)]))
    c.auxiliary_history_path.write_text(c.auxiliary_history_path.read_text()+'\n')
    report=asyncio.run(run_signals(c,mode='check',transport=Transport()))
    assert 'changed after configuration' in report['error'] and not FakeAuxHandle.calls
    document=json.loads(c.path.read_text());document['auxiliary_history_path']=str(c.journal_path)+'-wal'
    c.path.write_text(json.dumps(document))
    with pytest.raises(ConfigError,match='overlap'):load_signal_config(c.path)


def test_auxiliary_identity_is_portable_but_required_before_epoch_application(tmp_path):
    from pineforge_live.epoch import apply_epoch
    c=aux_config(tmp_path,direct_events([minute(6)]))
    with pytest.raises(RuntimeError,match='not bound'):
        apply_epoch(SimpleNamespace(auxiliary_history=None),c.epoch)
    copy=tmp_path/'copy.csv';copy.write_bytes(c.auxiliary_history_path.read_bytes())
    doc=json.loads(c.path.read_text());doc['auxiliary_history_path']=str(copy);c.path.write_text(json.dumps(doc))
    moved=load_signal_config(c.path)
    assert moved.epoch.epoch_hash()==c.epoch.epoch_hash() and moved.config_hash==c.config_hash


def test_partial_tick_restart_uses_current_minute_without_future_rows(tmp_path,monkeypatch,fake_runtime):
    import pineforge_live.signals.runtime as runtime
    monkeypatch.setattr(runtime,'EngineHandle',FakeAuxHandle);FakeAuxHandle.calls=[]
    c=aux_config(tmp_path,tick_events([minute(i) for i in range(6,9)]),input_mode='ticks',trigger_mode='intrabar')
    transport=Transport()
    first=asyncio.run(run_signals(c,mode='check',transport=transport,max_events=3))
    assert first['error'] is None and first['delivered']==0
    assert len(recorded(c)[1])==0
    assert len(FakeAuxHandle.calls[-1][1])==7 and FakeAuxHandle.calls[-1][1][-1][5]==0.75
    before=len(FakeAuxHandle.calls)
    resumed=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert resumed['error'] is None and resumed['delivered']==1
    assert len(FakeAuxHandle.calls[before][1])==6  # Confirmed ledger seed only.
    assert FakeAuxHandle.calls[-1][1]==tuple(minute(i).ohlcv() for i in range(9))


def test_auxiliary_restart_refuses_tampered_committed_minute(tmp_path,monkeypatch,fake_runtime):
    import pineforge_live.signals.runtime as runtime
    from pineforge_live.journal import Journal
    monkeypatch.setattr(runtime,'EngineHandle',FakeAuxHandle);FakeAuxHandle.calls=[]
    c=aux_config(tmp_path,direct_events([minute(i) for i in range(6,9)]))
    transport=Transport();first=asyncio.run(run_signals(c,mode='check',transport=transport,max_events=1))
    assert first['error'] is None
    j=Journal.open(c.journal_path)
    j._exec("UPDATE signal_input_minutes SET payload_json='{}'");j.close()
    before=len(FakeAuxHandle.calls)
    resumed=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert 'auxiliary input minute checksum mismatch' in resumed['error']
    assert len(FakeAuxHandle.calls)==before and not transport.received


@pytest.mark.parametrize('mode',['bars','ticks'])
def test_observed_auxiliary_stream_keeps_sparse_rows_without_padding(tmp_path,monkeypatch,fake_runtime,mode):
    import pineforge_live.signals.runtime as runtime
    from pineforge_live.adapters.mock_feed import mock_events
    monkeypatch.setattr(runtime,'EngineHandle',FakeAuxHandle);FakeAuxHandle.calls=[]
    path=tmp_path/'sparse.csv';write_bars(path,[minute(8)])
    events=list(mock_events(path,mode=mode,gap_policy='observed'))
    c=aux_config(tmp_path,events,input_mode=mode)
    document=json.loads(c.path.read_text());document['input_gap_policy']='observed';c.path.write_text(json.dumps(document));c=load_signal_config(c.path)
    transport=Transport();report=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert report['error'] is None and report['delivered']==1,report
    assert FakeAuxHandle.calls[-1][1]==tuple(minute(i).ohlcv() for i in [0,1,2,3,4,5,8])
    assert len(recorded(c)[1])==1
    again=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert again['error'] is None and again['delivered']==0
