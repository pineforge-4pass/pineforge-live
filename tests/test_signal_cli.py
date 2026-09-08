import json

from pineforge_live.cli import main
import pytest
from tests.test_signal_runtime import document, receiver as _receiver

receiver = pytest.fixture(_receiver.__wrapped__)


def test_run_and_check_commands_use_same_durable_webhook_state(test_so,test_feed,tmp_path,receiver,capsys):
    port,received=receiver
    path,_=document(test_so,test_feed,tmp_path,port)
    assert main(['run','--config',str(path)])==0
    first=json.loads(capsys.readouterr().out)
    assert first['delivered']==2 and len(received)==2
    assert main(['check','--config',str(path)])==0
    second=json.loads(capsys.readouterr().out)
    assert second['delivered']==0 and len(received)==2
    assert main(['webhook-inspect','--config',str(path)])==0
    rows=json.loads(capsys.readouterr().out)
    assert len(rows)==2 and all(r['state']=='DELIVERED' for r in rows)
    assert main(['webhook-flush','--config',str(path)])==0
    assert json.loads(capsys.readouterr().out)['delivered']==0


def test_check_http_snapshot_is_finite_and_replay_safe(test_so,test_feed,tmp_path,receiver,capsys):
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    port,received=receiver
    path,_=document(test_so,test_feed,tmp_path,port)
    frames=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw=json.dumps(frames).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        d=json.loads(path.read_text());d['source']={'kind':'http','url':f'http://127.0.0.1:{server.server_port}/events'}
        path.write_text(json.dumps(d))
        assert main(['check','--config',str(path)])==0
        report=json.loads(capsys.readouterr().out)
        assert report['source_events']==12 and report['delivered']==2
        assert len(received)==2
    finally:
        server.shutdown();server.server_close();thread.join()


def test_webhook_recovery_uses_stored_epoch_without_strategy_artifacts(test_so,test_feed,tmp_path,receiver,capsys):
    from pineforge_live.config import load_signal_config
    from pineforge_live.journal import Journal
    port,_=receiver;path,_=document(test_so,test_feed,tmp_path,port)
    config=load_signal_config(path)
    assert main(['check','--config',str(path)])==0
    capsys.readouterr()
    d=json.loads(path.read_text());d['strategy_path']='removed-strategy.so';d['history_path']='removed-history.csv'
    path.write_text(json.dumps(d))
    assert main(['webhook-inspect','--config',str(path)])==0
    rows=json.loads(capsys.readouterr().out)
    assert len(rows)==2
    j=Journal.open(config.journal_path)
    assert j._exec('SELECT COUNT(*) FROM webhook_targets').fetchone()[0]==1
    j.close()
    assert main(['webhook-flush','--config',str(path)])==0
