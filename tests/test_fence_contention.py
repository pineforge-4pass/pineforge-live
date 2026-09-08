"""N3 (task-4-rereview.md): the existing flock test was sequential (a
acquires and fully releases before b even starts), so LeaseHeld came from
the `_read()` expiry check -- the same path a plain "two sequential
acquires" test already covers -- and never exercised `_flock()` actually
excluding a concurrent holder. This proves contention with a real second
process: the parent holds a live lease, a child process attempts to
acquire the same lease and must observe LeaseHeld."""
from __future__ import annotations
import subprocess, sys, textwrap
from pineforge_live.journal import Journal, FencedLease

def test_child_process_acquire_sees_lease_held(tmp_path):
    j_path = tmp_path / "j.sqlite3"
    lock = tmp_path / "j.lock"
    j = Journal.open(j_path)
    a = FencedLease(lock, j)
    a.acquire(lease_ms=60_000, now_ms=0)  # long-lived: still live when the child runs

    child_code = textwrap.dedent(f"""
        from pineforge_live.journal import Journal, FencedLease, LeaseHeld
        j = Journal.open({str(j_path)!r}, create=False)
        b = FencedLease({str(lock)!r}, j)
        try:
            b.acquire(lease_ms=1_000, now_ms=0)
            print("ACQUIRED")
        except LeaseHeld:
            print("LEASE_HELD")
        finally:
            j.close()
    """)
    result = subprocess.run(
        [sys.executable, "-c", child_code], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LEASE_HELD", (result.stdout, result.stderr)
    j.close()

def test_child_process_acquire_succeeds_once_parent_lease_expires(tmp_path):
    # Sanity companion: the child is not permanently excluded -- once the
    # held lease has expired (by the clock the child passes in), it acquires.
    j_path = tmp_path / "j.sqlite3"
    lock = tmp_path / "j.lock"
    j = Journal.open(j_path)
    a = FencedLease(lock, j)
    t1 = a.acquire(lease_ms=1_000, now_ms=0)  # expiry 1_000

    child_code = textwrap.dedent(f"""
        from pineforge_live.journal import Journal, FencedLease, LeaseHeld
        j = Journal.open({str(j_path)!r}, create=False)
        b = FencedLease({str(lock)!r}, j)
        try:
            token = b.acquire(lease_ms=1_000, now_ms=5_000)
            print(f"ACQUIRED:{{token}}")
        except LeaseHeld:
            print("LEASE_HELD")
        finally:
            j.close()
    """)
    result = subprocess.run(
        [sys.executable, "-c", child_code], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ACQUIRED:{t1 + 1}", (result.stdout, result.stderr)
    j.close()
