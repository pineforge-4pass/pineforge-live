"""Durable webhook events and an append-only delivery audit.

Enqueue joins the engine decision transaction. Delivery records describe HTTP
delivery only: they never represent an exchange order or an execution fill.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re

from pineforge_live.journal.journal import JournalConflict, JournalCorrupt


def canonical_payload(payload: dict) -> bytes:
    if not isinstance(payload, dict):
        raise ValueError("webhook payload must be a JSON object")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: dict) -> str:
    return hashlib.sha256(canonical_payload(value)).hexdigest()


@contextlib.contextmanager
def transaction(journal):
    if journal.con.in_transaction:
        yield
    else:
        with journal.transaction():
            yield


_TABLES = (
    """CREATE TABLE IF NOT EXISTS webhook_targets (
        epoch_hash TEXT PRIMARY KEY, target_url TEXT NOT NULL,
        target_hash TEXT NOT NULL, record_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS webhook_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
        epoch_hash TEXT NOT NULL, target_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL, created_ms INTEGER NOT NULL, record_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS webhook_delivery (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL, total_attempts INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL, next_attempt_ms INTEGER NOT NULL,
        http_status INTEGER, error TEXT, previous_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS webhook_events_epoch ON webhook_events(epoch_hash, sequence)",
    "CREATE INDEX IF NOT EXISTS webhook_delivery_event ON webhook_delivery(event_id, id)",
)


def initialize(journal) -> None:
    """Create extension tables without executescript's implicit transaction commit."""
    with transaction(journal):
        for statement in _TABLES:
            journal._exec(statement)
        for table in ("webhook_targets", "webhook_events", "webhook_delivery"):
            for mutation in ("UPDATE", "DELETE"):
                journal._exec(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{mutation.lower()} "
                    f"BEFORE {mutation} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'webhook audit is append-only'); END"
                )


def _check(row, exclude=()) -> dict:
    result = dict(row)
    expected = _digest({k: v for k, v in result.items()
                        if k not in {"record_hash", *exclude}})
    if result["record_hash"] != expected:
        raise JournalCorrupt("webhook record checksum mismatch")
    return result


class Outbox:
    def __init__(self, journal, epoch_hash: str, target_url: str):
        if not epoch_hash or not isinstance(epoch_hash, str):
            raise ValueError("epoch hash is required")
        if not isinstance(target_url, str) or not target_url:
            raise ValueError("webhook target is required")
        self.j, self.epoch_hash, self.target_url = journal, epoch_hash, target_url
        self.target_hash = hashlib.sha256(target_url.encode("utf-8")).hexdigest()
        initialize(journal)
        with transaction(journal):
            target = journal._exec("SELECT * FROM webhook_targets WHERE epoch_hash=?",
                                   (epoch_hash,)).fetchone()
            if target is None:
                value = dict(epoch_hash=epoch_hash, target_url=target_url,
                             target_hash=self.target_hash)
                journal._exec("INSERT INTO webhook_targets VALUES(?,?,?,?)",
                              (*value.values(), _digest(value)))
            elif _check(target)["target_url"] != target_url:
                raise JournalConflict("webhook target changed for existing epoch")
        self.inspect()  # Validate all prior events and transitions on restart.

    def _event(self, event_id: str) -> dict | None:
        row = self.j._exec("SELECT * FROM webhook_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        result = _check(row, ("sequence",))
        if result["epoch_hash"] != self.epoch_hash or result["target_hash"] != self.target_hash:
            raise JournalConflict("webhook event belongs to another epoch or target")
        raw = result["payload_json"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != result["payload_hash"]:
            raise JournalCorrupt("webhook payload checksum mismatch")
        result["payload"] = json.loads(raw)
        return result

    def _delivery(self, event_id: str) -> dict | None:
        previous = ""
        latest = None
        for row in self.j._exec("SELECT * FROM webhook_delivery WHERE event_id=? ORDER BY id",
                                (event_id,)).fetchall():
            latest = _check(row, ("id",))
            if latest["previous_hash"] != previous:
                raise JournalCorrupt("webhook delivery audit chain mismatch")
            previous = latest["record_hash"]
        return latest

    def _append(self, event_id, state, now_ms, *, attempts, total_attempts,
                next_attempt_ms=0, http_status=None, error=None):
        prior = self._delivery(event_id)
        row = dict(event_id=event_id, state=state, attempts=attempts,
                   total_attempts=total_attempts, updated_ms=int(now_ms),
                   next_attempt_ms=int(next_attempt_ms), http_status=http_status,
                   error=error, previous_hash=prior["record_hash"] if prior else "")
        columns = ",".join((*row, "record_hash"))
        self.j._exec(f"INSERT INTO webhook_delivery ({columns}) VALUES ({','.join('?' for _ in range(len(row)+1))})",
                     (*row.values(), _digest(row)))

    def enqueue(self, event_id: str, payload: dict, created_ms: int) -> dict:
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", event_id):
            raise ValueError("invalid webhook event id")
        raw = canonical_payload(payload)
        if "event_id" in payload and payload["event_id"] != event_id:
            raise ValueError("payload event_id differs from webhook event identity")
        with transaction(self.j):
            prior = self._event(event_id)
            if prior is not None:
                if prior["payload_json"].encode("utf-8") != raw:
                    raise JournalConflict("webhook event id reused with different payload")
                return self.inspect(event_id)[0]
            row = dict(event_id=event_id, epoch_hash=self.epoch_hash,
                       target_hash=self.target_hash, payload_json=raw.decode("utf-8"),
                       payload_hash=hashlib.sha256(raw).hexdigest(), created_ms=int(created_ms))
            columns = ",".join((*row, "record_hash"))
            self.j._exec(f"INSERT INTO webhook_events ({columns}) VALUES ({','.join('?' for _ in range(len(row)+1))})",
                         (*row.values(), _digest(row)))
            self._append(event_id, "PENDING", created_ms, attempts=0, total_attempts=0)
            return self.inspect(event_id)[0]

    def inspect(self, event_id: str | None = None) -> list[dict]:
        ids = [event_id] if event_id is not None else [r[0] for r in self.j._exec(
            "SELECT event_id FROM webhook_events WHERE epoch_hash=? ORDER BY sequence",
            (self.epoch_hash,)).fetchall()]
        result = []
        for key in ids:
            event = self._event(key)
            if event is None:
                continue
            delivery = self._delivery(key)
            if delivery is None:
                raise JournalCorrupt("webhook event has no delivery audit")
            result.append({**event, **{k: v for k, v in delivery.items()
                                      if k not in {"id", "record_hash", "previous_hash"}}})
        return result

    def pending(self, now_ms: int, *, limit: int | None = None) -> list[dict]:
        # Filter settled delivery states in SQL so a long-lived runtime does
        # not deserialize its entire immutable history before every POST.
        ids = self.j._exec("""SELECT e.event_id, e.sequence FROM webhook_events e
            JOIN webhook_delivery d ON d.id = (
                SELECT MAX(id) FROM webhook_delivery WHERE event_id=e.event_id)
            WHERE e.epoch_hash=? AND d.state NOT IN ('DELIVERED','SKIPPED')
            ORDER BY e.sequence""", (self.epoch_hash,)).fetchall()
        if ids:
            head = self.j._exec("""SELECT e.event_id, e.epoch_hash, e.sequence, d.state
                FROM webhook_events e LEFT JOIN webhook_delivery d ON d.id = (
                    SELECT MAX(id) FROM webhook_delivery WHERE event_id=e.event_id)
                WHERE e.target_hash=? AND (d.state IS NULL OR d.state NOT IN ('DELIVERED','SKIPPED'))
                ORDER BY e.sequence LIMIT 1""", (self.target_hash,)).fetchone()
            if head is not None and head["state"] is None:
                raise JournalCorrupt("webhook event has no delivery audit")
            if head is not None and head["epoch_hash"] != self.epoch_hash:
                raise JournalConflict("undelivered webhook from another epoch precedes current events")
        ready = []
        for item in ids:
            row = self.inspect(item[0])[0]
            if row["state"] == "FAILED" or row["next_attempt_ms"] > now_ms:
                break
            ready.append(row)
            if limit is not None and len(ready) >= limit:
                break
        return ready

    def begin_attempt(self, event_id: str, now_ms: int) -> dict:
        with transaction(self.j):
            ready = self.pending(now_ms, limit=1)
            if not ready or ready[0]["event_id"] != event_id:
                raise JournalConflict("webhook delivery must follow event sequence")
            row = ready[0]
            self._append(event_id, "INFLIGHT", now_ms, attempts=row["attempts"] + 1,
                         total_attempts=row["total_attempts"] + 1)
            return self.inspect(event_id)[0]

    def mark_delivery(self, event_id: str, *, status: str, now_ms: int,
                      http_status: int | None = None, error: str | None = None,
                      next_attempt_ms: int | None = None) -> None:
        if status not in {"DELIVERED", "RETRY", "FAILED"}:
            raise ValueError("invalid delivery outcome")
        if status == "DELIVERED" and (http_status is None or not 200 <= http_status < 300):
            raise ValueError("delivery acknowledgment requires HTTP 2xx")
        with transaction(self.j):
            rows = self.inspect(event_id)
            if not rows or rows[0]["state"] not in {"INFLIGHT", "RETRY", "PENDING"}:
                raise JournalConflict("webhook event cannot accept a delivery outcome")
            row = rows[0]
            self._append(event_id, status, now_ms, attempts=row["attempts"],
                         total_attempts=row["total_attempts"],
                         next_attempt_ms=next_attempt_ms or 0,
                         http_status=http_status, error=error)

    def retry(self, event_id: str, now_ms: int = 0) -> None:
        """Explicit operator retry grants a fresh bounded attempt budget."""
        with transaction(self.j):
            rows = self.inspect(event_id)
            if not rows or rows[0]["state"] in {"DELIVERED", "SKIPPED", "INFLIGHT"}:
                raise JournalConflict("webhook event is not eligible for operator retry")
            self._append(event_id, "PENDING", now_ms, attempts=0,
                         total_attempts=rows[0]["total_attempts"])

    def skip(self, event_id: str, reason: str, now_ms: int = 0) -> None:
        if not reason or len(reason) > 500:
            raise ValueError("skip requires an audit reason of at most 500 characters")
        with transaction(self.j):
            rows = self.inspect(event_id)
            if not rows or rows[0]["state"] in {"DELIVERED", "SKIPPED", "INFLIGHT"}:
                raise JournalConflict("webhook event is not eligible for operator skip")
            row = rows[0]
            self._append(event_id, "SKIPPED", now_ms, attempts=row["attempts"],
                         total_attempts=row["total_attempts"], error=reason)
