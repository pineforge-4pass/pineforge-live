"""Loopback HTTP integration plus persistence/fault tests; no broker activity."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pineforge_live.journal.journal import Journal, JournalConflict, JournalCorrupt
from pineforge_live.webhooks.delivery import Dispatcher, HttpResponse, validate_target_url
from pineforge_live.webhooks.store import Outbox, initialize


@dataclass(frozen=True)
class Config:
    target_url: str
    secret_env: str | None = None
    timeout_ms: int = 1000
    max_attempts: int = 3
    backoff_initial_ms: int = 100
    backoff_max_ms: int = 1000
    retry_after_max_ms: int = 3000
    allow_insecure_http: bool = False


class Clock:
    def __init__(self):
        self.time = 10000

    def now_ms(self):
        return self.time


@contextmanager
def receiver(outcomes):
    """Collect actual HTTP requests; None accepts then loses its HTTP response."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(dict(body=body, headers=dict(self.headers), path=self.path))
            outcome = outcomes.pop(0)
            if outcome is None:
                self.close_connection = True
                return
            code, headers = outcome if isinstance(outcome, tuple) else (outcome, {})
            self.send_response(code)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/orders", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def journal(tmp_path):
    j = Journal.open(tmp_path / "webhooks.db")
    yield j
    j.close()


def enqueue(outbox, event_id="event-1"):
    return outbox.enqueue(event_id, {"event_id": event_id, "action": {"side": "buy", "qty": 1}}, 1)


