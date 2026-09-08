import json, subprocess, sys
from pathlib import Path


def run(*args):
    return subprocess.run([sys.executable, "-m", "pineforge_live.cli", *args], capture_output=True, text=True)


def test_version():
    r = run("version")
    assert r.returncode == 0 and r.stdout.startswith("pineforge-live 0.1.0 adapter-api 1 bar-policy v1:")


def test_journal_inspect(tmp_path):
    from pineforge_live.journal import Journal
    p = tmp_path / "j.sqlite3"
    j = Journal.open(p); j.append_epoch("e1", "{}"); j.close()
    r = run("journal-inspect", str(p))
    assert r.returncode == 0 and "epochs: 1" in r.stdout and "stop_marker absent" in r.stdout, r.stderr


def test_journal_inspect_reports_armed_and_present_stop_marker(tmp_path):
    from pineforge_live.journal import Journal, StopMarker
    p = tmp_path / "j.sqlite3"
    Journal.open(p).close()
    marker = StopMarker(str(p) + ".stop")
    marker.prepare()
    r = run("journal-inspect", str(p))
    assert r.returncode == 0 and "stop_marker armed" in r.stdout, r.stderr

    marker.write("HARD", "HOLD", "test")
    r = run("journal-inspect", str(p))
    assert r.returncode == 0 and "stop_marker present" in r.stdout, r.stderr


def test_journal_inspect_missing_journal_is_an_error(tmp_path):
    r = run("journal-inspect", str(tmp_path / "does-not-exist.sqlite3"))
    assert r.returncode == 1 and "error:" in r.stderr


def test_journal_inspect_classifies_a_hand_written_marker_as_armed(tmp_path):
    # N1/Final 13: a marker holding valid-but-non-STOP JSON (no `level` key)
    # is `armed`, not `present` -- the same predicate Journal.open() itself
    # refuses on (finding 5's 8-state matrix).
    from pineforge_live.journal import Journal
    p = tmp_path / "j.sqlite3"
    Journal.open(p).close()
    Path(str(p) + ".stop").write_text(json.dumps({"foo": 1}))
    r = run("journal-inspect", str(p))
    assert r.returncode == 0 and "stop_marker armed" in r.stdout, r.stderr


def test_journal_inspect_classifies_a_torn_marker_as_present(tmp_path):
    from pineforge_live.journal import Journal
    p = tmp_path / "j.sqlite3"
    Journal.open(p).close()
    Path(str(p) + ".stop").write_bytes(b'{"level": ')  # torn, unparsable
    r = run("journal-inspect", str(p))
    assert r.returncode == 0 and "stop_marker present" in r.stdout, r.stderr


def test_tape_smoke_bars_must_be_a_positive_integer(tmp_path):
    # N1/Final 13: --bars 0/-3 fail argparse validation (rc 2) before ever
    # touching the engine .so or the feed file -- dummy paths are fine.
    # N2: the message must not double-prefix "--bars" (argparse already
    # renders "argument --bars: ...").
    so, feed = tmp_path / "missing.so", tmp_path / "missing.csv"
    for bad in ("0", "-3"):
        r = run("tape-smoke", str(so), str(feed), "--bars", bad)
        assert r.returncode == 2, (bad, r.stdout, r.stderr)
        assert "argument --bars: must be a positive integer" in r.stderr, r.stderr
        assert "--bars --bars" not in r.stderr, r.stderr


def test_engine_info_and_tape_smoke(test_so, test_feed):
    r = run("engine-info", str(test_so))
    assert r.returncode == 0 and "abi 4" in r.stdout and "exports 24/24" in r.stdout, r.stderr
    r = run("tape-smoke", str(test_so), str(test_feed), "--bars", "300")
    assert r.returncode == 0 and r.stdout.strip().startswith("settled 300 bars"), r.stderr
