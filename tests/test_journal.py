import hashlib, json, os, sqlite3
from pathlib import Path
import pytest
from pineforge_live.journal import (
    Journal, StopMarker, FencedLease, JournalCorrupt, JournalConflict,
    JournalFault, LeaseHeld, LeaseLost, StopMarkerPresent,
)
from pineforge_live.journal import journal as journal_mod
from pineforge_live.journal.schema import DDL
from pineforge_live import types as T

def settlement(i, epoch="e1", **overrides):
    row = {"bar_index": i, "epoch_hash": epoch, "runtime_config_hash": "rc", "bars_hash": 123 + i,
           "broker_state_hash": 456 + i, "trades_len": i, "position": 0.0, "equity": 1000.0 + i,
           "trades_sha256": "0" * 64}
    row.update(overrides)
    return row

def bar(ts=1000, **overrides):
    kwargs = dict(ts_open=ts, o=1.0, h=2.0, l=0.5, c=1.5, v=10.0, trade_count=3)
    kwargs.update(overrides)
    return T.NormalizedBar(**kwargs)

def action(client_id="c1", **overrides):
    row = {"client_id": client_id, "epoch_hash": "e1", "intent_key": "L|ENTRY||1", "action_seq": 1,
           "level_version": 0, "run_token": 7, "cls": "TRIGGER", "lane": "DISCRETIONARY", "payload_json": "{}"}
    row.update(overrides)
    return row

def evaluation(**overrides):
    row = {"epoch_hash": "e1", "trigger": "BAR", "tick_seq_from": None, "tick_seq_to": None,
           "forming_json": "{}", "outcome": "NOOP", "recompute_ms": 5}
    row.update(overrides)
    return row

def fill(venue_trade_id="t1", **overrides):
    row = {"venue_trade_id": venue_trade_id, "client_id": "c1", "venue_order_id": "o1", "ts": 1,
           "side": "BUY", "qty": 1.0, "price": 100.0, "fee": 0.1, "cause": "OURS",
           "target_bar_index": 1, "cls": "ENTRY"}
    row.update(overrides)
    return row


def test_settlement_idempotent_and_checksummed(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")
    j.append_settlement(settlement(1)); j.append_settlement(settlement(1))   # idempotent
    rows = j.rows("settlements", "epoch_hash=?", ("e1",))
    assert len(rows) == 1
    j.verify_tail()  # the stored checksum verifies against the stored (hex-hash) row -- finding 2/4
    assert j.last_settlement("e1")["bar_index"] == 1
    j.close()

def test_verify_tail_refuses_bad_checksum(tmp_path):
    p = tmp_path / "j.sqlite3"
    j = Journal.open(p); j.append_epoch("e1", "{}"); j.append_settlement(settlement(1)); j.append_settlement(settlement(2)); j.close()
    con = sqlite3.connect(p); con.execute("UPDATE settlements SET equity = equity + 1 WHERE bar_index = 2"); con.commit(); con.close()
    with pytest.raises(JournalCorrupt):
        Journal.open(p, create=False).verify_tail()

def test_write_ahead_action_and_non_terminal_query(tmp_path):
    # Finding 2: actions is append-only now (no `terminal` column); this
    # exercises the original write-ahead shape through the new
    # actions_non_terminal()/update_order_state() derivation.
    j = Journal.open(tmp_path / "j.sqlite3"); j.append_epoch("e1", "{}")
    j.append_action(action("c1"))
    assert [a["client_id"] for a in j.actions_non_terminal()] == ["c1"]
    j.update_order_state("c1", json.dumps({"status": "FILLED"}), terminal=True)
    assert j.actions_non_terminal() == []
    j.close()

def test_emergency_log_appends_synced_lines(tmp_path):
    p = tmp_path / "emergency.log"
    j = Journal.open(tmp_path / "j.sqlite3")
    log = j.emergency_log(p)
    log({"kind": "EMERGENCY", "client_id": "c1", "reason": "sqlite write-ahead failed"})
    log({"kind": "EMERGENCY", "client_id": "c2"})
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["client_id"] == "c1" and rows[1]["client_id"] == "c2"
    # Never touches sqlite, never raises on a closed journal.
    j.close()
    log({"kind": "EMERGENCY", "client_id": "c3"})
    lines = p.read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[2])["client_id"] == "c3"