def test_real_http_signed_delivery_is_never_a_fill(journal, monkeypatch):
    secret = "never-persist-this-test-signing-secret"
    monkeypatch.setenv("PF_TEST_WEBHOOK_SECRET", secret)
    with receiver([204]) as (url, requests):
        box = Outbox(journal, "epoch", url)
        row = enqueue(box)
        report = asyncio.run(Dispatcher(box, Config(url, "PF_TEST_WEBHOOK_SECRET"), Clock()).drain())
    assert (report.attempted, report.delivered, report.pending, report.failed) == (1, 1, 0, 0)
    assert requests[0]["body"] == row["payload_json"].encode()
    headers = {k.lower(): v for k, v in requests[0]["headers"].items()}
    assert headers["content-type"] == "application/json"
    assert headers["idempotency-key"] == headers["x-pineforge-event-id"] == "event-1"
    assert headers["x-pineforge-signature"] == "sha256=" + hmac.new(
        secret.encode(), requests[0]["body"], hashlib.sha256).hexdigest()
    assert box.inspect()[0]["state"] == "DELIVERED"
    assert journal._exec("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    # Includes committed WAL rows, not merely the main database file.
    assert secret not in "\n".join(journal.con.iterdump())
    assert "sha256=" not in "\n".join(journal.con.iterdump())


def test_response_loss_retries_exact_same_bytes_and_id_across_restart(tmp_path):
    path = tmp_path / "restart.db"
    clock = Clock()
    with receiver([None, 200, 200]) as (url, requests):
        j = Journal.open(path)
        box = Outbox(j, "epoch", url)
        enqueue(box)
        enqueue(box, "event-2")
        first = asyncio.run(Dispatcher(box, Config(url), clock).drain())
        assert (first.delivered, first.pending, len(requests)) == (0, 2, 1)
        assert box.inspect()[0]["state"] == "RETRY"
        j.close()
        j = Journal.open(path)
        try:
            box = Outbox(j, "epoch", url)
            clock.time += 100
            second = asyncio.run(Dispatcher(box, Config(url), clock).drain())
            assert (second.delivered, second.pending) == (2, 0)
            assert [r["total_attempts"] for r in box.inspect()] == [2, 1]
        finally:
            j.close()
    assert requests[0]["body"] == requests[1]["body"]
    keys = [{k.lower(): v for k, v in r["headers"].items()}["idempotency-key"] for r in requests]
    assert keys == ["event-1", "event-1", "event-2"]


def test_transient_retry_after_cap_blocks_later_events(journal):
    clock = Clock()
    with receiver([(429, {"Retry-After": "999999"}), 200, 200]) as (url, requests):
        box = Outbox(journal, "epoch", url)
        enqueue(box)
        enqueue(box, "event-2")
        dispatcher = Dispatcher(box, Config(url), clock)
        assert asyncio.run(dispatcher.drain()).pending == 2
        assert box.inspect()[0]["next_attempt_ms"] == 13000
        assert asyncio.run(dispatcher.drain()).attempted == 0
        clock.time = 12999
        assert asyncio.run(dispatcher.drain()).attempted == 0
        clock.time = 13000
        assert asyncio.run(dispatcher.drain()).delivered == 2
        assert len(requests) == 3


@pytest.mark.parametrize("failure", [400, 401, 403, 404, 409, 422, 301, 307])
def test_permanent_failure_requires_explicit_retry_or_skip(journal, failure):
    with receiver([failure, 200, 200]) as (url, requests):
        box = Outbox(journal, "epoch", url)
        enqueue(box)
        enqueue(box, "event-2")
        dispatcher = Dispatcher(box, Config(url), Clock())
        first = asyncio.run(dispatcher.drain())
        assert (first.failed, first.pending) == (1, 1)
        assert asyncio.run(dispatcher.drain()).attempted == 0
        box.retry("event-1")
        assert asyncio.run(dispatcher.drain()).delivered == 2
        assert len(requests) == 3


def test_redirect_never_discloses_payload_or_signature(journal, monkeypatch):
    monkeypatch.setenv("PF_TEST_SECRET", "signing-secret")
    with receiver([]) as (other_url, other_requests):
        with receiver([(307, {"Location": other_url})]) as (url, requests):
            box = Outbox(journal, "epoch", url)
            enqueue(box)
            report = asyncio.run(Dispatcher(box, Config(url, "PF_TEST_SECRET"), Clock()).drain())
    assert report.failed == 1
    assert len(requests) == 1 and other_requests == []


def test_bounded_retries_then_audited_skip(journal):
    with receiver([503, 503, 503, 200]) as (url, requests):
        box = Outbox(journal, "epoch", url)
        enqueue(box)
        enqueue(box, "event-2")
        clock = Clock()
        dispatcher = Dispatcher(box, Config(url), clock)
        for attempt in range(3):
            report = asyncio.run(dispatcher.drain())
            clock.time += 1000
        assert (report.failed, report.pending) == (1, 1)
        assert asyncio.run(dispatcher.drain()).attempted == 0
        box.skip("event-1", "receiver rejected obsolete action", clock.time)
        assert asyncio.run(dispatcher.drain()).delivered == 1
    assert box.inspect()[0]["state"] == "SKIPPED"
    assert len(requests) == 4
    assert journal._exec("SELECT COUNT(*) FROM webhook_delivery WHERE event_id='event-1'").fetchone()[0] == 8


def test_crash_after_attempt_is_unknown_and_retries_same_id(journal):
    class Transport:
        async def post(self, url, body, headers, timeout_ms):
            assert headers["Idempotency-Key"] == "event-1"
            return HttpResponse(202)

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    box.begin_attempt("event-1", 2)
    box = Outbox(journal, "epoch", box.target_url)
    report = asyncio.run(Dispatcher(box, Config(box.target_url), Clock(), Transport()).drain())
    assert report.delivered == 1
    assert box.inspect()[0]["total_attempts"] == 2


def test_enqueue_is_atomic_with_engine_decision_and_conflicts_refuse(journal):
    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    with pytest.raises(RuntimeError):
        with journal.transaction():
            enqueue(box)
            raise RuntimeError("decision failed")
    assert box.inspect() == []
    original = enqueue(box)
    assert box.enqueue("event-1", original["payload"], 999)["sequence"] == original["sequence"]
    with pytest.raises(JournalConflict, match="different payload"):
        box.enqueue("event-1", {"different": True}, 2)
    with pytest.raises(JournalConflict, match="target changed"):
        Outbox(journal, "epoch", "https://other.example/orders")
    with pytest.raises(JournalConflict, match="another epoch"):
        enqueue(Outbox(journal, "other-epoch", box.target_url))


def test_initialization_never_commits_callers_transaction(journal):
    with pytest.raises(RuntimeError):
        with journal.transaction():
            initialize(journal)
            assert journal.con.in_transaction
            raise RuntimeError("roll back")
    assert journal._exec("SELECT 1 FROM sqlite_master WHERE name='webhook_events'").fetchone() is None


def test_payload_and_audit_append_only_and_checksums_verified(journal):
    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    with pytest.raises(Exception, match="append-only"):
        journal._exec("UPDATE webhook_events SET payload_json='{}'")
    with pytest.raises(Exception, match="append-only"):
        journal._exec("DELETE FROM webhook_delivery")
    journal._exec("DROP TRIGGER webhook_events_no_update")
    journal._exec("UPDATE webhook_events SET payload_json='{}'")
    with pytest.raises(JournalCorrupt, match="checksum"):
        Outbox(journal, "epoch", box.target_url)


def test_no_delivery_inside_transaction_or_after_lease_refusal(journal):
    class Transport:
        async def post(self, *_):
            pytest.fail("network must not run")

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    dispatcher = Dispatcher(box, Config(box.target_url), Clock(), Transport(), lambda horizon: False)
    with journal.transaction(), pytest.raises(RuntimeError, match="inside a journal"):
        asyncio.run(dispatcher.drain())
    with pytest.raises(RuntimeError, match="lease check refused"):
        asyncio.run(dispatcher.drain())
    assert box.inspect()[0]["attempts"] == 0


def test_timeout_redacts_exception_and_persists_retry(journal):
    class Transport:
        async def post(self, *_):
            raise OSError("do not log https://secret.example/token and signature=abc")

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    report = asyncio.run(Dispatcher(box, Config(box.target_url), Clock(), Transport()).drain())
    assert report.pending == 1 and "unknown" in report.last_error
    dump = "\n".join(journal.con.iterdump())
    assert "secret.example" not in dump and "signature=abc" not in dump


def test_cancelled_delivery_survives_as_unknown(journal):
    class Transport:
        async def post(self, *_):
            raise asyncio.CancelledError()

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Dispatcher(box, Config(box.target_url), Clock(), Transport()).drain())
    assert box.inspect()[0]["state"] == "INFLIGHT"
    assert box.pending(20000)[0]["event_id"] == "event-1"


