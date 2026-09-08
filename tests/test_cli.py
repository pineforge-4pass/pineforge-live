import subprocess, sys


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


def test_engine_info_and_tape_smoke(test_so, test_feed):
    r = run("engine-info", str(test_so))
    assert r.returncode == 0 and "abi 4" in r.stdout and "exports 24/24" in r.stdout, r.stderr
    r = run("tape-smoke", str(test_so), str(test_feed), "--bars", "300")
    assert r.returncode == 0 and r.stdout.strip().startswith("settled 300 bars"), r.stderr
