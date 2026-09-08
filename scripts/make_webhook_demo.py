#!/usr/bin/env python3
"""Create a runnable broker-neutral webhook demo from the public engine corpus."""
from __future__ import annotations
import argparse
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pineforge_live.adapters.tape import load_feed_csv  # noqa: E402
from pineforge_live.harness import tape_syminfo  # noqa: E402
from pineforge_live.config import load_signal_config  # noqa: E402


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--engine-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--target-url',default='http://127.0.0.1:8765/webhook')
    p.add_argument('--strategy',default='ta-sma-152-close-cross-01')
    p.add_argument('--start',type=int,default=2000)
    p.add_argument('--bars',type=int,default=200)
    p.add_argument('--unsigned',action='store_true',help='omit the signing secret setting for local experiments')
    a=p.parse_args(argv)
    if a.start<1 or a.bars<1:p.error('--start and --bars must be positive')
    if Path(a.strategy).name!=a.strategy:p.error('--strategy must be one corpus directory name')
    root=a.engine_root.expanduser().resolve()
    probe=root/'corpus/validation'/a.strategy
    library=next((probe/name for name in ('strategy.dylib','strategy.so') if (probe/name).is_file()),None)
    if library is None:p.error('compiled strategy missing; build the engine corpus first')
    feed=root/'corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv'
    bars=load_feed_csv(feed,limit=a.start+a.bars)
    if len(bars)<a.start+a.bars:p.error('feed is shorter than requested demo window')
    output=a.output.expanduser().resolve()
    output.mkdir(parents=True,exist_ok=True)
    if any((output/name).exists() for name in ('config.json','history.csv','events.jsonl','signals.sqlite3')):
        p.error('demo output files already exist; choose a fresh output directory')
    with (output/'history.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['timestamp','open','high','low','close','volume'])
        w.writerows(b.ohlcv() for b in bars[:a.start])
    with (output/'events.jsonl').open('w') as f:
        for b in bars[a.start:]:
            payload=asdict(b);payload.pop('is_forming')
            f.write(json.dumps({'type':'bar','bar':payload},separators=(',',':'))+'\n')
    config={'schema_version':1,'strategy_path':str(library),'strategy_name':a.strategy,
            'history_path':'history.csv','journal_path':'signals.sqlite3','script_tf':'15',
            'instrument':{'venue':'TAPE','market_type':'perp','symbol':'ETHUSDT'},
            'syminfo':asdict(tape_syminfo()),'trigger_mode':'settled',
            'webhook':{'target_url':a.target_url,'secret_env':None if a.unsigned else 'PINEFORGE_WEBHOOK_SECRET'},
            'source':{'kind':'jsonl','path':'events.jsonl'}}
    if (probe/'strategy.pine').is_file():config['strategy_source_path']=str(probe/'strategy.pine')
    path=output/'config.json';path.write_text(json.dumps(config,indent=2)+'\n')
    load_signal_config(path)
    print(path)
    return 0


if __name__=='__main__':raise SystemExit(main())
