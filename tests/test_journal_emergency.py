"""Task 5 F5 + I1: pineforge_live.journal.journal.Journal.emergency_log only.

Kept separate from tests/test_journal.py (owned by another reviewer during
this fix wave) to avoid touching that file.
"""
import json, os
from pineforge_live.journal import Journal


def test_emergency_log_retries_a_short_write(tmp_path, monkeypatch):
    # F5: os.write's return value used to be unchecked -- a short write
    # (signal, ENOSPC mid-line) would leave a torn, unparseable line behind.
    p = tmp_path / "emergency.log"
    j = Journal.open(tmp_path / "j.sqlite3")
    log = j.emergency_log(p)
    real_write = os.write
    calls = []

    def flaky_write(fd, data):
        calls.append(bytes(data))
        if len(calls) == 1 and len(data) > 1:
            return real_write(fd, data[:1])  # force a short write on the first call only
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky_write)
    log({"kind": "EMERGENCY", "client_id": "c1"})
    lines = p.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["client_id"] == "c1"
    assert len(calls) >= 2  # the short write forced a second os.write to finish the line
    j.close()


def test_emergency_log_falls_back_to_stderr_on_oserror(tmp_path, capsys):
    # I1: the out-of-band path's own mount can be missing (or full); the
    # EMERGENCY record must reach stderr instead of being lost, and the
    # call must never raise into the caller's already-degraded path.
    j = Journal.open(tmp_path / "j.sqlite3")
    unreachable = tmp_path / "no-such-mount" / "emergency.log"
    log = j.emergency_log(unreachable)
    log({"kind": "EMERGENCY", "client_id": "c1", "reason": "sqlite write-ahead failed"})  # must not raise
    assert not unreachable.exists()
    row = json.loads(capsys.readouterr().err.strip())
    assert row["client_id"] == "c1"
    j.close()
