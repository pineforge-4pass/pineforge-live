import json, os, sqlite3
import pytest
from pineforge_live.journal import (
    Journal, StopMarker, FencedLease, JournalCorrupt, JournalConflict,
    JournalFault, LeaseHeld, LeaseLost, StopMarkerPresent,
)
from pineforge_live import types as T

def settlement(i, epoch="e1", **overrides):
    row = {"bar_index": i, "epoch_hash": epoch, "runtime_config_hash": "rc", "bars_hash": 123 + i,
           "broker_state_hash": 456 + i, "trades_len": i, "position": 0.0, "equity": 1000.0 + i}
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
