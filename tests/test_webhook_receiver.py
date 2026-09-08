"""Exercise the example through real loopback HTTP, including durable replay."""
import asyncio
import hashlib
import hmac
import http.client
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("webhook_receiver", Path(__file__).parents[1] / "examples/webhook_receiver.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def body(event_id="evt-1", contracts=1):
    return json.dumps({"schema_version": 1, "event_id": event_id, "event": "order_action",
                       "order": {"id": "L", "action": "buy", "contracts": contracts}}).encode()


@contextmanager
def running(database, *, secret=None, on_record=None):
    server = receiver.Receiver(("127.0.0.1", 0), database, secret=secret, on_record=on_record)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def post(server, payload, *, secret=None, headers=None, path="/webhook"):
    merged = {"Content-Type": "application/json"}
    if secret is not None:
        merged[receiver.SIGNATURE_HEADER] = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    merged.update(headers or {})
    con = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        con.request("POST", path, body=payload, headers=merged)
        response = con.getresponse()
        return response.status, json.loads(response.read())
    finally:
        con.close()


def count(database):
    with sqlite3.connect(database) as db:
        return db.execute("SELECT count(*) FROM webhook_receipts").fetchone()[0]


def test_signed_delivery_duplicate_and_restart_are_one_durable_intent(tmp_path):
    database, seen = tmp_path / "receipts.sqlite3", []
    with running(database, secret=b"example-key", on_record=seen.append) as server:
        status, result = post(server, body(), secret=b"example-key")
        assert status == 200 and result == {"event_id": "evt-1", "recorded": True, "duplicate": False}
        assert count(database) == 1
        assert post(server, body(), secret=b"example-key")[1]["duplicate"] is True
    with running(database, secret=b"example-key", on_record=seen.append) as restarted:
        assert post(restarted, body(), secret=b"example-key")[1]["duplicate"] is True
        assert post(restarted, body(contracts=2), secret=b"example-key")[0] == 409
    assert count(database) == 1 and len(seen) == 1


def test_missing_wrong_and_body_mismatched_hmac_never_records(tmp_path):
    database = tmp_path / "receipts.sqlite3"
    with running(database, secret=b"key") as server:
        assert post(server, body())[0] == 401
        assert post(server, body(), secret=b"wrong")[0] == 401
        signature = "sha256=" + hmac.new(b"key", body(), hashlib.sha256).hexdigest()
        assert post(server, body(contracts=2), headers={receiver.SIGNATURE_HEADER: signature})[0] == 401
    assert count(database) == 0


def test_intrabar_update_is_recorded_as_notification(tmp_path):
    database, seen = tmp_path / "receipts.sqlite3", []
    event = json.loads(body("update-1"))
    event.update(event="order_update", original_event_id="evt-1", status="retracted")
    with running(database, on_record=seen.append) as server:
        payload = json.dumps(event).encode()
        assert post(server, payload)[0] == 200
        assert post(server, payload)[1]["duplicate"] is True
        assert post(server, body(), headers={"Idempotency-Key": "wrong"})[0] == 400
    assert len(seen) == count(database) == 1


@pytest.mark.parametrize("payload", [b"{", b"[]", b"\xff", b'{}',
    b'{"schema_version":true,"event_id":"x","event":"order_action"}',
    b'{"schema_version":1,"event_id":"x","event":[]}',
    b'{"schema_version":1,"event_id":"x","event_id":"y","event":"order_action"}',
    b'{"schema_version":1,"event_id":"x","event":"order_action","qty":NaN}'])
def test_malformed_events_do_not_get_an_ack_or_receipt(tmp_path, payload):
    database = tmp_path / "receipts.sqlite3"
    with running(database) as server:
        assert post(server, payload)[0] == 400
    assert count(database) == 0


def test_concurrent_retries_commit_and_invoke_once(tmp_path):
    database, seen = tmp_path / "receipts.sqlite3", []
    with running(database, on_record=seen.append) as server:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: post(server, body()), range(16)))
        assert all(status == 200 for status, _ in results)
        assert sum(not result["duplicate"] for _, result in results) == 1
    assert count(database) == len(seen) == 1


def test_commit_before_lost_ack_is_adopted_on_restart(tmp_path):
    database, seen = tmp_path / "receipts.sqlite3", []
    # The committed receipt is exactly the crash boundary before HTTP ACK.
    script = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('receiver', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
payload = sys.argv[3].encode()
module.ReceiptStore(sys.argv[2]).record(module.parse_event(payload), payload)
os._exit(17)
"""
    crashed = subprocess.run([sys.executable, "-c", script, str(Path(receiver.__file__)),
                              str(database), body().decode()], capture_output=True, text=True)
    assert crashed.returncode == 17, crashed.stderr
    with running(database, on_record=seen.append) as server:
        assert post(server, body())[1]["duplicate"] is True
    assert count(database) == 1 and seen == []


def test_write_failure_returns_retryable_failure_not_ack(tmp_path, monkeypatch):
    with running(tmp_path / "receipts.sqlite3") as server:
        def fail(*args):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(server.store, "record", fail)
        assert post(server, body()) == (503, {"error": "receipt_not_committed"})


def test_receiver_bounds_route_size_and_empty_secret(tmp_path):
    with running(tmp_path / "receipts.sqlite3") as server:
        assert post(server, body(), path="/other")[0] == 404
        assert post(server, b"", headers={"Content-Length": str(receiver.MAX_BODY_BYTES + 1)})[0] == 413
    with pytest.raises(ValueError, match="empty"):
        receiver.Receiver(("127.0.0.1", 0), tmp_path / "other.sqlite3", secret=b"")


def test_real_dispatcher_and_example_receiver_share_signed_event_contract(tmp_path, monkeypatch):
    from pineforge_live.adapters.tape import TapeClock
    from pineforge_live.config import WebhookConfig
    from pineforge_live.journal.journal import Journal
    from pineforge_live.webhooks.delivery import Dispatcher
    from pineforge_live.webhooks.store import Outbox

    monkeypatch.setenv("PINEFORGE_RECEIVER_TEST_SECRET", "shared-test-secret")
    database, seen = tmp_path / "receipts.sqlite3", []
    with running(database, secret=b"shared-test-secret", on_record=seen.append) as server:
        url = f"http://127.0.0.1:{server.server_port}/webhook"
        journal = Journal.open(tmp_path / "sender.sqlite3")
        try:
            outbox = Outbox(journal, "e" * 64, url)
            event = json.loads(body())
            event["message"] = "Unicode Pine ID: 多單"
            outbox.enqueue(event["event_id"], event, 1000)
            config = WebhookConfig(url, secret_env="PINEFORGE_RECEIVER_TEST_SECRET")
            dispatcher = Dispatcher(outbox, config, TapeClock(1000))
            report = asyncio.run(dispatcher.drain())
            assert report.delivered == 1 and report.pending == report.failed == 0
            assert outbox.inspect()[0]["state"] == "DELIVERED"
            assert journal.rows("fills", "1=1", ()) == []
            assert journal.rows("actions", "1=1", ()) == []
        finally:
            journal.close()
    assert count(database) == 1 and seen == [event]
