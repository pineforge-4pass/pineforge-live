# pineforge_live/journal/sidecar.py
"""Out-of-band STOP marker (spec §5.5): written with O_SYNC BEFORE any in-memory STOP."""
from __future__ import annotations
import json, os, time
from pathlib import Path
SIZE = 4096

class StopMarker:
    def __init__(self, path: str | Path):
        self.path = Path(path)
    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0
    def write(self, level: str, disposition: str, cause: str) -> None:
        payload = json.dumps({"level": level, "disposition": disposition, "cause": cause, "ts_ms": int(time.time() * 1000)}).encode()
        buf = payload + b"\n" + b"\0" * (SIZE - len(payload) - 1)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_SYNC", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            os.write(fd, buf); os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    def read(self) -> dict | None:
        if not self.exists():
            return None
        return json.loads(self.path.read_bytes().split(b"\n", 1)[0])
    def clear(self) -> dict | None:
        content = self.read()
        if self.path.exists():
            self.path.unlink()
        return content
