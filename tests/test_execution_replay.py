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


def test_an_orders_on_close_entry_without_a_pine_id_is_refused(test_so_pooc, test_feed, tmp_path):
    # Engine v1.0.0 fills this probe's flip entry at the close of the bar
    # that places it, together with its strategy.close, as TradingView does.
    # The entry never rested in the settled book, so the core cannot name its
    # Pine order ("?"), and the coordinator refuses an unattributed entry
    # (docs/plan-b3.md). Every orders-on-close fill in this window is such a
    # flip, so the replay stops at the first one (bar 2048) with a written
    # report and places neither of its legs.
    report = asyncio.run(execute_tape(so=test_so_pooc, feed=test_feed, journal_dir=tmp_path, bars=60))
    assert report['error'] == 'ExecutionSafetyError: ambiguous entry intent cannot become a venue order'
    assert report['summary']['bars'] == 48 and report['bars'][-1]['bar_index'] == 2047   # settled up to the flip bar
    assert report['summary']['physical_orders'] == 0 and report['summary']['action_receipts'] == 0
    assert json.loads((tmp_path / 'execution-report.json').read_text())['error'] == report['error']
