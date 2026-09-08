# pineforge_live/journal/sidecar.py
"""Out-of-band STOP marker (spec §5.5): written with O_SYNC BEFORE any in-memory STOP."""
from __future__ import annotations
import json, os, time
from pathlib import Path
from .journal import JournalFault
SIZE = 4096

class StopMarker:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def prepare(self) -> None:
        """Preallocate the 4096-byte zeroed marker file at startup, before
        any STOP can occur (finding 7): the disk-full fault that §6 routes
        to STOP(HARD, HOLD) via this marker must never need to grow the
        file during the emergency itself -- write() only ever overwrites
        already-allocated bytes in place."""
        buf = b"\0" * SIZE
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_SYNC", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
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
        payload = json.dumps({"level": level, "disposition": disposition, "cause": cause, "ts_ms": int(time.time() * 1000)}).encode()
        if len(payload) + 1 > SIZE:
            raise JournalFault(f"STOP marker payload for {self.path} exceeds the preallocated {SIZE} bytes")
        buf = payload + b"\n" + b"\0" * (SIZE - len(payload) - 1)
        # No O_CREAT/O_TRUNC: the file must already be prepare()'d, and this
        # write only ever overwrites in place (finding 7).
        flags = os.O_WRONLY | getattr(os, "O_SYNC", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            n = os.pwrite(fd, buf, 0)
            if n != len(buf):
                raise JournalFault(f"short write for STOP marker {self.path}: {n}/{len(buf)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_dir()

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