def test_concurrent_dispatchers_serialize_per_journal(journal):
    calls = []

    class Transport:
        async def post(self, url, body, headers, timeout_ms):
            calls.append(headers["Idempotency-Key"])
            await asyncio.sleep(0.01)
            return HttpResponse(200)

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    enqueue(box, "event-2")

    async def run():
        first = Dispatcher(box, Config(box.target_url), Clock(), Transport())
        second = Dispatcher(Outbox(journal, "epoch", box.target_url), Config(box.target_url), Clock(), Transport())
        return await asyncio.gather(first.drain(), second.drain())

    reports = asyncio.run(run())
    assert calls == ["event-1", "event-2"]
    assert sum(r.delivered for r in reports) == 2


@pytest.mark.parametrize("url", ["ftp://example.test", "http://example.test", "https://user:secret@example.test",
                                "https://example.test/#fragment", "https://example.test/\nheader", "https://"])
def test_unsafe_targets_rejected(url):
    with pytest.raises(ValueError):
        validate_target_url(url)


@pytest.mark.parametrize("url", ["https://example.test/orders", "http://localhost:9000/orders",
                                "http://127.0.0.1:9000", "http://[::1]:9000"])
def test_https_and_literal_loopback_targets_allowed(url):
    assert validate_target_url(url) == url


def test_remote_http_requires_explicit_override():
    assert validate_target_url("http://example.test", True) == "http://example.test"


def test_missing_signing_secret_refuses_before_attempt(journal, monkeypatch):
    monkeypatch.delenv("PF_MISSING_SECRET", raising=False)
    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    with pytest.raises(ValueError, match="signing secret is missing"):
        Dispatcher(box, Config(box.target_url, "PF_MISSING_SECRET"), Clock())
    assert box.inspect()[0]["attempts"] == 0


def test_async_lease_refusal_does_not_attempt(journal):
    async def refuse(horizon):
        assert horizon == 1000
        return False

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    with pytest.raises(RuntimeError, match="lease check refused"):
        asyncio.run(Dispatcher(box, Config(box.target_url), Clock(), lease_check=refuse).drain())
    assert box.inspect()[0]["attempts"] == 0


def test_crash_on_last_attempt_exhausts_budget_before_more_network(journal):
    class Transport:
        async def post(self, *_):
            pytest.fail("retry budget already exhausted")

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    box.begin_attempt("event-1", 2)
    report = asyncio.run(Dispatcher(box, Config(box.target_url, max_attempts=1), Clock(), Transport()).drain())
    assert report.failed == 1 and report.attempted == 0
    box.retry("event-1")
    assert box.inspect()[0]["attempts"] == 0
    assert box.inspect()[0]["total_attempts"] == 1


