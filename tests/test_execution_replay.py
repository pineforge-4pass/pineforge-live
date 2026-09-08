"""Actual mock order placement, receipts and restart on compiled strategies."""
import asyncio
import json

import pytest

from pineforge_live.drivers.tape_execution import execute_tape
from pineforge_live.execution.store import ExecutionStore
from pineforge_live.journal import JournalCorrupt


@pytest.mark.parametrize('mode', ['stream', 'check'])
def test_execution_replay_survives_restart_and_accepted_timeout(test_so, test_feed, tmp_path, mode):
    report = asyncio.run(execute_tape(so=test_so, feed=test_feed, journal_dir=tmp_path / mode,
                                     mode=mode, bars=12, restart_after=7, fault='after_accept'))
    assert report['error'] is None, report
    assert report['summary']['bars'] == 12
    assert report['summary']['restarts'] == 1
    assert report['summary']['physical_orders'] == 2
    assert report['summary']['receipts'] == 2
    assert report['summary']['unresolved'] == 0
    saved = json.loads((tmp_path / mode / 'execution-report.json').read_text())
    assert saved == report


def test_before_accept_timeout_is_reported_not_blindly_retried(test_so, test_feed, tmp_path):
    report = asyncio.run(execute_tape(so=test_so, feed=test_feed, journal_dir=tmp_path,
                                     bars=12, fault='before_accept'))
    assert report['error'] is not None
    assert report['summary']['physical_orders'] == 0
    assert report['summary']['unresolved'] > 0
    assert (tmp_path / 'execution-report.json').is_file()


def test_corrupt_store_still_produces_failure_report(test_so, test_feed, tmp_path, monkeypatch):
    def fail(self):
        raise JournalCorrupt('injected outbox corruption')
    monkeypatch.setattr(ExecutionStore, 'requests', fail)
    report = asyncio.run(execute_tape(so=test_so, feed=test_feed, journal_dir=tmp_path, bars=2))
    assert 'injected outbox corruption' in report['error']
    assert report['report_errors']
    assert json.loads((tmp_path / 'execution-report.json').read_text())['error'] == report['error']


def test_orders_on_close_get_separate_execution_receipts(test_so_pooc, test_feed, tmp_path):
    report = asyncio.run(execute_tape(so=test_so_pooc, feed=test_feed, journal_dir=tmp_path, bars=60))
    assert report['error'] is None, report['error']
    assert report['summary']['action_receipts'] > 0
    assert sum(row['action_receipts'] for row in report['bars']) > 0
    assert all('RETRACTED' not in row['classes'] for row in report['bars'])
