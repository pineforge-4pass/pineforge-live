"""Serial, at-least-once delivery of immutable order-action JSON.

The receiver must deduplicate Idempotency-Key/event_id. A timeout can follow
successful processing, so HTTP delivery cannot promise exactly-once effects.
"""
from __future__ import annotations

import asyncio
import email.utils
import hashlib
import hmac
import inspect
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import weakref
from dataclasses import dataclass, field
from datetime import timezone
from typing import Mapping, Protocol

from .store import Outbox


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    async def post(self, url: str, body: bytes, headers: dict[str, str],
                   timeout_ms: int) -> HttpResponse: ...


def validate_target_url(url: str, allow_insecure_http: bool = False) -> str:
    if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("invalid webhook URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid webhook URL") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or port == 0):
        raise ValueError("webhook URL requires HTTP(S), a host, and no credentials or fragment")
    local = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not (local or allow_insecure_http):
        raise ValueError("remote HTTP requires allow_insecure_http=true")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """HTTPS certificate verification stays enabled; redirects are never followed."""
    async def post(self, url: str, body: bytes, headers: dict[str, str],
                   timeout_ms: int) -> HttpResponse:
        def request():
            opener = urllib.request.build_opener(
                _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with opener.open(req, timeout=timeout_ms / 1000) as response:
                    # Acknowledgment uses status alone. Do not retain response
                    # bodies: they may echo a URL token, signature, or payload.
                    return HttpResponse(response.status, dict(response.headers))
            except urllib.error.HTTPError as error:
                try:
                    return HttpResponse(error.code, dict(error.headers or {}))
                finally:
                    error.close()
        return await asyncio.to_thread(request)


@dataclass(frozen=True)
class DeliveryReport:
    attempted: int
    delivered: int
    pending: int
    failed: int
    last_error: str | None = None


# Serialize multiple dispatcher objects sharing a journal in one event loop.
# Across processes the runtime supplies its fenced lease through lease_check.
_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


class Dispatcher:
    def __init__(self, outbox: Outbox, config, clock, transport: Transport | None = None,
                 lease_check=None):
        self.outbox, self.config, self.clock = outbox, config, clock
        self.transport = transport if transport is not None else UrllibTransport()
        self.lease_check = lease_check
        validate_target_url(config.target_url, config.allow_insecure_http)
        if config.target_url != outbox.target_url:
            raise ValueError("delivery target differs from journal target")
        for name in ("timeout_ms", "max_attempts", "backoff_initial_ms", "backoff_max_ms",
                     "retry_after_max_ms"):
            value = getattr(config, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if config.backoff_initial_ms > config.backoff_max_ms:
            raise ValueError("initial backoff exceeds maximum")
        secret_env = config.secret_env
        self._secret = None
        if secret_env is not None:
            if not isinstance(secret_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_env):
                raise ValueError("secret_env must be an environment variable name")
            secret = os.environ.get(secret_env)
            if not secret:
                raise ValueError("configured webhook signing secret is missing")
            self._secret = secret.encode("utf-8")

    def _lock(self):
        key = (str(self.outbox.j.path.resolve()), self.outbox.target_hash,
               id(asyncio.get_running_loop()))
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock

    def _headers(self, event_id: str, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "X-PineForge-Event-Id": event_id,
                   "Idempotency-Key": event_id, "User-Agent": "pineforge-live-webhook/1"}
        if self._secret is not None:
            digest = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            headers["X-PineForge-Signature"] = f"sha256={digest}"
        return headers

    def _retry_delay(self, attempts: int, response: HttpResponse | None, now_ms: int) -> int:
        # Bound exponent calculation as well as the result for corrupted or
        # unusually large attempt budgets supplied by an embedding application.
        delay = min(self.config.backoff_max_ms,
                    self.config.backoff_initial_ms * 2 ** min(max(attempts - 1, 0), 30))
        if response is None or response.status != 429:
            return delay
        raw = next((str(v) for k, v in response.headers.items() if k.lower() == "retry-after"), None)
        if raw is None:
            return delay
        try:
            if re.fullmatch(r"[0-9]{1,12}", raw.strip()):
                retry_ms = int(raw.strip()) * 1000
            else:
                date = email.utils.parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                retry_ms = max(0, int(date.timestamp() * 1000) - now_ms)
            return max(delay, min(retry_ms, self.config.retry_after_max_ms))
        except (ValueError, TypeError, OverflowError):
            return delay

    def _report(self, attempted: int, delivered: int, last_error: str | None) -> DeliveryReport:
        rows = self.outbox.inspect()
        failed = sum(row["state"] == "FAILED" for row in rows)
        pending = sum(row["state"] not in {"DELIVERED", "SKIPPED", "FAILED"} for row in rows)
        if last_error is None:
            last_error = next((row["error"] for row in rows
                               if row["state"] in {"FAILED", "RETRY"} and row["error"]), None)
        return DeliveryReport(attempted, delivered, pending, failed, last_error)

    async def _check_lease(self) -> None:
        if self.lease_check is not None:
            result = self.lease_check(self.config.timeout_ms)
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise RuntimeError("webhook delivery lease check refused")
        if self.outbox.j.con.in_transaction:
            raise RuntimeError("webhook lease callback left a journal transaction open")

    async def drain(self) -> DeliveryReport:
        """Deliver due events until empty, backoff, or a failure fences the head.

        Do not sleep inside drain. The scheduler invokes it again when the
        journal's next_attempt_ms is due. A failed head requires retry/skip.
        Lease checks cover the full HTTP timeout both before reservation and
        immediately before transport, after the durable attempt and request
        preparation. A second-check failure retains INFLIGHT without sending.
        A received ACK may be recorded after expiry as a historical delivery
        fact; the next POST still requires a fresh lease-horizon check.
        """
        if self.outbox.j.con.in_transaction:
            raise RuntimeError("webhook network delivery cannot run inside a journal transaction")
        attempted = delivered = 0
        last_error = None
        async with self._lock():
            while True:
                now_ms = self.clock.now_ms()
                ready = self.outbox.pending(now_ms, limit=1)
                if not ready:
                    break
                row = ready[0]
                event_id = row["event_id"]
                if row["attempts"] >= self.config.max_attempts:
                    last_error = "delivery attempt budget exhausted; operator retry or skip required"
                    self.outbox.mark_delivery(event_id, status="FAILED", now_ms=now_ms,
                                              error=last_error)
                    break
                await self._check_lease()
                row = self.outbox.begin_attempt(event_id, self.clock.now_ms())
                attempted += 1
                body = row["payload_json"].encode("utf-8")
                headers = self._headers(event_id, body)
                # BEGIN IMMEDIATE, fsync, and request preparation can consume
                # the previous lease horizon. Check again after they finish.
                # Keep this outside the transport error handler: refusal is
                # authority loss, not a retryable HTTP failure.
                await self._check_lease()
                response = None
                try:
                    response = await asyncio.wait_for(
                        self.transport.post(self.config.target_url, body, headers, self.config.timeout_ms),
                        timeout=self.config.timeout_ms / 1000)
                    if (isinstance(response.status, bool) or not isinstance(response.status, int)
                            or not 100 <= response.status < 600):
                        raise ValueError("invalid HTTP status")
                except asyncio.CancelledError:
                    # Durable INFLIGHT means outcome unknown. Retry the SAME
                    # event id after restart; never claim it was undelivered.
                    raise
                except Exception:
                    # Exception messages may contain the entire signed request.
                    # Persist only a fixed diagnostic, never repr(exception).
                    last_error = "transport failure or timeout; delivery outcome unknown"
                    response = None
                now_ms = self.clock.now_ms()
                if response is not None and 200 <= response.status < 300:
                    self.outbox.mark_delivery(event_id, status="DELIVERED", now_ms=now_ms,
                                              http_status=response.status)
                    delivered += 1
                    continue
                retryable = response is None or response.status == 429 or 500 <= response.status < 600
                if response is not None:
                    last_error = f"HTTP {response.status}"
                exhausted = row["attempts"] >= self.config.max_attempts
                status = "RETRY" if retryable and not exhausted else "FAILED"
                delay = self._retry_delay(row["attempts"], response, now_ms) if status == "RETRY" else 0
                self.outbox.mark_delivery(event_id, status=status, now_ms=now_ms,
                                          http_status=response.status if response else None,
                                          error=last_error, next_attempt_ms=now_ms + delay)
                break
        return self._report(attempted, delivered, last_error)
