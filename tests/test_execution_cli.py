"""Operator commands preserve STOP evidence and reject active writers."""
import json
import time

from pineforge_live.cli import main
from pineforge_live.journal import Journal, StopMarker
from pineforge_live.journal.fence import FencedLease


def stopped(tmp_path):
    path = tmp_path / 'j.sqlite3'
    marker = StopMarker(str(path) + '.stop')
    marker.prepare()
    journal = Journal.open(path)
    marker.write('HARD', 'HOLD', 'test')
    journal.append_stop('HARD', 'HOLD', 'test')
    return path, marker, journal


def test_stop_clear_refuses_live_lease(tmp_path):
    path, marker, journal = stopped(tmp_path)
    lease = FencedLease(tmp_path / 'custom.lock', journal)
    lease.acquire(60_000, int(time.time() * 1000))
    assert main(['stop-clear', str(path), '--cause', 'operator checked']) == 1
    assert marker.exists()
    assert journal.rows('stops', 'cleared_ms IS NULL', ())
    journal.close()


def test_stop_clear_is_audited_and_releases_lease(tmp_path):
    path, marker, journal = stopped(tmp_path)
    journal.close()
    assert main(['stop-clear', str(path), '--cause', 'verified venue flat']) == 0
    assert not marker.exists()
    journal = Journal.open(path)
    assert not journal.rows('stops', 'cleared_ms IS NULL', ())
    rows = journal.rows('incidents', "kind='operator_stop_clear'", ())
    assert json.loads(rows[0]['detail_json'])['cause'] == 'verified venue flat'
    assert journal.live_check(int(time.time() * 1000)) is None
    journal.close()


def test_stop_clear_journal_failure_keeps_marker(tmp_path, monkeypatch):
    path, marker, journal = stopped(tmp_path)
    journal.close()
    from pineforge_live.journal import JournalFault
    monkeypatch.setattr(Journal, 'append_stop_cleared', lambda *a: (_ for _ in ()).throw(JournalFault('write failed')))
    assert main(['stop-clear', str(path), '--cause', 'operator checked']) == 1
    assert marker.exists()


def test_execution_cli_writes_an_offline_report(test_so, test_feed, tmp_path, capsys):
    result = main(['execution-replay', str(test_so), str(test_feed), '--bars', '3',
                   '--journal-dir', str(tmp_path / 'run'), '--mode', 'check', '--restart-after', '7'])
    assert result == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['summary']['restarts'] == 1
    report = json.loads((tmp_path / 'run' / 'execution-report.json').read_text())
    assert report['lane'] == 'offline-mock-execution'
