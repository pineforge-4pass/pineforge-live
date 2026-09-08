"""One registry-sourced two-input measurement, executed only by Cloud Run.

Three independent observations are retained: 1m/native-chart bar identity,
live-vs-full-batch identity, and HTTP webhook accounting. Optional canonical
TV grading is diagnostic (the campaign's optimization ladder is not rerun).
"""
from __future__ import annotations

import asyncio
from bisect import bisect_left
import csv
from dataclasses import asdict,is_dataclass
import enum
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from pineforge_live import types as T
from pineforge_live.adapters.synthetic import SyntheticMinuteTicks
from pineforge_live.bars.minute import MinuteBarAggregator
from pineforge_live.bars.builder import compare_bar
from pineforge_live.bars.policy import tf_ms
from decimal import Decimal
from pineforge_live.config import load_signal_config
from pineforge_live.core.book import book_diff
from pineforge_live.core.classify import emulated_from_settle
from pineforge_live.core.ledger import Ledger
from pineforge_live.core.ids import trades_sha256
from pineforge_live.engine import EngineHandle
from pineforge_live.epoch import apply_epoch
from pineforge_live.journal import Journal
from pineforge_live.signals.runtime import run_signals
from .cloud_io import canonical_json_bytes,file_identity


def clean(value):
    if is_dataclass(value):return clean(asdict(value))
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean(v) for v in value]
    if isinstance(value,enum.Enum):return value.value
    if isinstance(value,Path):return str(value)
    if isinstance(value,float) and not math.isfinite(value):return None
    return value


def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);sys.modules[name]=result;spec.loader.exec_module(result)
    return result


def read_bars(path,start=None,end=None):
    bars=[]
    with Path(path).open() as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames!=['timestamp','open','high','low','close','volume']:
            raise ValueError('feed header is not canonical OHLCV')
        previous=None
        for row in reader:
            stamp=int(row['timestamp'])
            if previous is not None and stamp<=previous:raise ValueError('feed timestamp order invalid')
            previous=stamp
            if start is not None and stamp<start:continue
            if end is not None and stamp>=end:break
            bar=T.NormalizedBar(stamp,*(float(row[k]) for k in ('open','high','low','close','volume')),0)
            if stamp<0 or stamp%60_000 or not all(math.isfinite(x) for x in bar.ohlcv()[1:]) or bar.v<0 or bar.h<max(bar.o,bar.c,bar.l) or bar.l>min(bar.o,bar.c,bar.h):
                raise ValueError('invalid feed OHLCV')
            bars.append(bar)
    return bars


def timestamps(path):
    with Path(path).open() as stream:
        next(stream)
        result=[int(line.split(',',1)[0]) for line in stream if line.strip()]
    if any(b<=a for a,b in zip(result,result[1:])):raise ValueError('minute timestamps not strictly ordered')
    return result


def replay_calendar(chart,minute_ts,script_tf):
    """Replay-only schedule from timestamps, never prices or future actions.

    Intraday windows cannot extend past one script interval. Session daily
    windows finish at the last supplied minute before the next native open.
    Missing constituent minutes remain a refusal in the runtime aggregator.
    """
    width=tf_ms(script_tf);result=[]
    for index,bar in enumerate(chart):
        next_open=chart[index+1].ts_open if index+1<len(chart) else bar.ts_open+width
        bound=min(next_open,bar.ts_open+width) if width<86_400_000 else next_open
        low=bisect_left(minute_ts,bar.ts_open);high=bisect_left(minute_ts,bound)
        close=minute_ts[high-1]+60_000 if high>low else min(bound,bar.ts_open+width)
        if close<=bar.ts_open:raise ValueError('invalid timestamp-derived parent window')
        result.append((bar.ts_open,close))
    return result


