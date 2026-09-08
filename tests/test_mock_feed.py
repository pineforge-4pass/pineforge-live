"""Public mock-feed CLI, using fabricated CSV fixtures only."""
import asyncio
import csv
import json
from dataclasses import asdict

import pytest

from pineforge_live.adapters.mock_feed import mock_events
from pineforge_live.cli import main
from pineforge_live.config import load_signal_config
from pineforge_live.signals.runtime import run_signals
from tests.test_minute_runtime import fake_runtime,minute,config,Transport
from tests.test_webhook_config import config_document,write_config


def csv_feed(tmp_path,bars):
    path=tmp_path/'minutes.csv'
    with path.open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['timestamp','open','high','low','close','volume'])
        writer.writerows(bar.ohlcv() for bar in bars)
    return path


@pytest.mark.parametrize('mode',['bars','ticks'])
def test_mock_cli_generated_file_runs_through_public_runtime(tmp_path,fake_runtime,mode):
    feed=csv_feed(tmp_path,[minute(i,0 if i==6 else .1) for i in range(6,9)])
    output=tmp_path/'generated.jsonl'
    assert main(['mock-feed',str(feed),'--input-mode',mode,'--output',str(output)])==0
    c=config(tmp_path,[],input_mode=mode)
    document=json.loads(c.path.read_text());document['source']['path']=output.name
    c=load_signal_config(write_config(tmp_path,document))
    transport=Transport();report=asyncio.run(run_signals(c,mode='check',transport=transport))
    assert report['error'] is None,report
    assert report['settled_bars']==1 and report['delivered']==1
    assert transport.received[0]['bar']=={'index':2,'time':360_000,'confirmed':True,
                                         'open':107.,'high':111.,'low':104.,'close':109.,'volume':.2}


@pytest.mark.parametrize('policy,extremes',[('high-first',[103,98]),('low-first',[98,103])])
def test_mock_cli_stdout_paths_and_original_boundary(tmp_path,capsys,policy,extremes):
    feed=csv_feed(tmp_path,[minute(0),minute(1,0)])
    assert main(['mock-feed',str(feed),'--policy',policy,'--start-seq','11'])==0
    rows=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row['price'] for row in rows[:4]]==[100,*extremes,101]
    assert [row['seq'] for row in rows[:4]]==[11,12,13,14]
    assert rows[4]=={'type':'bar','bar':asdict(minute(0))}
    assert rows[5]=={'type':'bar','bar':asdict(minute(1,0))}


def test_seeded_output_and_selected_range_are_reproducible(tmp_path):
    feed=csv_feed(tmp_path,[minute(i) for i in range(20)])
    kwargs={'mode':'ticks','policy':'seeded','seed':9,'start_ms':180_000,'end_ms':360_000}
    first=list(mock_events(feed,**kwargs));second=list(mock_events(feed,**kwargs))
    assert first==second
    assert [row['bar']['ts_open'] for row in first if row['type']=='bar']==[180_000,240_000,300_000]
    assert first[0]['seq']==1


def test_calendar_preserves_global_sequence_across_session_gap(tmp_path):
    feed=csv_feed(tmp_path,[minute(i) for i in (1,2,15,16)])
    windows=[{'open_ms':60_000,'close_ms':180_000},{'open_ms':900_000,'close_ms':1_020_000}]
    calendar=tmp_path/'calendar.json';calendar.write_text(json.dumps(windows))
    output=tmp_path/'output.jsonl'
    assert main(['mock-feed',str(feed),'--parent-windows',str(calendar),'--output',str(output)])==0
    rows=[json.loads(line) for line in output.read_text().splitlines()]
    assert [row['seq'] for row in rows if row['type']=='tick']==list(range(1,17))


def test_explicit_null_calendar_cannot_silently_select_utc_mode(tmp_path,capsys):
    feed=csv_feed(tmp_path,[minute(0)])
    calendar=tmp_path/'calendar.json';calendar.write_text('null')
    assert main(['mock-feed',str(feed),'--parent-windows',str(calendar)])==1
    assert 'schedule required' in capsys.readouterr().err


def test_gap_failure_leaves_no_partial_output_and_never_overwrites(tmp_path,capsys):
    feed=csv_feed(tmp_path,[minute(0),minute(2)])
    output=tmp_path/'output.jsonl'
    assert main(['mock-feed',str(feed),'--output',str(output)])==1
    assert not output.exists() and not list(tmp_path.glob('.output.jsonl.*.tmp'))
    assert 'missing' in capsys.readouterr().err
    output.write_text('existing content')
    assert main(['mock-feed',str(feed),'--output',str(output)])==1
    assert output.read_text()=='existing content'


@pytest.mark.parametrize('args',[['--start-ms','1'],['--start-ms','120000','--end-ms','60000'],
                                ['--seed','-1'],['--start-ms','600000']])
def test_invalid_or_empty_selection_fails(tmp_path,capsys,args):
    feed=csv_feed(tmp_path,[minute(0)])
    assert main(['mock-feed',str(feed),*args])==1
    assert 'error:' in capsys.readouterr().err


def test_empty_basecurrency_is_valid_for_native_non_pair_instruments(tmp_path):
    document=config_document(tmp_path)
    document['syminfo']['basecurrency']=''
    c=load_signal_config(write_config(tmp_path,document))
    assert c.syminfo.basecurrency==''
