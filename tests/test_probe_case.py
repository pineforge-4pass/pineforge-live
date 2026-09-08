"""Offline verifier contract tests: all strategy decisions below are fake."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from pineforge_live import types as T
from pineforge_live.bars.minute import MinuteBarAggregator
from pineforge_live.engine.report import RunResult, TradeRow
from pineforge_live.verification import probe_case as case


def minute(i):
    return T.NormalizedBar(i*60_000,100+i,103+i,98+i,101+i,4.,0)


def test_direct_and_tick_event_writer_preserve_explicit_empty_quotes(tmp_path):
    minutes=[replace(minute(0),v=0),minute(1),minute(2)]
    for mode,policy,count in [('bars','direct',3),('ticks','high-first',11),('ticks','low-first',11)]:
        path=tmp_path/(mode+policy+'.jsonl')
        case.write_events(path,minutes,'3',[(0,180_000)],mode,policy,1)
        rows=[json.loads(x) for x in path.read_text().splitlines()]
        assert len(rows)==count
        assert [x['bar']['v'] for x in rows if x['type']=='bar']==[0,4,4]
        assert rows[0]['type']=='bar' and rows[0]['bar']['h']==103


def test_calendar_and_window_are_price_independent_and_refuse_internal_holes():
    chart=[replace(minute(i*3),ts_open=i*180_000) for i in range(8)]
    stamps=[i*60_000 for i in range(24)]
    windows=case.replay_calendar(chart,stamps,'3')
    changed=[replace(x,o=x.o+20,h=x.h+20,l=x.l+20,c=x.c+20) for x in chart]
    assert case.replay_calendar(changed,stamps,'3')==windows
    assert case.choose_window(chart,stamps,windows,[SimpleNamespace(exit_bar_index=4,open_at_end=False)],2)==(3,5)
    with pytest.raises(ValueError,match='no complete'):
        case.choose_window(chart,stamps[::2],windows,[],2)


def test_feed_reader_rejects_invalid_order_and_nonfinite(tmp_path):
    p=tmp_path/'bars.csv'
    case.write_bars(p,[minute(2),minute(1)])
    with pytest.raises(ValueError,match='order'):case.read_bars(p)
    case.write_bars(p,[replace(minute(1),v=float('nan'))])
    with pytest.raises(ValueError,match='invalid'):case.read_bars(p)
    case.write_bars(p,[replace(minute(1),ts_open=60001)])
    with pytest.raises(ValueError,match='invalid'):case.read_bars(p)


class FakeHandle:
    def __init__(self,*args):self.auxiliary_history=None;self.auxiliary_provider=None
    def set_auxiliary_history(self,path,sha,*,start_ms=0):
        from pineforge_live.engine.auxiliary import AuxiliaryHistory
        self.auxiliary_history=AuxiliaryHistory.read(path,sha,start_ms=start_ms)
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def close(self):pass
    def request_abort(self):pass
    def run_full(self,bars,*args,**kwargs):
        rows=[b.ohlcv() if hasattr(b,'ohlcv') else b for b in bars]
        if self.auxiliary_history is not None:
            observed=self.auxiliary_provider() if self.auxiliary_provider else ()
            data,_=self.auxiliary_history.with_observed(observed)
            from pineforge_live.engine.auxiliary import _BAR
            effective=sum(row[-1]<rows[-1][0]+180000 for row in _BAR.iter_unpack(data))
            assert effective==len(rows)*3,'fake verifier expects exact 1m coverage through the chart tail'
        hashes=[int(T.canonical_sha256(row)[:15],16) for row in rows]
        trades=[]
        if len(rows)>=4:
            trades=[TradeRow(rows[2][0],rows[3][0],rows[2][1],rows[3][1],3.,3.,True,1.,0.,2,3,False,'E','X','',0)]
        size=1. if len(rows)==3 else 0.
        return RunResult(0,trades,3. if trades else 0.,len(rows),hashes,size,rows[-1][1] if size else float('nan'),1,float('nan'),10000.,0)


class EmptyHandle(FakeHandle):
    def run_full(self,*args,**kwargs):
        return replace(super().run_full(*args,**kwargs),trades=[],position_size=0.,
                       position_avg_price=float('nan'),net_profit=0.)


class RepaintingHandle(FakeHandle):
    def run_full(self,bars,*args,**kwargs):
        result=super().run_full(bars,*args,**kwargs)
        if len(bars)==8:
            return replace(result,broker_state_hash=[result.broker_state_hash[0]^1,*result.broker_state_hash[1:]])
        return result


@pytest.mark.parametrize('auxiliary',[False,True])
@pytest.mark.parametrize('handle_type,expected_actions,status,corrupt_actions',[(FakeHandle,2,'passed',False),(EmptyHandle,0,'unmeasured',False),(FakeHandle,2,'failed',True),(RepaintingHandle,2,'failed',False)])
def test_verifier_runs_both_modes_real_http_and_restart_with_fake_decisions(tmp_path,monkeypatch,handle_type,expected_actions,status,corrupt_actions,auxiliary):
    """Tests verifier wiring; fake decisions never count as probe measurements."""
    import pineforge_live.signals.runtime as runtime
    if corrupt_actions:
        import pineforge_live.signals.engine as signals
        original=case.emulated_from_settle
        def doubled(*args):return [replace(fill,qty=fill.qty*2) for fill in original(*args)]
        monkeypatch.setattr(case,'emulated_from_settle',doubled)
        monkeypatch.setattr(signals,'emulated_from_settle',doubled)
    monkeypatch.setattr(case,'EngineHandle',handle_type)
    monkeypatch.setattr(runtime,'EngineHandle',handle_type)
    monkeypatch.setattr(case,'apply_epoch',lambda *args:None)
    monkeypatch.setattr(runtime,'apply_epoch',lambda *args:None)
    monkeypatch.setitem(sys.modules,'verify_routing',SimpleNamespace(pine_input_overrides_from_document=lambda x:{}))
    monkeypatch.setitem(sys.modules,'source_trade_provenance',SimpleNamespace(validate_source_trade_pairing=lambda x:None))
    so=tmp_path/'fixture.so';so.write_bytes(b'FAKE LIBRARY - NEVER LOADED')
    monkeypatch.setattr(case,'compile_strategy',lambda x:so)
    source=tmp_path/'fixture.pine';source.write_text('// FAKE - never transpiled\n'+('value = request.security(syminfo.tickerid, "1", close)' if auxiliary else ''))
    minutes=[minute(i) for i in range(24)]
    agg=MinuteBarAggregator('3');parents=[]
    for m in minutes:parents.extend(agg.push(m))
    chart=tmp_path/'chart.csv';finer=tmp_path/'finer.csv'
    case.write_bars(chart,parents);case.write_bars(finer,minutes)
    output=tmp_path/'output';output.mkdir()
    doc={'output':str(output),'evidence':{'strategy':str(source)},'lab':str(tmp_path),'engine':str(tmp_path),
         'probe':{'probe_id':'offline-fixture','symbol':'TEST:MOCK','timeframe':'3'},'template':{'environment':{}},
         'feeds':{'chart':str(chart),'finer':str(finer)},'replay_bars':2,'daily_replay_bars':2,
         'tick_policies':['high-first','low-first'],'seed':7}
    result=case.verify(doc)
    assert result['status']==status,result
    if handle_type is RepaintingHandle:assert result['native_prefix_mismatches']==[0]
    assert result['native_chart_equal'] and result['batch_actions_in_window']==expected_actions
    assert all(x['closed_trade_exit_quantity_equal'] is (not corrupt_actions) for x in result['modes'].values())
    assert set(result['modes'])=={'bars-direct','ticks-high-first','ticks-low-first'}
    assert all(x['actions']==expected_actions and x['restart_deliveries']==0 and x['input_minutes']==6 for x in result['modes'].values())


@pytest.mark.parametrize('metadata,chart_clock',[({},''),({'chart_timezone':''},''),({'chart_timezone':'Asia/Taipei'},'Asia/Taipei')])
def test_verifier_keeps_pinned_campaign_chart_default_separate_from_exchange(tmp_path,monkeypatch,metadata,chart_clock):
    # Pinned campaign run_strategy --chart-tz defaults to empty; lab forwards
    # lane timezone as syminfo only. Explicit probe chart clock wins.
    from pineforge_live.config import load_signal_config
    monkeypatch.setitem(sys.modules,'verify_routing',SimpleNamespace(pine_input_overrides_from_document=lambda x:{}))
    library=tmp_path/'fake.so';library.write_bytes(b'not executed')
    source=tmp_path/'strategy.pine';source.write_text('// fixture')
    history=tmp_path/'history.csv';case.write_bars(history,[minute(0),minute(3)])
    events=tmp_path/'events.jsonl';events.write_text('')
    calendar=tmp_path/'calendar.json';calendar.write_text(json.dumps([[0,180000],[180000,360000],[360000,540000]]))
    fixture={'lab':str(tmp_path),'probe':{'symbol':'NYSE:TEST','probe_id':'clock-fixture','timeframe':'3'},
             'template':{'environment':{'PINEFORGE_VERIFY_TIMEZONE':'America/New_York'}},'evidence':{'strategy':str(source)}}
    doc=case.config_base(fixture,library,metadata,calendar,history,events,tmp_path/'signals.sqlite3','http://127.0.0.1:1/webhook')
    path=tmp_path/'config.json';path.write_text(json.dumps(doc))
    c=load_signal_config(path)
    assert c.epoch.setter_sequence()[:2]==[('set_chart_timezone',(chart_clock,)),('set_syminfo_timezone',('America/New_York',))]