def test_injected_transport_has_bounded_timeout(journal):
    class Transport:
        async def post(self, *_):
            await asyncio.Event().wait()

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    report = asyncio.run(Dispatcher(box, Config(box.target_url, timeout_ms=10), Clock(), Transport()).drain())
    assert report.pending == 1 and report.attempted == 1
    assert box.inspect()[0]["state"] == "RETRY"


def test_retry_after_date_and_malformed_value(journal):
    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    dispatcher = Dispatcher(box, Config(box.target_url), Clock())
    assert dispatcher._retry_delay(1, HttpResponse(429, {"Retry-After": "Thu, 01 Jan 1970 00:00:12 GMT"}), 10000) == 2000
    assert dispatcher._retry_delay(1, HttpResponse(429, {"Retry-After": "malformed"}), 10000) == 100
    assert dispatcher._retry_delay(3, HttpResponse(503), 10000) == 400


def test_payload_event_identity_and_non_json_numbers_refused(journal):
    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    with pytest.raises(ValueError, match="event_id differs"):
        box.enqueue("event-1", {"event_id": "event-2"}, 1)
    with pytest.raises(ValueError):
        box.enqueue("event-1", {"qty": float("nan")}, 1)
    with pytest.raises(ValueError, match="invalid webhook event id"):
        box.enqueue("event\r\nInjected: header", {}, 1)
    assert box.inspect() == []


def test_older_epoch_backlog_cannot_be_overtaken_on_same_target(journal):
    target = "https://receiver.example/orders"
    older = Outbox(journal, "epoch-1", target)
    enqueue(older, "old-event")
    newer = Outbox(journal, "epoch-2", target)
    enqueue(newer, "new-event")
    with pytest.raises(JournalConflict, match="another epoch precedes"):
        newer.pending(10000)
    older.skip("old-event", "explicit operator decision")
    assert newer.pending(10000)[0]["event_id"] == "new-event"
    assert older.pending(10000) == []


def test_lease_is_rechecked_after_attempt_commit_before_network(journal, monkeypatch):
    calls = []
    clock = Clock()
    expiry_ms = 12000

    class Transport:
        async def post(self, url, body, headers, timeout_ms):
            calls.append(headers["Idempotency-Key"])
            return HttpResponse(200)

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    original = box.begin_attempt

    def delayed_commit(event_id, now_ms):
        row = original(event_id, now_ms)
        # SQLite contention/fsync consumed the horizon admitted by preflight.
        clock.time = 11500
        return row

    def lease(horizon_ms):
        assert not journal.con.in_transaction
        return clock.now_ms() + horizon_ms < expiry_ms

    monkeypatch.setattr(box, "begin_attempt", delayed_commit)
    dispatcher = Dispatcher(box, Config(box.target_url), clock, Transport(), lease)
    with pytest.raises(RuntimeError, match="lease check refused"):
        asyncio.run(dispatcher.drain())
    assert calls == []
    assert box.inspect()[0]["state"] == "INFLIGHT"
    assert box.inspect()[0]["total_attempts"] == 1
    # A new authorized owner adopts the same immutable event and retries it.
    expiry_ms = 20000
    assert asyncio.run(dispatcher.drain()).delivered == 1
    assert calls == ["event-1"]
    assert box.inspect()[0]["total_attempts"] == 2


def test_late_ack_is_recorded_but_expiry_blocks_the_next_post(journal):
    calls = []
    clock = Clock()

    class Transport:
        async def post(self, url, body, headers, timeout_ms):
            calls.append(headers["Idempotency-Key"])
            clock.time = 20000
            return HttpResponse(200)

    box = Outbox(journal, "epoch", "https://receiver.example/orders")
    enqueue(box)
    enqueue(box, "event-2")
    dispatcher = Dispatcher(box, Config(box.target_url), clock, Transport(),
                            lambda horizon: clock.now_ms() + horizon < 12000)
    with pytest.raises(RuntimeError, match="lease check refused"):
        asyncio.run(dispatcher.drain())
    assert calls == ["event-1"]
    assert [row["state"] for row in box.inspect()] == ["DELIVERED", "PENDING"]