def choose_window(chart,minute_ts,calendar,trades,count):
    eligible=[]
    for i,(start,end) in enumerate(calendar):
        left=bisect_left(minute_ts,start);right=bisect_left(minute_ts,end)
        if (left<right and minute_ts[left]==start and minute_ts[right-1]+60_000==end
                and right-left==(end-start)//60_000):eligible.append(i)
    available=set(eligible)
    trade_ends=sorted({t.exit_bar_index for t in trades if not t.open_at_end and t.exit_bar_index in available},reverse=True)
    ends=trade_ends+list(reversed(eligible))
    for end in ends:
        # One following timestamp is retained so the run's calendar can
        # represent its next forming bar without guessing a session opening.
        if end>=len(chart)-1:continue
        start=end-count+1
        if start>=1 and all(i in available for i in range(start,end+1)):
            return start,end+1
    raise ValueError('no complete requested window shared by chart and minute feeds')


def _run(command,cwd=None,timeout=300):
    result=subprocess.run(command,cwd=cwd,text=True,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError(result.stderr[-6000:])
    return result.stdout


def compile_strategy(case):
    build=Path(case['build_dir']);build.mkdir(parents=True,exist_ok=True)
    source=Path(case['evidence']['strategy']).read_text()
    sys.path.insert(0,case['codegen'])
    from pineforge_codegen import transpile
    cpp=transpile(source)
    output=build/'generated.cpp';library=build/'strategy.so'
    expected=hashlib.sha256(cpp.encode()).hexdigest()
    if library.exists() and output.exists() and file_identity(output)['sha256']==expected:return library
    output.write_text(cpp)
    engine=Path(case['engine'])
    _run(['g++','-std=c++17','-O1','-fPIC','-I',str(engine/'include'),'-I',str(engine/'build/include'),
          '-c',str(output),'-o',str(build/'generated.o')],timeout=300)
    _run(['g++','-shared','-o',str(library),str(build/'generated.o'),'-Wl,--whole-archive',
          str(engine/'build/lib/libpineforge.a'),'-Wl,--no-whole-archive'],timeout=120)
    return library


def materialize_evidence(case,root):
    root.mkdir(parents=True,exist_ok=True)
    names={'strategy':'strategy.pine','tvTrades':'tv_trades.csv','metrics':'metrics.json','meta':'meta.json','inputs':'inputs.json'}
    for kind,path in case['evidence'].items():shutil.copyfile(path,root/names[kind])
    return json.loads((root/'inputs.json').read_text()) if (root/'inputs.json').exists() else {}


def config_base(case,library,metadata,calendar,history_path,source_path,journal_path,url):
    env=case['template']['environment'];symbol=case['probe']['symbol'];prefix,_,ticker=symbol.partition(':')
    sys.path.insert(0,str(Path(case['lab'])/'scripts'))
    import verify_routing
    inputs=verify_routing.pine_input_overrides_from_document(metadata)
    runtime=dict(metadata.get('runtime_overrides') or {})
    numeric=dict(runtime.get('syminfo_metadata') or {})
    for key,name in [('PINEFORGE_VERIFY_MARGIN_LONG','margin_long'),('PINEFORGE_VERIFY_MARGIN_SHORT','margin_short')]:
        if env.get(key):numeric[name]=float(env[key])
    numeric['qty_step']=float(env.get('PINEFORGE_VERIFY_QTY_STEP',runtime.get('qty_step',.0001)))
    tick=float(env.get('PINEFORGE_VERIFY_MINTICK',runtime.get('mintick',.01)))
    # Currency and empty basecurrency preserve constructor defaults.
    # pricescale/minmove are derived live metadata (unused by this population).
    scale=10**max(0,-Decimal(str(tick)).normalize().as_tuple().exponent)
    minmove=max(1,round(tick*scale))
    kind=env.get('PINEFORGE_VERIFY_SYMTYPE',runtime.get('type','crypto'))
    syminfo={'ticker':ticker or prefix,'tickerid':symbol,'prefix':prefix,'root':'',
             'type':kind,'currency':runtime.get('currency','USD'),'basecurrency':runtime.get('basecurrency',''),
             'mintick':tick,'pricescale':scale,'pointvalue':float(env.get('PINEFORGE_VERIFY_POINT_VALUE',runtime.get('pointvalue',1))),
             'minmove':minmove,'session':env.get('PINEFORGE_VERIFY_SESSION',runtime.get('session','24x7')),
             'timezone':env.get('PINEFORGE_VERIFY_TIMEZONE',runtime.get('timezone','UTC')) or 'UTC',
             'volumetype':runtime.get('volumetype','base'),'description':runtime.get('description',''),
             'numeric_metadata':{k:float(v) for k,v in numeric.items() if k not in ('pricescale','minmove')}}
    overrides=metadata.get('strategy_overrides') or {}
    return {'strategy_path':str(library),'strategy_name':case['probe']['probe_id'],
            'strategy_source_path':case['evidence']['strategy'],'history_path':str(history_path),'journal_path':str(journal_path),
            'script_tf':case['probe']['timeframe'],'input_tf':'1','input_mode':'bars','parent_windows_path':str(calendar),
            # The pinned campaign CLI defaults --chart-tz to empty (UTC);
            # its lane syminfo timezone setter does not change that clock.
            'chart_timezone':metadata.get('chart_timezone',''),
            'instrument':{'venue':prefix,'market_type':'future' if kind=='futures' else 'spot','symbol':ticker or prefix},
            'syminfo':syminfo,'inputs':list(inputs.items()),'overrides':[[str(k),str(v).lower() if isinstance(v,bool) else str(v)] for k,v in overrides.items()],
            'trigger_mode':'settled','horizon_bars':1_000_000,
            'webhook':{'target_url':url,'backoff_initial_ms':1,'backoff_max_ms':1},
            'source':{'kind':'jsonl','path':str(source_path)}}


def write_bars(path,bars):
    with Path(path).open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['timestamp','open','high','low','close','volume'])
        writer.writerows(b.ohlcv() for b in bars)


def write_events(path,minutes,script_tf,calendar,mode,policy,seed):
    generator=SyntheticMinuteTicks(policy,seed=seed,parent_windows=calendar) if mode=='ticks' else None
    with Path(path).open('w') as stream:
        for bar in minutes:
            if mode=='ticks':
                for tick in generator.push(bar).ticks:
                    stream.write(json.dumps({'type':'tick','ts':tick.ts,'seq':tick.seq,'price':tick.price,'qty':tick.qty},separators=(',',':'))+'\n')
            row=asdict(bar);row.pop('is_forming')
            stream.write(json.dumps({'type':'bar','bar':row},separators=(',',':'))+'\n')


def action_projection(row):
    p=row['payload'] if 'payload' in row else row
    o=p['order']
    return {'bar_index':p['bar']['index'],'bar_time':p['bar']['time'],'id':o['id'],'leg':o['leg'],
            'action':o['action'],'contracts':o['contracts'],'price':o['price'],'reduce_only':o['reduce_only']}


def verify(case):
    output=Path(case['output']);evidence=output/'original';metadata=materialize_evidence(case,evidence)
    sys.path.insert(0,str(Path(case['lab'])/'scripts'))
    from source_trade_provenance import validate_source_trade_pairing
    error=validate_source_trade_pairing(evidence)
    if error:raise ValueError('source-trade provenance: '+str(error))
    library=compile_strategy(case)
    chart=read_bars(case['feeds']['chart'],start=metadata.get('ohlcv_start_ms'));minute_ts=timestamps(case['feeds']['finer'])
    calendar=replay_calendar(chart,minute_ts,case['probe']['timeframe'])
    calendar_path=output/'calendar.json';calendar_path.write_bytes(canonical_json_bytes([{'open_ms':a,'close_ms':b} for a,b in calendar]))
    # Configuration/control stage computes the full native chart once and
    # chooses a bounded replay window around a real engine closed trade.
    history_path=output/'history.csv';write_bars(history_path,chart[:1])
    source_path=output/'empty.jsonl';source_path.write_text('')
    base=config_base(case,library,metadata,calendar_path,history_path,source_path,output/'control.sqlite3','http://127.0.0.1:1/webhook')
    config_path=output/'control-config.json';config_path.write_bytes(canonical_json_bytes(base))
    config=load_signal_config(config_path)
    with EngineHandle(library) as handle:
        apply_epoch(handle,config.epoch);native=handle.run_full(chart,config.script_tf)
        if native.status!=0:raise RuntimeError('native reference aborted')
    count=case['daily_replay_bars'] if tf_ms(config.script_tf)>=86_400_000 else case['replay_bars']
    start,end=choose_window(chart,minute_ts,calendar,native.trades,count)
    minutes=read_bars(case['feeds']['finer'],calendar[start][0],calendar[end-1][1])
    # Limit input to declared active windows; no quotes from a closed session
    # are injected into a parent merely because they fall between sessions.
    active=calendar[start:end]
    minutes=[b for b in minutes if any(a<=b.ts_open<z for a,z in active)]
    agg=MinuteBarAggregator(config.script_tf,parent_windows=calendar)
    derived=[]
    for b in minutes:derived.extend(agg.push(b))
    if len(derived)!=end-start:raise ValueError('trailing incomplete parent in selected minute window')
    bar_differences=[{'index':start+i,'fields':compare_bar(expected,actual),'native':expected.ohlcv(),'derived':actual.ohlcv()}
                     for i,(expected,actual) in enumerate(zip(chart[start:end],derived)) if compare_bar(expected,actual)]
    history=chart[:start];write_bars(history_path,history)
    config=load_signal_config(config_path)
    reference_bars=history+derived
    with EngineHandle(library) as handle:
        apply_epoch(handle,config.epoch);reference=handle.run_full(reference_bars,config.script_tf)
        if reference.status!=0:raise RuntimeError('batch reference aborted')
    # Independently feed already-formed parent bars into the batch ledger
    # projection. No synthetic tick or webhook code contributes to this oracle.
    expected=[]
    with EngineHandle(library) as handle:
        apply_epoch(handle,config.epoch)
        j=Journal.open(output/'projection.sqlite3')
        ledger=Ledger(handle,config.epoch,j,config.config_hash);ledger.seed(history)
        for bar in derived:
            previous=ledger.last;s=ledger.settle(bar,calendar[ledger.n][1])
            for f in emulated_from_settle(s,book_diff(previous.book,s.book),previous.book):
                side='buy' if (f.is_long if f.leg=='ENTRY' else not f.is_long) else 'sell'
                expected.append({'bar_index':s.bar_index,'bar_time':bar.ts_open,'id':None if f.intent in ('?','') else f.intent,
                                 'leg':f.leg.lower(),'action':side,'contracts':f.qty,'price':f.price,'reduce_only':f.leg=='EXIT'})
        j.close()
    (output/'batch-trades.json').write_bytes(canonical_json_bytes(clean(reference.trades)))
    (output/'expected-actions.json').write_bytes(canonical_json_bytes(expected))
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']));payload=json.loads(raw)
            if self.headers.get('Idempotency-Key')!=payload['event_id']:
                self.send_response(400);self.end_headers();return
            received.append(payload);self.send_response(200);self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    modes={}
    try:
        for mode,policy in [('bars','direct')]+[('ticks',p) for p in case['tick_policies']]:
            name=mode+'-'+policy;folder=output/name;folder.mkdir()
            events=folder/'events.jsonl';write_events(events,minutes,config.script_tf,calendar,mode,policy,case['seed'])
            doc=dict(base,journal_path=str(folder/'signals.sqlite3'),input_mode=mode,
                     source={'kind':'jsonl','path':str(events)},webhook={'target_url':f'http://127.0.0.1:{server.server_port}/webhook','backoff_initial_ms':1,'backoff_max_ms':1})
            path=folder/'config.json';path.write_bytes(canonical_json_bytes(doc));mode_config=load_signal_config(path)
            before=len(received);report=asyncio.run(run_signals(mode_config,mode='check'))
            actual=received[before:]
            j=Journal.open(mode_config.journal_path)
            settlements=j.rows('settlements','epoch_hash=? AND bar_index>=?',(mode_config.epoch.epoch_hash(),start))
            hash_mismatches=[r['bar_index'] for r in settlements if r['broker_state_hash']!=reference.broker_state_hash[r['bar_index']]]
            trades_mismatch=not settlements or settlements[-1]['trades_sha256']!=trades_sha256([t for t in reference.trades if not t.open_at_end])
            input_rows=j.rows('signal_input_minutes','epoch_hash=?',(mode_config.epoch.epoch_hash(),))
            j.close()
            actions=[action_projection(p) for p in actual if p['event']=='order_action']
            before_restart=len(received);restarted=asyncio.run(run_signals(mode_config,mode='check'))
            duplicate_ids=len(actual)-len({p['event_id'] for p in actual})
            entry={'runtime':report,'restart':restarted,'actions':len(actions),'expected_actions':len(expected),
                   'actions_equal':actions==expected,'hash_mismatches':hash_mismatches,'final_trades_equal':not trades_mismatch,
                   'duplicate_event_ids':duplicate_ids,'restart_deliveries':len(received)-before_restart,
                   'script_bars':len(settlements),'input_minutes':len(input_rows),'input_events_sha256':file_identity(events)['sha256']}
            entry['ok']=not report['error'] and not restarted['error'] and actions==expected and not hash_mismatches and not trades_mismatch and duplicate_ids==0 and entry['restart_deliveries']==0 and len(settlements)==len(derived) and len(input_rows)==len(minutes)
            modes[name]=entry
            (folder/'received.json').write_bytes(canonical_json_bytes(actual))
            # Keep bounded artifacts; raw generated ticks are reproducible
            # from pinned minutes/policy and are not copied into every tar.
            events.unlink()
    finally:
        server.shutdown();server.server_close();thread.join()
    grading={}
    try:
        scripts=Path(case['engine'])/'scripts';sys.path.insert(0,str(scripts))
        driver=module(scripts/'run_strategy.py','verification_run_strategy')
        grader=module(scripts/'verify_corpus.py','verification_canonical_grader')
        directory=output/'canonical-grade';shutil.copytree(evidence,directory)
        driver.write_engine_trades_csv([asdict(t) for t in native.trades],directory/'engine_trades.csv')
        grading={'scope':'full native chart, unchanged canonical rubric, fixed live configuration; campaign warmup/origin optimization ladder and TV report-window/range-end projection not rerun',
                 'result':clean(grader.analyze_strategy(directory)),'grader_sha256':file_identity(scripts/'verify_corpus.py')['sha256']}
    except Exception as exc:grading={'error':f'{type(exc).__name__}: {exc}','scope':'diagnostic only; not a campaign parity verdict'}
    ok=all(r['ok'] for r in modes.values())
    return {'status':('passed' if expected else 'unmeasured') if ok else 'failed','live_backtest_equal':ok,'native_chart_equal':not bar_differences,
            'native_chart_differences':bar_differences[:10],'native_chart_difference_count':len(bar_differences),
            'window':{'start_index':start,'end_index_exclusive':end,'history_bars':start,'replay_bars':end-start,
                      'first_minute':minutes[0].ts_open,'last_minute':minutes[-1].ts_open,'minutes':len(minutes)},
            'calendar':{'rule':'replay timestamp-derived; no prices used','sha256':file_identity(calendar_path)['sha256']},
            'modes':modes,'batch_closed_trades':sum(not t.open_at_end for t in reference.trades),
            'batch_actions_in_window':len(expected),'trading_coverage':'nonempty' if expected else 'no-actions-in-window',
            'library':file_identity(library),'canonical_diagnostic':grading,
            'configuration':{'chart_timezone':base['chart_timezone'],'syminfo':base['syminfo'],
                             'inputs':base['inputs'],'overrides':base['overrides'],'ohlcv_start_ms':metadata.get('ohlcv_start_ms'),
                             'auxiliary_feeds':'not wired in live ABI; request.security uses the supplied script bars',
                             'bar_magnifier':False,'realtime_tail':True,'horizon_bars':base['horizon_bars']},
            'fidelity':'synthetic ticks, no historical tick order or broker fill claim; live compared with batch on identical reconstructed script bars'}


def main():
    if 'CLOUD_RUN_TASK_INDEX' not in os.environ:raise RuntimeError('registry probe measurements must execute on Cloud Run')
    case=json.loads(Path(sys.argv[1]).read_text());output=Path(case['output'])
    try:result=verify(case)
    except Exception as exc:result={'status':'failed','error':f'{type(exc).__name__}: {exc}'}
    (output/'result.json').write_bytes(canonical_json_bytes(clean(result)))
    print(json.dumps({'probeId':case['probe']['probe_id'],'status':result['status'],'error':result.get('error')},sort_keys=True))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
