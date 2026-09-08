# pineforge_live/journal/fence.py
"""Leased lock with a fencing token (spec §2, check mode). One host, one journal (v1)."""
from __future__ import annotations
import json, os
from pathlib import Path

class LeaseHeld(RuntimeError): pass

class FencedLease:
    def __init__(self, lock_path: str | Path, journal):
        self.lock_path = Path(lock_path); self.journal = journal; self.token: int | None = None; self.expiry_ms: int | None = None
    def _read(self) -> dict | None:
        # Safe when the lock file is absent, empty, or holds unreadable content
        # (e.g. a stale/corrupt file left behind by a previous journal) — treated
        # the same as "no live lease", never a hard failure here.
        try:
            return json.loads(self.lock_path.read_text() or "{}") or None
        except (FileNotFoundError, json.JSONDecodeError):
            return None
    def acquire(self, lease_ms: int, now_ms: int) -> int:
        cur = self._read()
        if cur and cur.get("expiry_ms", 0) > now_ms:
            raise LeaseHeld(f"lease token {cur['token']} live until {cur['expiry_ms']}")
        # Token continuity: even when the lock file is absent/empty/stale (a
        # previous journal's leftover), never hand out a token the journal has
        # already recorded — take the max of the journal's own high-water mark
        # and whatever the lock file last claimed.
        self.token = max(self.journal.max_fencing_token(), (cur or {}).get("token", 0)) + 1
        self.expiry_ms = now_ms + lease_ms
        self.journal.append_check(self.token, self.expiry_ms)
        tmp = self.lock_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": self.token, "expiry_ms": self.expiry_ms})); os.replace(tmp, self.lock_path)
        return self.token
    def renew(self, now_ms: int, lease_ms: int) -> None:
        assert self.token is not None
        self.expiry_ms = now_ms + lease_ms
        tmp = self.lock_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": self.token, "expiry_ms": self.expiry_ms})); os.replace(tmp, self.lock_path)
    def expired(self, now_ms: int) -> bool:
        return self.expiry_ms is None or now_ms > self.expiry_ms