def test_stop_marker_durability_and_refusal(tmp_path):
    # Finding 7: write() no longer creates the file -- prepare() preallocates
    # the 4096-byte zeroed marker first.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    assert not m.exists()
    m.prepare()
    assert not m.exists()  # armed (zeroed), not set
    m.write("HARD", "HOLD", "journal fault")
    assert m.exists() and os.path.getsize(m.path) >= 4096
    assert m.read()["level"] == "HARD"
    with pytest.raises(StopMarkerPresent):  # finding 14: subclass of JournalCorrupt
        Journal.open(tmp_path / "j.sqlite3", stop_marker=m)
    assert m.clear()["cause"] == "journal fault" and not m.exists()

def test_fencing_token_monotonic_and_exclusive(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j); t1 = a.acquire(lease_ms=10_000, now_ms=1_000)
    b = FencedLease(lock, j)
    with pytest.raises(LeaseHeld):
        b.acquire(lease_ms=10_000, now_ms=2_000)
    assert a.expired(now_ms=11_001)
    t2 = b.acquire(lease_ms=10_000, now_ms=11_001)
    assert t2 == t1 + 1
    assert [r["fencing_token"] for r in j.rows("checks", "1=1", ())] == [t1, t2]
    j.close()

def test_fenced_lease_safe_when_lock_absent_empty_or_from_prior_journal(tmp_path):
    lock = tmp_path / "j.lock"

    # Absent: acquires normally.
    j1 = Journal.open(tmp_path / "j1.sqlite3")
    assert FencedLease(lock, j1).acquire(lease_ms=1_000, now_ms=0) == 1
    j1.close()

    # Empty file: not a live lease, acquires normally against a fresh journal
    # (whose own max_fencing_token() is 0) even though the lock file exists.
    lock.write_text("")
    j2 = Journal.open(tmp_path / "j2.sqlite3")
    assert FencedLease(lock, j2).acquire(lease_ms=1_000, now_ms=0) == 1
    j2.close()

    # Unreadable/corrupt content: treated the same as "no live lease".
    lock.write_text("{not json")
    j3 = Journal.open(tmp_path / "j3.sqlite3")
    assert FencedLease(lock, j3).acquire(lease_ms=1_000, now_ms=0) == 1
    j3.close()

    # Stale lock from a previous journal (expired, high token) against a brand
    # new journal that has never seen a check row: token continuity comes from
    # max(journal.max_fencing_token(), lock token) + 1, not just the fresh
    # journal's own (empty) history.
    lock.write_text(json.dumps({"token": 41, "expiry_ms": 0}))
    j4 = Journal.open(tmp_path / "j4.sqlite3")
    assert j4.max_fencing_token() == 0
    assert FencedLease(lock, j4).acquire(lease_ms=1_000, now_ms=100) == 42
    j4.close()


# --- new tests (task-4 fix wave) --------------------------------------------

def test_append_bar_and_fill_round_trip(tmp_path):
    # Finding 1: append_bar/append_fill raised on every call (no created_ms
    # column on bars/fills).
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")
    j.append_bar("e1", bar(1000), bars_hash=42)
    j.append_fill(fill("t1"))
    brows = j.rows("bars", "epoch_hash=?", ("e1",))
    frows = j.rows("fills", "1=1", ())
    assert len(brows) == 1 and brows[0]["ts_open"] == 1000 and brows[0]["created_ms"] is not None
    assert len(frows) == 1 and frows[0]["venue_trade_id"] == "t1" and frows[0]["created_ms"] is not None
    j.close()

def test_verify_tail_after_every_checksummed_writer(tmp_path):
    # Finding 2: verify_tail used to raise on every journal holding an
    # actions or evaluations row (checksum computed over caller values,
    # verified over SELECT * with extra/coerced columns).
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")

    j.append_action(action("c1"))
    j.verify_tail()

    j.update_order_state("c1", json.dumps({"status": "FILLED"}), terminal=True)
    j.verify_tail()  # actions row itself was never mutated -- still verifies

    j.append_evaluation(evaluation())
    j.verify_tail()

    j.append_bar("e1", bar(1000), bars_hash=7)
    j.verify_tail()
    j.close()

def _journal_with_one_row_of_every_checksummed_table(path):
    j = Journal.open(path)
    j.append_epoch("e1", "{}")
    j.append_settlement(settlement(1))
    j.append_bar("e1", bar(1000), bars_hash=7)
    j.append_action(action("c1"))
    j.append_evaluation(evaluation())
    j.close()

def test_open_verifies_one_row_of_every_checksummed_table(tmp_path):
    p = tmp_path / "j.sqlite3"
    _journal_with_one_row_of_every_checksummed_table(p)
    Journal.open(p, create=False).close()  # succeeds: every checksummed table's last row verifies

