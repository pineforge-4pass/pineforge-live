"""N3/R1 (task-4-rereview.md, task-4-rereview-2.md): the original subprocess
test was cross-process but never contended on the flock -- the parent's
acquire() had already released `_flock()` before the child started, so the
child's LeaseHeld came from the lock-file expiry check (fence.py:55-56)
exactly as a plain sequential-acquire test already covers. Proven by R1:
the same tests pass unchanged with `fcntl.flock` deleted from `_flock()`.

This module now separates the two concerns:

  - `test_child_process_acquire_sees_lease_held` and
    `test_child_process_acquire_succeeds_once_parent_lease_expires` are
    LEASE-EXCLUSION tests: they exercise the lock-file-expiry business
    logic across a real second process, not `_flock()`'s mutual exclusion.

  - `test_child_process_blocks_on_flock_while_parent_holds_it` and
    `test_child_process_without_flock_does_not_block_control` are the
    actual FLOCK-CONTENTION tests: the parent holds the OS advisory lock
    itself (via `a._flock()`) while a child process attempts `acquire()`
    on the same lock/journal, and we assert on wall-clock time that the
    child was genuinely blocked. The second test is the control: with the
    child's `_flock()` neutered, it returns fast -- proving the first
    test would fail (not pass vacuously) if `_flock()`'s exclusion were
    ever removed from `fence.py`.
"""
from __future__ import annotations
import select, subprocess, sys, textwrap, time
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

def test_child_process_blocks_on_flock_while_parent_holds_it(tmp_path):
    # R1: real contention. The parent holds the sibling `.flock` file's OS
    # advisory lock itself (via `a._flock()`, not via a completed
    # `acquire()`), and a child process attempts `acquire()` on a fresh
    # lock file -- there is no live lease to trip the lock-file/checks
    # expiry checks, so the only thing that can make the child wait is
    # `_flock()`'s mutual exclusion.
    #
    # Final 6: the child prints READY right before calling acquire() and
    # the parent waits for that line before starting its 0.5s hold clock --
    # measuring "still blocked" and "elapsed" from READY, not from Popen().
    # A margin between interpreter start + package import + Journal.open()
    # (all of which happen before READY) and the fixed 0.5s/0.4s window
    # used to make this test flaky on a loaded host (finding 6).
    j_path = tmp_path / "j.sqlite3"
    lock = tmp_path / "j.lock"
    j = Journal.open(j_path)
    a = FencedLease(lock, j)

    child_code = textwrap.dedent(f"""
        import time
        from pineforge_live.journal import Journal, FencedLease
        j = Journal.open({str(j_path)!r}, create=False)
        b = FencedLease({str(lock)!r}, j)
        t0 = time.monotonic()
        print("READY", flush=True)
        token = b.acquire(lease_ms=1_000, now_ms=0)
        elapsed = time.monotonic() - t0
        print(f"ACQUIRED:{{token}}:{{elapsed:.3f}}")
        j.close()
    """)

    with a._flock():
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # NF7: a blocking readline() here would hang the whole suite forever
        # if the child never prints READY (e.g. it crashed on import before
        # reaching the print) -- select.select() on the pipe with a 15s
        # timeout fails with a clear message instead.
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        if not ready:
            proc.kill()
            proc.wait(timeout=5)  # reap immediately rather than leaving a zombie until GC
            raise AssertionError(f"child did not print READY within 15s: {proc.stderr.read()}")
        ready_line = proc.stdout.readline()
        assert ready_line.strip() == "READY", (ready_line, proc.stderr.read())
        time.sleep(0.5)
        assert proc.poll() is None, "child should still be blocked on the flock 0.5s after READY"
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 0, stderr
    marker, token_str, elapsed_str = stdout.strip().split(":")
    assert marker == "ACQUIRED", (stdout, stderr)
    assert float(elapsed_str) >= 0.4, f"child should have blocked on the flock (measured from READY): {stdout}"
    j.close()

def test_child_process_without_flock_does_not_block_control(tmp_path):
    # R1's control: with the CHILD's `_flock()` neutered to a no-op (while
    # the parent still genuinely holds the OS lock), the child never
    # contends and returns fast. This proves the previous test is
    # discriminating -- if `_flock()`'s exclusion were ever removed from
    # `fence.py` for real, that test's >= 0.4s assertion would fail rather
    # than passing vacuously.
    j_path = tmp_path / "j.sqlite3"
    lock = tmp_path / "j.lock"
    j = Journal.open(j_path)
    a = FencedLease(lock, j)

    child_code = textwrap.dedent(f"""
        import contextlib, time
        from pineforge_live.journal import Journal, FencedLease
        FencedLease._flock = lambda self: contextlib.nullcontext()
        j = Journal.open({str(j_path)!r}, create=False)
        b = FencedLease({str(lock)!r}, j)
        t0 = time.monotonic()
        token = b.acquire(lease_ms=1_000, now_ms=0)
        elapsed = time.monotonic() - t0
        print(f"ACQUIRED:{{token}}:{{elapsed:.3f}}")
        j.close()
    """)

    with a._flock():
        result = subprocess.run(
            [sys.executable, "-c", child_code], capture_output=True, text=True, timeout=15,
        )
    assert result.returncode == 0, result.stderr
    marker, token_str, elapsed_str = result.stdout.strip().split(":")
    assert marker == "ACQUIRED", (result.stdout, result.stderr)
    assert float(elapsed_str) < 0.2, f"child should not have blocked (flock neutered): {result.stdout}"
    j.close()
