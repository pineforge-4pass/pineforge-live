"""Public webhook signals derive solely from the engine, not venue echoes."""

import pytest

from pineforge_live.adapters.tape import TapeTickSource
from pineforge_live.bars import FormingBarBuilder
from pineforge_live.core.book import book_diff
from pineforge_live.core.classify import emulated_from_settle
from pineforge_live.core.ledger import RecomputeAborted
from pineforge_live.harness import tape_spec,make_handle
from pineforge_live.journal import Journal,StopMarker,JournalCorrupt
from pineforge_live.signals.engine import SignalEngine,SignalRecoveryRequired
from pineforge_live.webhooks.store import Outbox
from tests.helpers import load_bars


def setup(so,path,mode='settled'):
    spec=tape_spec(so=so)
    handle=make_handle(so,spec)
    j=Journal.open(path/'j.sqlite3')
    marker=StopMarker(path/'marker.stop');marker.prepare()
    outbox=Outbox(j,spec.epoch_hash(),'http://localhost:9999/webhook')
    return SignalEngine(handle,spec,j,marker,outbox,strategy_name='strategy',mode=mode)


@pytest.mark.parametrize('fixture',['test_so','test_so_bracket','test_so_pooc'])
def test_settled_signals_equal_backtest_fill_sequence(request,fixture,test_feed,tmp_path):
    so=request.getfixturevalue(fixture)
    engine=setup(so,tmp_path)
    bars=load_bars(test_feed,2200)
    engine.seed(bars[:2000])
    assert engine.outbox.inspect()==[]
    expected=[]
    for bar in bars[2000:]:
        previous=engine.ledger.last
        result=engine.settle(bar,bar.ts_open+900_000)
        assert result.completed
        for f in emulated_from_settle(engine.ledger.last,book_diff(previous.book,engine.ledger.last.book),previous.book):
            expected.append((f.bar_index,f.intent,f.leg.lower(),f.qty,f.price))
    actual=[(r['payload']['bar']['index'],r['payload']['order']['id'],r['payload']['order']['leg'],
             r['payload']['order']['contracts'],r['payload']['order']['price']) for r in engine.outbox.inspect()]
    assert actual==expected
    assert all(r['payload']['event']=='order_action' and r['payload']['status']=='confirmed' for r in engine.outbox.inspect())
    engine.h.close();engine.j.close()


def test_restart_does_not_reemit_settled_actions(test_so,test_feed,tmp_path):
    engine=setup(test_so,tmp_path);bars=load_bars(test_feed,2003)
    engine.seed(bars[:2000])
    for b in bars[2000:2002]:engine.settle(b,b.ts_open+900_000)
    before=engine.outbox.inspect();assert len(before)==2
    engine.h.close();engine.j.close()
    reborn=setup(test_so,tmp_path)
    reborn.seed(bars[:2002])
    repeated=reborn.settle(bars[2001],bars[2001].ts_open+900_000)
    assert repeated.duplicate and not repeated.event_ids
    assert reborn.outbox.inspect()==before
    reborn.h.close();reborn.j.close()


def test_atomic_signal_commit_failure_rolls_back_settlement_and_outbox(test_so,test_feed,tmp_path,monkeypatch):
    engine=setup(test_so,tmp_path);bars=load_bars(test_feed,2002)
    engine.seed(bars[:2000]);engine.settle(bars[2000],bars[2000].ts_open+900_000)
    monkeypatch.setattr(engine,'_save',lambda: (_ for _ in ()).throw(RuntimeError('crash after enqueue')))
    with pytest.raises(RuntimeError):engine.settle(bars[2001],bars[2001].ts_open+900_000)
    assert engine.j.last_settlement(engine.epoch)['bar_index']==2000
    assert engine.outbox.inspect()==[]
    assert engine.poisoned and engine.marker.exists()
    with pytest.raises(SignalRecoveryRequired):engine.settle(bars[2001],bars[2001].ts_open+900_000)
    engine.h.close();engine.j.close()


def test_aborted_recompute_can_retry_without_consuming_an_alert(test_so,test_feed,tmp_path,monkeypatch):
    engine=setup(test_so,tmp_path);bars=load_bars(test_feed,2002)
    engine.seed(bars[:2000])
    original=engine.ledger.settle
    monkeypatch.setattr(engine.ledger,'settle',lambda *a: (_ for _ in ()).throw(RecomputeAborted()))
    assert not engine.settle(bars[2000],0).completed
    assert not engine.poisoned and engine.outbox.inspect()==[]
    monkeypatch.setattr(engine.ledger,'settle',original)
    assert engine.settle(bars[2000],0).completed
    engine.h.close();engine.j.close()


