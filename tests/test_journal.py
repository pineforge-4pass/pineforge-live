import json, os, sqlite3
import pytest
from pineforge_live.journal import Journal, StopMarker, FencedLease, checksum, JournalCorrupt, LeaseHeld

def settlement(i, epoch="e1"):
    return {"bar_index": i, "epoch_hash": epoch, "runtime_config_hash": "rc", "bars_hash": 123 + i,
            "broker_state_hash": 456 + i, "trades_len": i, "position": 0.0, "equity": 1000.0 + i}

def test_settlement_idempotent_and_checksummed(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3")
    j.append_epoch("e1", "{}")
    j.append_settlement(settlement(1)); j.append_settlement(settlement(1))   # idempotent
    rows = j.rows("settlements", "epoch_hash=?", ("e1",))
    assert len(rows) == 1 and rows[0]["checksum"] == checksum({k: v for k, v in rows[0].items() if k != "checksum"})
    assert j.last_settlement("e1")["bar_index"] == 1
    j.close()

def test_verify_tail_refuses_bad_checksum(tmp_path):
    p = tmp_path / "j.sqlite3"
    j = Journal.open(p); j.append_epoch("e1", "{}"); j.append_settlement(settlement(1)); j.append_settlement(settlement(2)); j.close()
    con = sqlite3.connect(p); con.execute("UPDATE settlements SET equity = equity + 1 WHERE bar_index = 2"); con.commit(); con.close()
    with pytest.raises(JournalCorrupt):
        Journal.open(p, create=False).verify_tail()

def test_write_ahead_action_and_non_terminal_query(tmp_path):
    j = Journal.open(tmp_path / "j.sqlite3"); j.append_epoch("e1", "{}")
    j.append_action({"client_id": "c1", "intent_key": "L|ENTRY||1", "action_seq": 1, "level_version": 0,
                     "run_token": 7, "cls": "TRIGGER", "lane": "DISCRETIONARY", "payload_json": "{}", "epoch_hash": "e1"})
    assert [a["client_id"] for a in j.actions_non_terminal()] == ["c1"]
    j.update_order_state("c1", json.dumps({"status": "FILLED"}), terminal=True)
    assert j.actions_non_terminal() == []
    j.close()

def test_stop_marker_durability_and_refusal(tmp_path):
    m = StopMarker(tmp_path / "j.sqlite3.stop")
    assert not m.exists()
    m.write("HARD", "HOLD", "journal fault")
    assert m.exists() and os.path.getsize(m.path) >= 4096
    assert m.read()["level"] == "HARD"
    with pytest.raises(JournalCorrupt):
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