@pytest.mark.parametrize("table,set_clause", [
    ("settlements", "equity = equity + 1"),
    ("bars", "c = c + 1"),
    ("actions", "payload_json = '{\"tampered\": true}'"),
    ("evaluations", "outcome = 'EDITED'"),
])
def test_open_refuses_when_a_checksummed_row_is_edited(tmp_path, table, set_clause):
    p = tmp_path / f"j_{table}.sqlite3"
    _journal_with_one_row_of_every_checksummed_table(p)
    con = sqlite3.connect(p); con.execute(f"UPDATE {table} SET {set_clause}"); con.commit(); con.close()
    with pytest.raises(JournalCorrupt):
        Journal.open(p, create=False)

def test_hash_columns_round_trip_full_uint64_range(tmp_path):
    # Finding 3: bars_hash/broker_state_hash >= 2**63 overflowed SQLite's
    # signed INTEGER storage class.
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")
    j.append_bar("e1", bar(1000), bars_hash=2**64 - 1)
    assert j.rows("bars", "1=1", ())[0]["bars_hash"] == 2**64 - 1

    j.append_settlement(settlement(1, bars_hash=2**64 - 1, broker_state_hash=2**63 + 5))
    s = j.settlement("e1", 1)
    assert s["bars_hash"] == 2**64 - 1 and s["broker_state_hash"] == 2**63 + 5
    assert isinstance(s["bars_hash"], int) and isinstance(s["broker_state_hash"], int)
    j.verify_tail()
    j.close()

def test_settlement_conflict_idempotency_and_nan_rejection(tmp_path):
    # Finding 5: INSERT OR IGNORE silently dropped a conflicting row (and any
    # NOT NULL/NaN-coerced-to-NULL violation) instead of raising.
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")
    j.append_settlement(settlement(1))
    j.append_settlement(settlement(1))  # byte-identical re-append: idempotent
    assert len(j.rows("settlements", "epoch_hash=?", ("e1",))) == 1

    with pytest.raises(JournalConflict):
        j.append_settlement(settlement(1, equity=999.0))  # same key, different content

    with pytest.raises(JournalFault):  # finding 4: NaN rejected outright
        j.append_settlement(settlement(2, equity=float("nan")))
    assert j.settlement("e1", 2) is None
    j.close()

def test_stop_marker_prepare_then_write_then_exists(tmp_path):
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.prepare()
    assert os.path.getsize(m.path) == 4096
    assert not m.exists()
    m.write("FLAT_ONLY", "NONE", "risk breach")
    assert m.exists()
    assert m.read() == {"level": "FLAT_ONLY", "disposition": "NONE", "cause": "risk breach", "ts_ms": m.read()["ts_ms"]}

def test_torn_stop_marker_unreadable_and_refuses_open(tmp_path):
    # Finding 8/14: a torn marker used to escape as json.JSONDecodeError
    # instead of a journal exception.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.prepare()
    fd = os.open(m.path, os.O_WRONLY)
    try:
        os.pwrite(fd, b"{\"level\": ", 0)  # 10 bytes of garbage, not valid JSON
    finally:
        os.close(fd)
    r = m.read()
    assert r is not None and r.get("unreadable") is True
    with pytest.raises(StopMarkerPresent):
        Journal.open(tmp_path / "j.sqlite3", stop_marker=m)

def test_renew_after_another_holder_acquired_raises_lease_lost(tmp_path):
    # Finding 9: renew() rewrote the lock unconditionally, letting a lapsed
    # holder regress the lock file's token back after someone else acquired.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    a.acquire(lease_ms=1_000, now_ms=1_000)  # expiry 2_000
    b = FencedLease(lock, j)
    b.acquire(lease_ms=1_000, now_ms=3_000)  # a's lease has lapsed; b takes over
    with pytest.raises(LeaseLost):
        a.renew(now_ms=3_500, lease_ms=1_000)
    assert a.token is None
    cur = json.loads(lock.read_text())
    assert cur["token"] == 2  # unaffected by a's failed renew
    j.close()

