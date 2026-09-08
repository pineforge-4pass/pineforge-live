# pineforge_live/journal/fence.py
"""Leased lock with a fencing token (spec §2, check mode). One host, one journal (v1)."""
from __future__ import annotations
import contextlib, fcntl, json, os
from pathlib import Path
from .journal import JournalFault

class LeaseHeld(RuntimeError): pass
class LeaseLost(LeaseHeld):
    """A held lease was invalidated -- renew() found the lock file's token
    no longer matches ours, or found we had already expired (finding 9):
    someone else may already believe they hold the lease. self.token is
    cleared so a further renew()/any use of the stale lease fails loudly
    rather than silently."""

class FencedLease:
    def __init__(self, lock_path: str | Path, journal):
        self.lock_path = Path(lock_path); self.journal = journal; self.token: int | None = None; self.expiry_ms: int | None = None

    @contextlib.contextmanager
    def _flock(self):
        # finding 9: acquire()/renew() are check-then-write; an OS advisory
        # lock on a sibling `.flock` file (never the lock file itself, so a
        # reader that just os.replace()s the lock file is unaffected) makes
        # each check-then-write atomic across processes.
        flock_path = Path(str(self.lock_path) + ".flock")
        fd = os.open(flock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict | None:
        # Safe when the lock file is absent, empty, holds unreadable JSON,
        # or holds valid JSON that isn't an object (e.g. a list) -- treated
        # the same as "no live lease", never a hard failure here
        # (finding 10).
        try:
            raw = self.lock_path.read_text()
        except FileNotFoundError:
            return None
        if not raw:
            return None
        try:
            cur = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return cur if isinstance(cur, dict) else None

    def acquire(self, lease_ms: int, now_ms: int) -> int:
        with self._flock():
            cur = self._read()
            if cur and cur.get("expiry_ms", 0) > now_ms:
                raise LeaseHeld(f"lease token {cur.get('token')} live until {cur['expiry_ms']}")
            # N4: the lock file alone is not the source of truth -- it can be
            # deleted (operator, a /tmp cleaner) while its lease is still
            # live. The journal's own `checks` table survives that, so a
            # live row there (lease_expiry_ms > now_ms) refuses acquire()
            # exactly as a live lock file would, closing finding 9's
            # residual gap.
            live = self.journal.live_check(now_ms)
            if live is not None:
                raise LeaseHeld(
                    f"fencing token {live['fencing_token']} live in checks until {live['lease_expiry_ms']} "
                    "(lock file missing/stale)"
                )
            # Token continuity: even when the lock file is absent/empty/stale (a
            # previous journal's leftover), never hand out a token the journal has
            # already recorded — take the max of the journal's own high-water mark
            # and whatever the lock file last claimed.
            token = max(self.journal.max_fencing_token(), (cur or {}).get("token", 0)) + 1
            expiry_ms = now_ms + lease_ms
            try:
                self.journal.append_check(token, expiry_ms)
            except JournalFault as e:
                # finding 9: a `checks` PRIMARY KEY collision on `fencing_token`
                # (another holder recorded this exact token first) is a live
                # lease, not a journal defect.
                if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                    raise LeaseHeld(f"fencing token {token} already recorded: {e}") from e
                raise
            self.token, self.expiry_ms = token, expiry_ms
            tmp = self.lock_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"token": self.token, "expiry_ms": self.expiry_ms})); os.replace(tmp, self.lock_path)
            return self.token

    def renew(self, now_ms: int, lease_ms: int) -> None:
        if self.token is None:
            raise LeaseHeld("renew before acquire")  # finding 13: was `assert`, stripped under -O
        with self._flock():
            cur = self._read()
            held_token = self.token
            if cur is None or cur.get("token") != held_token or self.expired(now_ms):
                # finding 9: someone else's token is on the lock file, or we
                # had already lapsed by our own clock -- renewing now would
                # let a lapsed holder regress the lock file back to a stale
                # token even after another holder has taken over.
                self.token = None
                raise LeaseLost(f"lease token {held_token} lost (lock file now {cur})")
            self.expiry_ms = now_ms + lease_ms
            tmp = self.lock_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"token": self.token, "expiry_ms": self.expiry_ms})); os.replace(tmp, self.lock_path)

    def expired(self, now_ms: int) -> bool:
        return self.expiry_ms is None or now_ms > self.expiry_ms
