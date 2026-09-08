"""Real HTTP webhook E2E through the compiled strategy and public runtime."""
import asyncio
import csv
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest

from pineforge_live.config import load_signal_config
from pineforge_live.signals.runtime import run_signals
from tests.helpers import load_bars
from tests.test_webhook_config import config_document


def document(test_so,test_feed,tmp_path,port,*,trigger_mode='settled'):
    bars=load_bars(test_feed,2200)
    path=tmp_path/'config.json'
    d=config_document(tmp_path)
    d.update(strategy_path=str(test_so),strategy_name='public strategy',script_tf='15',
             trigger_mode=trigger_mode,source={'kind':'jsonl','path':'events.jsonl'})
    d['webhook']={'target_url':f'http://127.0.0.1:{port}/webhook','backoff_initial_ms':1,'backoff_max_ms':1}
    d['instrument']={'venue':'TAPE','market_type':'perp','symbol':'ETHUSDT'}
    from pineforge_live.harness import tape_syminfo
    d['syminfo']=asdict(tape_syminfo())
    with (tmp_path/'history.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['timestamp','open','high','low','close','volume'])
        w.writerows(b.ohlcv() for b in bars[:2000])
    events=[]
    for b in bars[2000:2012]:
        raw=asdict(b);raw.pop('is_forming')
        events.append({'type':'bar','bar':raw})
    (tmp_path/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    path.write_text(json.dumps(d))
    return path,bars


@pytest.fixture
def receiver():
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            received.append((dict(self.headers),json.loads(body)))
            self.send_response(200);self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield server.server_port,received
    server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('mode',['stream','check'])
def test_public_runtime_delivers_real_http_without_broker_and_dedups_restart(test_so,test_feed,tmp_path,receiver,mode):
    port,received=receiver
    path,bars=document(test_so,test_feed,tmp_path,port)
    c=load_signal_config(path)
    report=asyncio.run(run_signals(c,mode=mode))
    assert report['error'] is None,report
    assert report['settled_bars']==12 and report['emitted']==report['delivered']==2
    assert len(received)==2
    assert {r[1]['order']['leg'] for r in received}=={'entry','exit'}
    for headers,payload in received:
        assert payload['event']=='order_action'
        assert headers['Idempotency-Key']==payload['event_id']
    report2=asyncio.run(run_signals(c,mode=mode))
    assert report2['error'] is None,report2
    assert report2['emitted']==report2['delivered']==0 and len(received)==2
    assert json.loads(c.journal_path.with_suffix('.report.json').read_text())==report2


def test_partial_forming_snapshot_can_grow_into_confirmed_bar(test_so,test_feed,tmp_path,receiver):
    port,_=receiver;path,bars=document(test_so,test_feed,tmp_path,port)
    b=bars[2000]
    forming={'type':'forming','bar':{'ts_open':b.ts_open,'o':b.o,'h':b.o,'l':b.o,'c':b.o,'v':0}}
    existing=(tmp_path/'events.jsonl').read_text()
    (tmp_path/'events.jsonl').write_text(json.dumps(forming)+'\n'+existing)
    report=asyncio.run(run_signals(load_signal_config(path),mode='check'))
    assert report['error'] is None,report


def test_failed_delivery_retained_and_retry_does_not_recompute_action(test_so,test_feed,tmp_path):
    from pineforge_live.webhooks.delivery import HttpResponse
    from pineforge_live.journal import Journal
    from pineforge_live.webhooks.store import Outbox
    class Transport:
        def __init__(self):self.status=400;self.bodies=[]
        async def post(self,url,body,headers,timeout_ms):
            self.bodies.append(body);return HttpResponse(self.status)
    transport=Transport();path,bars=document(test_so,test_feed,tmp_path,9999)
    c=load_signal_config(path)
    report=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert report['failed']==1 and report['error']
    j=Journal.open(c.journal_path);o=Outbox(j,c.epoch.epoch_hash(),c.webhook.target_url)
    event=o.inspect()[0];o.retry(event['event_id'],now_ms=0);j.close()
    transport.status=200
    report2=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert report2['error'] is None,report2
    assert transport.bodies[0]==transport.bodies[1]
    assert len(transport.bodies)==3 # failed first, its retry, sibling entry


def test_gap_stops_and_does_not_send_new_alerts(test_so,test_feed,tmp_path,receiver):
    port,received=receiver;path,bars=document(test_so,test_feed,tmp_path,port)
    b=bars[2000]
    events=[{'type':'tick','ts':b.ts_open,'seq':1,'price':b.o,'qty':1},
            {'type':'tick','ts':b.ts_open+1,'seq':3,'price':b.o,'qty':1}]
    (tmp_path/'events.jsonl').write_text('\n'.join(map(json.dumps,events))+'\n')
    c=load_signal_config(path);report=asyncio.run(run_signals(c,mode='check'))
    assert 'gap' in report['error'].lower()
    assert not received
    assert c.journal_path.with_name(c.journal_path.name+'.stop').exists()


def test_restart_preserves_tick_origin_and_rejects_conflicting_final_bar(test_so,test_feed,tmp_path,receiver):
    port,_=receiver;path,bars=document(test_so,test_feed,tmp_path,port)
    b=bars[2000]
    tick={'type':'tick','ts':b.ts_open,'seq':1,'price':b.o,'qty':1}
    (tmp_path/'events.jsonl').write_text(json.dumps(tick)+'\n')
    c=load_signal_config(path)
    first=asyncio.run(run_signals(c,mode='check'))
    assert first['error'] is None
    bar={'type':'bar','bar':{'ts_open':b.ts_open,'o':b.o,'h':b.o+1,'l':b.o,'c':b.o+1,'v':1}}
    (tmp_path/'events.jsonl').write_text(json.dumps(bar)+'\n')
    second=asyncio.run(run_signals(c,mode='check'))
    assert 'disagrees' in second['error']


@pytest.mark.parametrize('same_sequence',[False,True])
def test_tick_timestamp_or_duplicate_conflict_after_restart_is_refused(test_so,test_feed,tmp_path,receiver,same_sequence):
    port,_=receiver;path,bars=document(test_so,test_feed,tmp_path,port);b=bars[2000]
    event={'type':'tick','ts':b.ts_open+1000,'seq':1,'price':b.o,'qty':1}
    (tmp_path/'events.jsonl').write_text(json.dumps(event)+'\n');c=load_signal_config(path)
    assert asyncio.run(run_signals(c,mode='check'))['error'] is None
    event.update(ts=b.ts_open+500,seq=1 if same_sequence else 2)
    (tmp_path/'events.jsonl').write_text(json.dumps(event)+'\n')
    report=asyncio.run(run_signals(c,mode='check'))
    assert report['error'] and report['delivered']==0
    assert c.journal_path.with_name(c.journal_path.name+'.stop').exists()


def test_existing_stop_overwrites_previous_success_report(test_so,test_feed,tmp_path,receiver):
    from pineforge_live.journal import StopMarker
    port,_=receiver;path,_=document(test_so,test_feed,tmp_path,port);c=load_signal_config(path)
    assert asyncio.run(run_signals(c,mode='check'))['error'] is None
    StopMarker(str(c.journal_path)+'.stop').write('HARD','HOLD','test stop')
    report=asyncio.run(run_signals(c,mode='check'))
    assert report['error'] and report['phase']=='startup'
    assert json.loads(c.journal_path.with_suffix('.report.json').read_text())==report


def test_real_websocket_feed_runs_cpp_and_delivers_webhooks(test_so,test_feed,tmp_path,receiver):
    websockets=pytest.importorskip('websockets')
    port,received=receiver;path,_=document(test_so,test_feed,tmp_path,port)
    frames=(tmp_path/'events.jsonl').read_text().splitlines()
    async def scenario():
        async def feed(socket):
            for frame in frames:
                await socket.send(frame)
                await asyncio.sleep(.002)
            await socket.wait_closed()
        async with websockets.serve(feed,'127.0.0.1',0) as server:
            ws_port=server.sockets[0].getsockname()[1]
            d=json.loads(path.read_text());d['source']={'kind':'websocket','url':f'ws://127.0.0.1:{ws_port}'}
            path.write_text(json.dumps(d))
            return await asyncio.wait_for(run_signals(load_signal_config(path),max_events=len(frames)),10)
    report=asyncio.run(scenario())
    assert report['error'] is None,report
    assert report['source_events']==12 and report['delivered']==2
    assert len(received)==2