def test_two_acquires_under_flock_second_raises_lease_held(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    a.acquire(lease_ms=10_000, now_ms=0)
    b = FencedLease(lock, j)
    with pytest.raises(LeaseHeld):
        b.acquire(lease_ms=10_000, now_ms=1_000)  # still well within a's lease
    j.close()

def test_pragma_readback_wal_and_synchronous_full(tmp_path):
    # Finding 11: WAL/synchronous PRAGMAs were set but never read back.
    j = Journal.open(tmp_path / "j.sqlite3")
    mode = j.con.execute("PRAGMA journal_mode").fetchone()[0]
    sync = j.con.execute("PRAGMA synchronous").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert int(sync) == 2
    j.close()

def test_stop_cleared_journaled_on_latest_open_stop_row(tmp_path):
    # Finding 12: stops.cleared_ms was never written.
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_stop("HARD", "HOLD", "journal fault")
    row = j.rows("stops", "1=1", ())[0]
    assert row["cleared_ms"] is None
    j.append_stop_cleared("operator cleared")
    row = j.rows("stops", "1=1", ())[0]
    assert row["cleared_ms"] is not None
    j.close()

def test_renew_before_acquire_raises_lease_held(tmp_path):
    # Finding 13: renew() used a bare `assert`, stripped under -O.
    j = Journal.open(tmp_path / "j.sqlite3")
    lease = FencedLease(tmp_path / "j.lock", j)
    with pytest.raises(LeaseHeld):
        lease.renew(now_ms=0, lease_ms=1_000)
    j.close()

def test_fence_read_handles_non_dict_json(tmp_path):
    # Finding 10: a JSON list in the lock file used to raise AttributeError.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    lock.write_text(json.dumps([1, 2]))
    assert FencedLease(lock, j).acquire(lease_ms=1_000, now_ms=0) == 1
    j.close()

def test_open_default_create_true_verifies_existing_journal(tmp_path):
    # Finding 6: create=True (the default) used to skip verify_tail entirely.
    p = tmp_path / "j.sqlite3"
    j = Journal.open(p); j.append_epoch("e1", "{}"); j.append_settlement(settlement(1)); j.close()
    con = sqlite3.connect(p); con.execute("UPDATE settlements SET equity = equity + 1"); con.commit(); con.close()
    with pytest.raises(JournalCorrupt):
        Journal.open(p)  # create=True default


# --- re-review fix wave 2 (task-4-rereview.md N1-N10) ------------------------

def test_prepare_over_a_set_marker_raises_and_leaves_it_untouched(tmp_path):
    # N1: prepare() must never disarm a SET marker -- the natural startup
    # order "arm the sidecar, then open the journal" must not silently
    # clobber an unacknowledged STOP.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.prepare()
    m.write("HARD", "HOLD", "journal fault")
    assert m.exists()
    with pytest.raises(StopMarkerPresent):
        m.prepare()
    assert m.exists() and m.read()["level"] == "HARD"  # untouched

def test_prepare_over_a_torn_marker_raises_and_preserves_the_evidence(tmp_path):
    # N1: a torn marker is itself evidence of an in-flight STOP write --
    # prepare() must not erase it either.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.prepare()
    fd = os.open(m.path, os.O_WRONLY)
    try:
        os.pwrite(fd, b"{\"level\": ", 0)  # torn, not valid JSON
    finally:
        os.close(fd)
    with pytest.raises(StopMarkerPresent):
        m.prepare()
    r = m.read()
    assert r is not None and r.get("unreadable") is True

def test_prepare_over_an_armed_marker_is_a_noop_and_idempotent(tmp_path):
    # N1: an armed (all-zero, preallocated-but-unset) marker is the state
    # prepare() itself produces -- calling it again must be a true no-op,
    # not a rewrite, and calling it a third time must behave identically.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.prepare()
    assert not m.exists() and os.path.getsize(m.path) == 4096
    m.prepare()
    assert not m.exists() and os.path.getsize(m.path) == 4096
    m.prepare()
    assert not m.exists() and os.path.getsize(m.path) == 4096

def test_prepare_pads_a_zero_byte_armed_marker_back_up_to_size(tmp_path):
    # R3: a crash between prepare()'s own O_CREAT|O_EXCL create and its
    # zero-fill pwrite (or an externally created empty file) used to leave
    # an armed marker under 4096 bytes forever -- every later prepare()
    # no-op'd on it since payload is None either way (armed, not SET).
    # Now it is topped up idempotently, never disarming anything.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.path.write_bytes(b"")  # simulated crash: 0 bytes, still armed
    m.prepare()
    assert os.path.getsize(m.path) == 4096
    assert not m.exists()
    m.prepare()  # idempotent: already full-size, true no-op
    assert os.path.getsize(m.path) == 4096

def test_prepare_pads_a_short_nonzero_armed_marker_back_up_to_size(tmp_path):
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.path.write_bytes(b"\0" * 100)  # e.g. an externally created short file
    m.prepare()
    assert os.path.getsize(m.path) == 4096
    assert not m.exists()

def test_prepare_wraps_permission_error_as_journal_fault(tmp_path):
    # R5: prepare()'s OS-level failures used to escape raw (unlike
    # write(), which already normalizes to JournalFault). An unreadable
    # existing marker (_payload()'s PermissionError) is now wrapped too.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.prepare()
    os.chmod(m.path, 0o000)
    try:
        with pytest.raises(JournalFault):
            m.prepare()
    finally:
        os.chmod(m.path, 0o600)  # restore so tmp_path cleanup can remove it

def test_prepare_wraps_missing_parent_directory_as_journal_fault(tmp_path):
    m = StopMarker(tmp_path / "missing_dir" / "j.sqlite3.stop")
    with pytest.raises(JournalFault):
        m.prepare()

def test_write_without_prepare_still_produces_a_durable_marker(tmp_path):
    # N2: the emergency STOP path cannot depend on prepare() having been
    # called (no runtime caller is wired up yet) or having succeeded.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    assert not m.path.exists()
    m.write("HARD", "HOLD", "no prepare() call happened")
    assert m.exists()
    assert os.path.getsize(m.path) == 4096
    assert m.read()["level"] == "HARD"

def test_acquire_consults_checks_table_when_lock_file_is_deleted(tmp_path):
    # N4: the lock file alone is not authoritative -- it can be deleted
    # (operator, a /tmp cleaner) while its lease is still live in the
    # journal's own `checks` table.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    a.acquire(lease_ms=10_000, now_ms=0)  # expiry 10_000
    lock.unlink()
    b = FencedLease(lock, j)
    with pytest.raises(LeaseHeld):
        b.acquire(lease_ms=10_000, now_ms=1_000)  # a's lease is still live in `checks`
    j.close()

def test_update_check_expiry_extends_the_stored_row(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_check(1, 1_000)
    j.update_check_expiry(1, 5_000)
    row = j.rows("checks", "fencing_token=?", (1,))[0]
    assert row["lease_expiry_ms"] == 5_000
    j.close()

def test_renew_extends_checks_lease_row_so_deleted_lock_stays_guarded(tmp_path):
    # N4/R2: acquire()'s checks-table guard only covered the acquire-time
    # expiry until renew() also extended it -- once a holder renews past
    # its original lease_ms window, deleting the lock file reopened the
    # original N4 hole (a second acquire could succeed while the first
    # holder still believed itself live). renew() now extends the same
    # `checks` row under the same `_flock()`.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    t1 = a.acquire(lease_ms=1_000, now_ms=0)  # expiry 1_000, checks row (t1, 1_000)
    a.renew(now_ms=900, lease_ms=1_000)       # expiry 1_900, checks row extended to (t1, 1_900)
    lock.unlink()
    b = FencedLease(lock, j)
    with pytest.raises(LeaseHeld):
        b.acquire(lease_ms=1_000, now_ms=1_500)  # a's renewed lease is still live in `checks`
    t2 = b.acquire(lease_ms=1_000, now_ms=2_000)  # past 1_900 -- a's lease has truly lapsed
    assert t2 == t1 + 1
    j.close()

def test_append_stop_cleared_records_cause_and_reports_when_nothing_open(tmp_path):
    # N5: `cause` used to be accepted and discarded, and a no-open-stop call
    # returned normally with no signal either way.
    j = Journal.open(tmp_path / "j.sqlite3")
    assert j.append_stop_cleared("nothing to clear") is False
    j.append_stop("HARD", "HOLD", "journal fault")
    assert j.append_stop_cleared("operator investigated, resumed") is True
    row = j.rows("stops", "1=1", ())[0]
    assert row["cleared_ms"] is not None
    assert row["cleared_cause"] == "operator investigated, resumed"
    assert j.append_stop_cleared("already cleared") is False  # no open stop left
    j.close()

def test_refused_open_closes_its_connection_before_raising(tmp_path, monkeypatch):
    # N8: verify_tail()'s JournalCorrupt on a torn tail used to propagate
    # with the sqlite3.Connection still open -- the WAL file staying open
    # right when the operator is about to act on the refusal.
    p = tmp_path / "j.sqlite3"
    j = Journal.open(p); j.append_epoch("e1", "{}"); j.append_settlement(settlement(1)); j.close()
    con = sqlite3.connect(p); con.execute("UPDATE settlements SET equity = equity + 1"); con.commit(); con.close()

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect
    def spy_connect(*a, **kw):
        c = real_connect(*a, **kw)
        opened.append(c)
        return c
    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    with pytest.raises(JournalCorrupt):
        Journal.open(p, create=False)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed by Journal.open, not leaked

    monkeypatch.undo()
    # The refusal does not leave the file locked/open behind it: fixing the
    # row and reopening still works.
    con = sqlite3.connect(p); con.execute("UPDATE settlements SET equity = equity - 1"); con.commit(); con.close()
    Journal.open(p, create=False).close()

def test_open_closes_connection_on_baseexception_not_just_exception(tmp_path, monkeypatch):
    # R6: Journal.open()'s cleanup used to catch only `Exception`, so a
    # BaseException (KeyboardInterrupt/SystemExit landing mid-PRAGMA/DDL/
    # verify_tail) still left `con` open. `except BaseException` closes it
    # unconditionally.
    p = tmp_path / "j.sqlite3"
    Journal.open(p).close()  # existed=True on reopen, so verify_tail() runs

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect
    def spy_connect(*a, **kw):
        c = real_connect(*a, **kw)
        opened.append(c)
        return c
    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    monkeypatch.setattr(Journal, "verify_tail", lambda self: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        Journal.open(p, create=False)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed despite a BaseException, not an Exception

def test_open_writes_schema_meta_version_on_create_and_reopen_is_clean(tmp_path):
    # R4: a fresh journal records SCHEMA_VERSION once; reopening it must
    # not error or duplicate the row.
    p = tmp_path / "j.sqlite3"
    j = Journal.open(p)
    assert j.rows("schema_meta", "1=1", ()) == [{"version": 2}]
    j.close()
    j2 = Journal.open(p, create=False)
    assert j2.rows("schema_meta", "1=1", ()) == [{"version": 2}]
    j2.close()

def test_open_refuses_schema_meta_version_mismatch(tmp_path):
    p = tmp_path / "j.sqlite3"
    Journal.open(p).close()
    con = sqlite3.connect(p); con.execute("UPDATE schema_meta SET version=999"); con.commit(); con.close()
    with pytest.raises(JournalFault, match="schema_meta version"):
        Journal.open(p, create=False)

def test_open_refuses_a_pre_schema_versioning_journal(tmp_path):
    # R4: a journal created before 57b9730 has no schema_meta row --
    # simulated here by deleting it from an otherwise-normal journal,
    # since a real pre-versioning file would likewise have the table
    # freshly (re)created empty by `CREATE TABLE IF NOT EXISTS` on open,
    # then found with zero rows. v1 journals are not migrated: refused
    # with a clear message rather than silently backfilled to version 1.
    p = tmp_path / "j.sqlite3"
    Journal.open(p).close()
    con = sqlite3.connect(p); con.execute("DELETE FROM schema_meta"); con.commit(); con.close()
    with pytest.raises(JournalFault, match="not migrated"):
        Journal.open(p, create=False)


# --- final fix wave (task-4-rereview-3.md R7-R11, rowcount; final-review.md Final 8/9) ---

def test_prepare_pad_races_a_concurrent_write_without_disarming_it(tmp_path, monkeypatch):
    # R7: the R3 pad path is check-then-write (stat() then a separate
    # open+write) -- if another process's write() (an emergency STOP from
    # the previous holder, landing in exactly the window N1 was about)
    # landed between them, the old explicit-offset pwrite would overwrite
    # the just-written payload with zeros, disarming it. Padding via
    # O_APPEND instead means the pad can only add bytes past whatever the
    # file's CURRENT end is when the write syscall actually runs (the
    # kernel re-reads that then, not our stale `size`) -- it can never
    # overwrite bytes a racing write() already put at the front of the
    # file, so the worst case is a harmless over-length file.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.path.write_bytes(b"")  # short/armed, as in the R3 crash scenario
    real_open = os.open
    raced = []
    def racing_open(path, flags, *a, **kw):
        # Inject after prepare() captured the short size but before it opens
        # the padding descriptor. Path.exists() calls Path.stat() on some
        # Python versions, so a Path.stat hook can instead set the marker
        # before the initial payload check and miss the intended race.
        if os.fspath(path) == os.fspath(m.path) and flags & os.O_APPEND and not raced:
            raced.append(True)
            StopMarker(m.path).write("HARD", "HOLD", "raced in")
        return real_open(path, flags, *a, **kw)
    monkeypatch.setattr(os, "open", racing_open)

    m.prepare()

    from pineforge_live.journal.sidecar import SIZE
    assert raced == [True]
    assert m.path.stat().st_size == 2 * SIZE  # STOP payload, then stale-size pad.
    assert m.exists()  # the race's SET marker survived the pad -- not disarmed
    assert m.read()["cause"] == "raced in"


def test_stop_marker_payload_normalises_unicode_decode_error(tmp_path):
    # R9: json.loads on bytes runs json.detect_encoding first, which reads
    # a head starting with NUL bytes as a UTF-32 BOM heuristic and can raise
    # UnicodeDecodeError -- a ValueError, but not a json.JSONDecodeError --
    # on a torn/garbage payload. Must normalize to "unreadable" the same as
    # a JSONDecodeError does, not escape raw.
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    m.path.write_bytes(b"\0" * 50 + b"xyz")
    r = m.read()
    assert r is not None and r.get("unreadable") is True
    with pytest.raises(StopMarkerPresent):
        Journal.open(tmp_path / "j.sqlite3", stop_marker=m)


def test_open_refuses_a_file_with_no_schema_meta_table_without_mutating_it(tmp_path):
    # R10: a GENUINELY pre-versioning file (no schema_meta table at all, not
    # just an empty one -- built here already in WAL/synchronous=FULL mode,
    # exactly as a real one created by an earlier version of this same code
    # would be) must be refused BEFORE executescript(DDL) runs, so the
    # refused file comes back byte-for-byte unchanged instead of gaining an
    # empty schema_meta table moments before being rejected.
    p = tmp_path / "j.sqlite3"
    ddl_without_schema_meta = "\n".join(
        line for line in DDL.strip().splitlines() if "schema_meta" not in line
    )
    con = sqlite3.connect(p, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript(ddl_without_schema_meta)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fully settled: nothing left for a later connection to checkpoint
    con.close()
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(JournalFault, match="no schema_meta table"):
        Journal.open(p, create=False)
    after = hashlib.sha256(p.read_bytes()).hexdigest()
    assert before == after


def test_open_refuses_a_pre_versioning_delete_mode_file_without_mutating_it(tmp_path):
    # NF5: `PRAGMA journal_mode=WAL` rewrites the database file header (and
    # creates -wal/-shm siblings) even on a file about to be refused -- the
    # R10 test above builds its fixture already in WAL mode, which cannot
    # catch that. A genuinely pre-schema-versioning file sitting in
    # SQLite's default DELETE journal mode (as any such file predating WAL
    # adoption in this codebase would) must come back byte-for-byte
    # unchanged, so the schema_meta-table check has to run BEFORE that
    # PRAGMA, not after.
    p = tmp_path / "j.sqlite3"
    ddl_without_schema_meta = "\n".join(
        line for line in DDL.strip().splitlines() if "schema_meta" not in line
    )
    con = sqlite3.connect(p, isolation_level=None)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "delete"  # sqlite's default -- the pre-WAL-adoption shape
    con.executescript(ddl_without_schema_meta)
    con.close()
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(JournalFault, match="no schema_meta table"):
        Journal.open(p, create=False)
    after = hashlib.sha256(p.read_bytes()).hexdigest()
    assert before == after
    assert not (tmp_path / "j.sqlite3-wal").exists()


def test_update_check_expiry_returns_rowcount(tmp_path):
    # rowcount: the implementer's own concern from task-4-rereview-3.md --
    # a silent no-op UPDATE would let renew() believe it extended a lease
    # nothing durable backs.
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_check(1, 1_000)
    assert j.update_check_expiry(1, 5_000) == 1
    assert j.update_check_expiry(999, 5_000) == 0  # no such fencing_token
    j.close()


def test_renew_raises_lease_lost_when_checks_row_is_gone(tmp_path):
    # rowcount: if the checks row acquire() wrote has vanished by the time
    # renew() runs, update_check_expiry's rowcount is 0 -- renew() must not
    # silently believe the lease extended.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    a.acquire(lease_ms=10_000, now_ms=0)
    j.con.execute("DELETE FROM checks WHERE fencing_token=?", (a.token,))
    with pytest.raises(LeaseLost):
        a.renew(now_ms=100, lease_ms=10_000)
    assert a.token is None
    j.close()


def test_renew_leaves_expiry_ms_unchanged_when_update_check_expiry_fails(tmp_path, monkeypatch):
    # R8: self.expiry_ms must only advance AFTER both the checks-row UPDATE
    # and the lock-file write succeed. Previously it was assigned FIRST, so
    # a JournalFault from update_check_expiry (e.g. "database is locked" --
    # a far more plausible failure than the os.replace() this ordering
    # originally guarded against) left this holder believing a lease
    # nobody recorded: the lock file and the `checks` row would still say
    # the OLD expiry while self.expiry_ms/self.expired() said otherwise.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    a.acquire(lease_ms=1_000, now_ms=0)  # expiry_ms == 1_000
    before_expiry = a.expiry_ms
    def boom(*_a, **_kw):
        raise JournalFault("database is locked")
    monkeypatch.setattr(j, "update_check_expiry", boom)
    with pytest.raises(JournalFault):
        a.renew(now_ms=900, lease_ms=1_000)
    assert a.expiry_ms == before_expiry
    assert json.loads(lock.read_text())["expiry_ms"] == before_expiry
    j.close()


def test_expired_and_live_check_boundary_agree_at_exact_expiry(tmp_path):
    # Final 8: expired() must agree with acquire()'s/live_check()'s "live
    # iff expiry_ms > now_ms" AT THE BOUNDARY. Previously expired() used
    # `now_ms > expiry_ms` (NOT expired at now==expiry) while a contender's
    # acquire() already treated now==expiry as NOT live -- a one-
    # millisecond window where the holder believed itself live while a
    # contender could already take over.
    j = Journal.open(tmp_path / "j.sqlite3")
    lock = tmp_path / "j.lock"
    a = FencedLease(lock, j)
    a.acquire(lease_ms=1_000, now_ms=0)  # expiry_ms == 1_000
    assert a.expired(999) is False
    assert a.expired(1_000) is True  # boundary: now == expiry is expired, not "still live"
    b = FencedLease(lock, j)
    assert b.acquire(lease_ms=1_000, now_ms=1_000) == a.token + 1  # a contender agrees
    j.close()


def test_insert_checksummed_rolls_back_on_baseexception_not_just_exception(tmp_path, monkeypatch):
    # Final 9: a KeyboardInterrupt/SystemExit landing between BEGIN
    # IMMEDIATE and COMMIT is not an Exception -- `except Exception` let it
    # propagate through the still-open transaction, so the connection's
    # NEXT append failed with "cannot start a transaction within a
    # transaction". R6 hardened Journal.open() for BaseException; this
    # closes the same gap on the write path.
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")

    def boom(row):
        raise KeyboardInterrupt()
    monkeypatch.setattr(journal_mod, "checksum", boom)
    with pytest.raises(KeyboardInterrupt):
        j.append_settlement(settlement(1))
    monkeypatch.undo()

    # No leaked open transaction: a normal append right after succeeds.
    j.append_settlement(settlement(1))
    assert len(j.rows("settlements", "1=1", ())) == 1
    j.close()


def test_settlement_requires_trades_sha256(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3"); j.append_epoch("e1", "{}")
    row = {"bar_index": 1, "epoch_hash": "e1", "runtime_config_hash": "rc", "bars_hash": 1, "broker_state_hash": 2,
           "trades_len": 0, "position": 0.0, "equity": 1.0}
    with pytest.raises(JournalFault):
        j.append_settlement(row)
    j.append_settlement({**row, "trades_sha256": "0" * 64})
    assert j.last_settlement("e1")["trades_sha256"] == "0" * 64
    j.close()


@pytest.mark.parametrize('position,equity',[(0.0,10000.0),(-0.0,10000.0),(31.0,99800.5)])
def test_checksum_reads_stored_affinity_after_older_sqlite_returning(tmp_path,monkeypatch,position,equity):
    j=Journal.open(tmp_path/'old-returning.sqlite3')
    original=j._exec
    class ReturningCursor:
        def __init__(self,cur):self.cur=cur;self.lastrowid=cur.lastrowid
        def fetchone(self):
            raw=self.cur.fetchone()
            if raw is None:return None
            # SQLite3.37/3.40 exposes exact integral REAL columns as int in
            # RETURNING, though subsequent SELECT observes REAL affinity.
            return {k:int(v) if isinstance(v,float) and v.is_integer() else v for k,v in dict(raw).items()}
    def execute(sql,params=()):
        cursor=original(sql,params)
        return ReturningCursor(cursor) if 'RETURNING *' in sql else cursor
    monkeypatch.setattr(j,'_exec',execute)
    j.append_settlement(settlement(1,position=position,equity=equity))
    j.append_settlement(settlement(1,position=position,equity=equity))
    j.verify_tail();j.close()
    reopened=Journal.open(tmp_path/'old-returning.sqlite3');reopened.verify_tail();reopened.close()
