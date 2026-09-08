# pineforge_live/journal/sidecar.py
"""Out-of-band STOP marker (spec §5.5): written with O_SYNC BEFORE any in-memory STOP."""
from __future__ import annotations
import json, os, time
from pathlib import Path
from .journal import JournalFault, StopMarkerPresent
SIZE = 4096

class StopMarker:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def prepare(self) -> None:
        """Preallocate the 4096-byte zeroed marker file at startup, before
        any STOP can occur (finding 7): the disk-full fault that §6 routes
        to STOP(HARD, HOLD) via this marker must never need to grow the
        file during the emergency itself -- write() only ever overwrites
        already-allocated bytes in place.

        N1: this must never DISARM an existing marker. A restart's natural
        startup order is "arm the sidecar, then open the journal" -- which
        means prepare() runs before anyone has looked at whether a previous
        run left the marker SET (an unacknowledged STOP) or UNREADABLE (a
        torn write, itself evidence). Both must survive prepare() untouched
        so Journal.open(stop_marker=...) still refuses. O_EXCL makes the
        "was it already there" check and the create atomic across
        processes: no unguarded exists()-then-open race.

        - Absent: create with O_CREAT|O_EXCL|O_SYNC, zero-fill 4096 bytes,
          fsync file + dir.
        - Exists and armed (payload parses to None -- all-zero or empty):
          no-op, file untouched. Idempotent: calling prepare() twice on an
          already-armed marker does nothing the second time.
        - Exists and SET (parses with `level`) or UNREADABLE (non-zero,
          non-parsable payload): raise StopMarkerPresent and touch nothing.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_SYNC", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            payload = self._payload()
            if payload is not None:
                # SET (parses with `level`) or UNREADABLE (torn/garbage):
                # never disarm -- leave the file exactly as it is.
                raise StopMarkerPresent(
                    f"STOP marker already present at {self.path}: {payload}; operator must clear it"
                ) from None
            # Armed-only (all-zero, or an empty/short file): already the
            # no-op state prepare() is meant to produce -- nothing to do.
            return
        try:
            buf = b"\0" * SIZE
            n = os.pwrite(fd, buf, 0)
            if n != len(buf):
                raise JournalFault(f"short preallocate write for STOP marker {self.path}: {n}/{len(buf)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        dfd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def write(self, level: str, disposition: str, cause: str) -> None:
        """N2: this must still land a durable marker even when prepare()
        was skipped (no runtime caller wired it up yet) or itself failed
        (e.g. ENOSPC at startup) -- the emergency STOP path cannot depend
        on an earlier call having succeeded. O_CREAT (still no O_TRUNC)
        creates the file best-effort at STOP time if it isn't already
        there; the preallocation benefit (never growing the file during
        the emergency) is lost only in that case. Any OSError from
        open/pwrite/fsync is reraised as JournalFault so callers routing
        on fault type see one exception, not FileNotFoundError."""
        payload = json.dumps({"level": level, "disposition": disposition, "cause": cause, "ts_ms": int(time.time() * 1000)}).encode()
        if len(payload) + 1 > SIZE:
            raise JournalFault(f"STOP marker payload for {self.path} exceeds the preallocated {SIZE} bytes")
        buf = payload + b"\n" + b"\0" * (SIZE - len(payload) - 1)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_SYNC", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
            try:
                n = os.pwrite(fd, buf, 0)
                if n != len(buf):
                    raise JournalFault(f"short write for STOP marker {self.path}: {n}/{len(buf)} bytes")
                os.fsync(fd)
            finally:
                os.close(fd)
            self._fsync_dir()
        except OSError as e:
            # JournalFault (short write) is a RuntimeError, not an OSError,
            # so it already propagates untouched; this catches genuine
            # OS-level failures (open/pwrite/fsync) and normalizes them.
            raise JournalFault(f"STOP marker write failed for {self.path}: {e}") from e

    def _payload(self) -> dict | None:
        """Best-effort parse of the on-disk payload: None for an absent or
        all-zero (armed-but-not-set) file, {"unreadable": True, "raw": ...}
        for a torn/garbage payload (finding 8), or the parsed dict."""
        if not self.path.exists():
            return None
        raw = self.path.read_bytes()
        head = raw.split(b"\n", 1)[0]
        if not head or head.strip(b"\0") == b"":
            return None
        try:
            obj = json.loads(head)
        except json.JSONDecodeError:
            return {"unreadable": True, "raw": raw[:200]}
        if not isinstance(obj, dict):
            return {"unreadable": True, "raw": raw[:200]}
        return obj

    def exists(self) -> bool:
        """True iff the marker is *set*: file present and payload parses to
        a dict carrying `level` (finding 7) -- a prepare()'d but never
        write()'d (all-zero) file is armed, not set."""
        payload = self._payload()
        return isinstance(payload, dict) and not payload.get("unreadable") and "level" in payload

    def read(self) -> dict | None:
        return self._payload()

    def clear(self) -> dict | None:
        content = self.read()
        if self.path.exists():
            self.path.unlink()
        return content