def test_intrabar_emits_one_action_then_confirmation_across_restart(test_so,test_feed,tmp_path):
    import asyncio
    engine=setup(test_so,tmp_path,'intrabar');bars=load_bars(test_feed,2002)
    engine.seed(bars[:2000]);engine.settle(bars[2000],bars[2000].ts_open+900_000)
    source=TapeTickSource([bars[2001]],'15',policy='path4',seed=1)
    async def ticks():return [e.tick async for e in source.subscribe(engine.spec.instrument,0)]
    points=asyncio.run(ticks());builder=FormingBarBuilder('15')
    builder.push(points[0]);engine.evaluate(builder.forming(),points[0].ts,last_seq=points[0].seq)
    actions=[r for r in engine.outbox.inspect() if r['payload']['event']=='order_action']
    assert len(actions)==2
    ids=[r['event_id'] for r in actions]
    engine.h.close();engine.j.close()
    engine=setup(test_so,tmp_path,'intrabar');engine.seed(bars[:2001])
    engine.evaluate(builder.forming(),points[0].ts,last_seq=points[0].seq)
    for t in points[1:]:
        builder.push(t);engine.evaluate(builder.forming(),t.ts,last_seq=t.seq)
    engine.settle(bars[2001],bars[2001].ts_open+900_000)
    rows=engine.outbox.inspect()
    assert [r['event_id'] for r in rows if r['payload']['event']=='order_action']==ids
    confirms=[r['payload'] for r in rows if r['payload']['event']=='order_update' and r['payload']['status']=='confirmed']
    assert {r['original_event_id'] for r in confirms}==set(ids)
    assert all(r['bar']['confirmed'] for r in confirms)
    engine.h.close();engine.j.close()


def test_checkpoint_corruption_refuses_without_rewriting_it(test_so,test_feed,tmp_path):
    engine=setup(test_so,tmp_path);bars=load_bars(test_feed,2000);engine.seed(bars)
    engine.j._exec("UPDATE signal_checkpoints SET payload_json='{}'")
    with pytest.raises(JournalCorrupt):engine.checkpoint_bar_index
    assert engine.poisoned
    assert engine.j._exec('SELECT payload_json FROM signal_checkpoints').fetchone()[0]=='{}'
    engine.h.close();engine.j.close()


def test_cpp_recompute_does_not_hold_sqlite_writer_lock(test_so,test_feed,tmp_path,monkeypatch):
    engine=setup(test_so,tmp_path,'intrabar');bars=load_bars(test_feed,2002)
    original=engine.h.run_full
    seen=[]
    def checked(*args,**kwargs):
        assert not engine.j.con.in_transaction
        other=Journal.open(engine.j.path)
        try:
            with other.transaction():
                other.append_incident('heartbeat_can_write',{})
        finally:other.close()
        seen.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(engine.h,'run_full',checked)
    engine.seed(bars[:2000])
    from dataclasses import replace
    engine.evaluate(replace(bars[2000],is_forming=True),bars[2000].ts_open)
    engine.settle(bars[2000],bars[2000].ts_open+900_000)
    assert len(seen)>=3
    engine.h.close();engine.j.close()


def test_lost_authority_between_recompute_and_commit_sends_nothing(test_so,test_feed,tmp_path,monkeypatch):
    engine=setup(test_so,tmp_path);bars=load_bars(test_feed,2002)
    engine.seed(bars[:2000]);engine.settle(bars[2000],bars[2000].ts_open+900_000)
    engine.authority=lambda:False
    with pytest.raises(SignalRecoveryRequired):engine.settle(bars[2001],bars[2001].ts_open+900_000)
    assert engine.outbox.inspect()==[]
    assert engine.j.last_settlement(engine.epoch)['bar_index']==2000
    assert not engine.marker.exists() # expired owner must not mutate a new owner's STOP
    engine.h.close();engine.j.close()


@pytest.mark.parametrize('price',[0.0,-10.0])
def test_neutral_signal_forwards_finite_nonpositive_engine_reference_price(price):
    from types import SimpleNamespace
    fill=SimpleNamespace(qty=1.,price=price,intent='L',leg='ENTRY',is_long=True)
    assert SignalEngine._order(None,fill)['price']==price
