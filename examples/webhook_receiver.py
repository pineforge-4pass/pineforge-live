#!/usr/bin/env python3
"""Local webhook receiver: durably record order intents, never place trades.

Run: python examples/webhook_receiver.py --database ./webhook-receipts.sqlite3
Set PINEFORGE_WEBHOOK_SECRET to require signatures. This example acknowledges
receipt only; a broker integration needs its own durable, idempotent worker.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

MAX_BODY_BYTES = 1_048_576
SIGNATURE_HEADER = "X-PineForge-Signature"


class PayloadConflict(Exception):
    """An existing event ID was reused for different bytes."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_event(body: bytes) -> dict:
    def bad_constant(value):
        raise ValueError("nonfinite JSON number")
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result
    event = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                       parse_constant=bad_constant, parse_float=finite_float)
    if not isinstance(event, dict):
        raise ValueError("JSON object required")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
        raise ValueError("event_id must be a nonempty string of at most 256 characters")
    if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    if event.get("event") not in ("order_action", "order_update"):
        raise ValueError("unsupported event type")
    if event["event"] == "order_update" and (not isinstance(event.get("original_event_id"), str)
                                              or not event["original_event_id"]):
        raise ValueError("order_update requires original_event_id")
    return event


class ReceiptStore:
    """One transaction records the event ID and full bytes before HTTP ACK."""
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("receiver needs an on-disk database")
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS webhook_receipts ("
                       "event_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL, "
                       "payload_json TEXT NOT NULL, received_ms INTEGER NOT NULL)")
            db.commit()
        finally:
            db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def record(self, event: dict, body: bytes) -> bool:
        checksum = hashlib.sha256(body).hexdigest()
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload_sha256,payload_json FROM webhook_receipts WHERE event_id=?",
                               (event["event_id"],)).fetchone()
            if prior is not None:
                if prior != (checksum, body.decode("utf-8")):
                    raise PayloadConflict("event_id reused for different payload")
                db.commit()
                return False
            db.execute("INSERT INTO webhook_receipts VALUES(?,?,?,?)",
                       (event["event_id"], checksum, body.decode("utf-8"), time.time_ns() // 1_000_000))
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


class Receiver(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address: tuple[str, int], database: str | Path, *,
                 secret: bytes | None = None, on_record: Callable[[dict], None] | None = None):
        if secret == b"":
            raise ValueError("configured signature secret must not be empty")
        self.store = ReceiptStore(database)
        self.secret, self.on_record = secret, on_record
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Receiver

    def log_message(self, format, *args):
        # Request headers, signed bodies and URL secrets are never logged.
        pass

    def _reply(self, status: int, payload: dict):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.connection.settimeout(10)
        if self.path != "/webhook":
            self._reply(404, {"error": "not_found"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
            self._reply(411, {"error": "single_content_length_required"})
            return
        try:
            size = int(lengths[0])
        except ValueError:
            self._reply(400, {"error": "invalid_content_length"})
            return
        if not 0 < size <= MAX_BODY_BYTES:
            self._reply(413, {"error": "body_size_limit"})
            return
        try:
            body = self.rfile.read(size)
        except (OSError, TimeoutError):
            self._reply(408, {"error": "incomplete_request_body"})
            return
        if len(body) != size:
            self._reply(400, {"error": "incomplete_request_body"})
            return
        if self.server.secret is not None:
            signatures = self.headers.get_all(SIGNATURE_HEADER, [])
            expected = "sha256=" + hmac.new(self.server.secret, body, hashlib.sha256).hexdigest()
            supplied = signatures[0] if len(signatures) == 1 else ""
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii")):
                self._reply(401, {"error": "invalid_signature"})
                return
        try:
            event = parse_event(body)
        except (ValueError, UnicodeError, RecursionError):
            self._reply(400, {"error": "invalid_event"})
            return
        for name in ("X-PineForge-Event-Id", "Idempotency-Key"):
            values = self.headers.get_all(name, [])
            if values and (len(values) != 1 or values[0] != event["event_id"]):
                self._reply(400, {"error": "event_id_header_mismatch"})
                return
        try:
            created = self.server.store.record(event, body)
        except PayloadConflict:
            self._reply(409, {"error": "event_id_payload_conflict"})
            return
        except (sqlite3.Error, OSError):
            self._reply(503, {"error": "receipt_not_committed"})
            return
        if created and self.server.on_record is not None:
            # The database is the durable receipt; console output is advisory.
            try:
                self.server.on_record(event)
            except Exception:
                pass
        self._reply(200, {"event_id": event["event_id"], "recorded": True, "duplicate": not created})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="listen address (default: localhost)")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--database", default="webhook-receipts.sqlite3")
    parser.add_argument("--secret-env", default="PINEFORGE_WEBHOOK_SECRET",
                        help="optional environment variable containing the HMAC secret")
    args = parser.parse_args(argv)
    value = os.environ.get(args.secret_env)
    secret = value.encode("utf-8") if value is not None else None
    try:
        server = Receiver((args.bind, args.port), args.database, secret=secret,
                          on_record=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Cannot start receiver: {type(exc).__name__}\n")
    print(f"Receiving intents at http://{args.bind}:{server.server_port}/webhook; "
          f"signatures {'required' if secret is not None else 'disabled'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
