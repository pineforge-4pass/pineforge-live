from __future__ import annotations
import hashlib, json, os, sqlite3, time
from pathlib import Path
from typing import Any, Callable
from pineforge_live import types as T
from .schema import DDL, CHECKSUMMED

class JournalFault(RuntimeError): pass
class JournalCorrupt(RuntimeError): pass

def checksum(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

def _now() -> int:
    return int(time.time() * 1000)

class Journal:
    def __init__(self, con: sqlite3.Connection, path: Path):
        self.con, self.path = con, path
        self.con.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str | Path, *, create: bool = True, stop_marker=None) -> "Journal":
        path = Path(path)
        # Refusal on an existing STOP marker happens BEFORE the sqlite connection
        # is opened — an operator-cleared marker is the only way back in.
        if stop_marker is not None and stop_marker.exists():
            raise JournalCorrupt(f"STOP marker present at {stop_marker.path}: {stop_marker.read()}; operator must clear it")
        if not create and not path.exists():
            raise JournalFault(f"journal {path} does not exist")
        try:
            con = sqlite3.connect(path, isolation_level=None)
            con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA synchronous=FULL")
            con.executescript(DDL)
        except sqlite3.Error as e:
            raise JournalFault(str(e)) from e
        journal = cls(con, path)
        if not create:
            # Reopening an existing journal (the runtime's restart path) refuses
            # a torn tail immediately, rather than waiting for the caller to
            # remember to call verify_tail() explicitly.
            journal.verify_tail()
        return journal

    def close(self):
        self.con.close()

    @staticmethod
    def emergency_log(path: str | Path) -> Callable[[dict], None]:
        """Spec §5.2: an EMERGENCY action attempts the sqlite write-ahead and,
        on failure, still submits AND appends to an out-of-band log on a
        different filesystem (or stderr) so the attempt is never silently
        lost. The returned callable is independent of any Journal instance
        (no `self`, never touches sqlite), so it is safe to call after the
        Journal it stands in for has been closed. Each call appends exactly
        one line -- canonical JSON of `row` + "\\n" -- via a single
        O_SYNC'd write + fsync so the line lands durably or not at all."""
        path = Path(path)
        def _append(row: dict[str, Any]) -> None:
            line = (json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_SYNC, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        return _append

    # --- helpers --------------------------------------------------------------
    def _exec(self, sql: str, params=()):
        try:
            return self.con.execute(sql, params)
        except sqlite3.Error as e:
            raise JournalFault(f"{sql[:40]}...: {e}") from e

    def _insert(self, table: str, row: dict[str, Any], *, or_ignore: bool = False):
        row = dict(row); row.setdefault("created_ms", _now())
        if table in CHECKSUMMED:
            row["checksum"] = checksum(row)
        cols = ",".join(row); qs = ",".join("?" for _ in row)
        self._exec(f"INSERT {'OR IGNORE' if or_ignore else ''} INTO {table}({cols}) VALUES({qs})", tuple(row.values()))

    def rows(self, table: str, where: str, params) -> list[dict[str, Any]]:
        return [dict(r) for r in self._exec(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", tuple(params))]

    # --- writers ----------------------------------------------------------------
    def append_epoch(self, epoch_hash: str, spec_json: str):
        self._insert("epochs", {"epoch_hash": epoch_hash, "spec_json": spec_json}, or_ignore=True)
    def append_runtime_config(self, h: str, config_json: str):
        self._insert("runtime_configs", {"runtime_config_hash": h, "config_json": config_json}, or_ignore=True)
    def append_bar(self, epoch_hash: str, bar: T.NormalizedBar, bars_hash: int):
        self._insert("bars", {"epoch_hash": epoch_hash, "ts_open": bar.ts_open, "o": bar.o, "h": bar.h, "l": bar.l, "c": bar.c,
                              "v": bar.v, "trade_count": bar.trade_count, "synthesized": int(bar.synthesized), "bars_hash": bars_hash}, or_ignore=True)
    def append_settlement(self, row: dict[str, Any]):
        self._insert("settlements", row, or_ignore=True)
    def append_evaluation(self, row: dict[str, Any]):
        self._insert("evaluations", row)
    def append_intent(self, epoch_hash: str, intent_key: str, state: str, payload_json: str):
        self._exec("INSERT OR REPLACE INTO intents VALUES(?,?,?,?,?)", (epoch_hash, intent_key, state, payload_json, _now()))
    def append_action(self, row: dict[str, Any]):
        self._insert("actions", row)
    def update_order_state(self, client_id: str, state_json: str, *, terminal: bool = False):
        self._exec("INSERT INTO order_states VALUES(?,?,?)", (client_id, state_json, _now()))
        if terminal:
            self._exec("UPDATE actions SET terminal=1 WHERE client_id=?", (client_id,))
    def append_order_id(self, client_id: str, venue_order_id: str):
        self._exec("INSERT OR IGNORE INTO order_ids VALUES(?,?)", (client_id, venue_order_id))
    def append_fill(self, row: dict[str, Any]):
        self._insert("fills", row, or_ignore=True)
    def append_reconcile(self, row: dict[str, Any]):
        self._insert("reconciles", row)
    def append_incident(self, kind: str, detail: dict[str, Any]):
        self._insert("incidents", {"kind": kind, "detail_json": json.dumps(detail, sort_keys=True)})
    def append_stop(self, level: str, disposition: str, cause: str):
        self._insert("stops", {"level": level, "disposition": disposition, "cause": cause})
    def append_check(self, fencing_token: int, lease_expiry_ms: int, row: dict[str, Any] | None = None):
        self._insert("checks", {"fencing_token": fencing_token, "lease_expiry_ms": lease_expiry_ms, "row_json": json.dumps(row or {}, sort_keys=True)})

    # --- readers ----------------------------------------------------------------
    def last_settlement(self, epoch_hash: str) -> dict[str, Any] | None:
        r = self._exec("SELECT * FROM settlements WHERE epoch_hash=? ORDER BY bar_index DESC LIMIT 1", (epoch_hash,)).fetchone()
        return dict(r) if r else None
    def settlement(self, epoch_hash: str, bar_index: int) -> dict[str, Any] | None:
        r = self._exec("SELECT * FROM settlements WHERE epoch_hash=? AND bar_index=?", (epoch_hash, bar_index)).fetchone()
        return dict(r) if r else None
    def actions_non_terminal(self) -> list[dict[str, Any]]:
        return self.rows("actions", "terminal=0", ())
    def max_fencing_token(self) -> int:
        r = self._exec("SELECT COALESCE(MAX(fencing_token),0) AS m FROM checks").fetchone(); return int(r["m"])

    def verify_tail(self):
        """Spec §6: the last row of every checksummed table must verify, else refuse.

        Explicit (called by the runtime's restart path, Plan B2/B3) — also
        invoked by open() when create=False, so a reopened journal refuses a
        torn tail immediately rather than trusting a partially-written last row.
        """
        for table in CHECKSUMMED:
            r = self._exec(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            if r is None:
                continue
            row = dict(r); stored = row.pop("checksum")
            if checksum(row) != stored:
                raise JournalCorrupt(f"{table}: last row checksum mismatch (torn or edited write)")
