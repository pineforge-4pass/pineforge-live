from __future__ import annotations
import hashlib, json, os, sqlite3, sys, time
from pathlib import Path
from typing import Any, Callable
from pineforge_live import types as T
from .schema import DDL, CHECKSUMMED, CHECKSUM_EXCLUDE, HEX_HASH_COLUMNS

class JournalFault(RuntimeError): pass
class JournalCorrupt(RuntimeError): pass
class JournalConflict(JournalCorrupt):
    """A conflicting row for an existing natural key (finding 5): the new
    append is neither a fresh row nor a byte-for-byte idempotent
    resubmission of the existing one."""
class StopMarkerPresent(JournalCorrupt):
    """Open refused because the sidecar STOP marker is present (or torn) --
    an operator state, not a corrupt journal (finding 14), but still fatal
    to opening: callers that want to distinguish the two catch this
    subclass specifically rather than the base JournalCorrupt."""

def checksum(row: dict[str, Any]) -> str:
    # Same canonicalization as pineforge_live.types.canonical_sha256
    # (finding 17/18): an Enum value never legitimately reaches a stored
    # row (callers pass `.value` strings before journaling), so using the
    # strict `_canon` default here -- instead of `default=str`, which would
    # silently accept any object -- catches a caller mistake as a hard
    # JournalFault (via sqlite3's own "Error binding parameter") rather than
    # journaling a value verify_tail could never reproduce.
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=T._canon).encode()).hexdigest()

def _now() -> int:
    return int(time.time() * 1000)

def _hash_to_hex(h: int) -> str:
    # Finding 3: bars_hash/broker_state_hash are 64-bit FNV-1a values, half
    # of which are >= 2**63 and overflow SQLite's signed INTEGER storage
    # class. Stored as 16 lowercase hex digits instead.
    return f"{h:016x}"

def _domain(table: str, row: dict[str, Any], *, for_identity: bool = False) -> dict[str, Any]:
    """The set of columns a table's checksum is computed over (finding 2/4):
    every stored column except `checksum` itself and the table's declared
    exclusions (e.g. `evaluations.id`, an AUTOINCREMENT surrogate that is
    not part of the row's logical content).

    `for_identity=True` additionally drops `created_ms`: used only to judge
    whether a conflicting append (finding 5) is the same settlement/bar
    being re-journaled, where the audit timestamp legitimately differs
    between the two append calls even though the row is otherwise
    identical. verify_tail() never sets this -- created_ms is part of what
    makes a single stored row's checksum self-consistent.
    """
    excluded = {"checksum", *CHECKSUM_EXCLUDE.get(table, ())}
    if for_identity:
        excluded = excluded | {"created_ms"}
    return {k: v for k, v in row.items() if k not in excluded}

def _reject_unchecksummable(row: dict[str, Any]) -> None:
    """Finding 4: bool and NaN/inf silently round-trip through SQLite as
    0/1 or NULL, which would make verify_tail's recomputed checksum
    permanently disagree with what was actually stored. Reject them at the
    door instead."""
    for k, v in row.items():
        if isinstance(v, bool):
            raise JournalFault(f"{k}: bool is not a checksummable value (pass an explicit 0/1 int)")
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            raise JournalFault(f"{k}: NaN/inf is not a checksummable value")

def _decode_hashes(row: dict[str, Any]) -> dict[str, Any]:
    for k in HEX_HASH_COLUMNS:
        if k in row and row[k] is not None:
            row[k] = int(row[k], 16)
    return row

class Journal:
    def __init__(self, con: sqlite3.Connection, path: Path):
        self.con, self.path = con, path
        self.con.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: str | Path, *, create: bool = True, stop_marker=None) -> "Journal":
        """Open (or create) the journal at `path`.

        `stop_marker`, when given, is checked BEFORE the sqlite connection
        is opened: a set or torn STOP marker refuses the open outright
        (raising StopMarkerPresent), since an operator-cleared marker is
        the only way back in. The runtime must always pass its StopMarker
        here (finding 15) -- the parameter is optional only so a caller can
        name a marker on a filesystem this journal's own path cannot derive
        (e.g. a different mount, per spec §5.5).
        """
        path = Path(path)
        if stop_marker is not None:
            payload = stop_marker.read()
            if stop_marker.exists():
                raise StopMarkerPresent(f"STOP marker present at {stop_marker.path}: {payload}; operator must clear it")
            if isinstance(payload, dict) and payload.get("unreadable"):
                raise StopMarkerPresent(
                    f"STOP marker at {stop_marker.path} is unreadable (torn write): {payload}; operator must clear it"
                )
        existed = path.exists()
        if not create and not existed:
            raise JournalFault(f"journal {path} does not exist")
        # N8: a refused open (bad PRAGMA, or verify_tail's JournalCorrupt on
        # a torn tail) must not leak the sqlite3.Connection -- the operator
        # is about to act on the refusal (fix the file, restore a backup),
        # and a live connection holding the WAL open behind that refusal is
        # exactly the kind of thing that turns a clean recovery into a
        # locked/busy file. `con` is closed on every path out of this try
        # except the one that returns successfully below.
        con = None
        try:
            con = sqlite3.connect(path, isolation_level=None)
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise JournalFault(f"WAL mode not available for {path} (journal_mode={mode!r})")
            con.execute("PRAGMA synchronous=FULL")
            sync = con.execute("PRAGMA synchronous").fetchone()[0]
            if int(sync) != 2:
                raise JournalFault(f"synchronous=FULL not honored for {path} (synchronous={sync!r})")
            con.executescript(DDL)
            journal = cls(con, path)
            if existed:
                # A journal that already existed on disk -- whether opened
                # via the runtime's normal "open or create" call
                # (create=True, the default) or explicitly reopened
                # (create=False, the restart path) -- must never be trusted
                # without checking its last row first (finding 6:
                # create=True on an existing file used to skip this).
                journal.verify_tail()
        except sqlite3.Error as e:
            if con is not None:
                con.close()
            raise JournalFault(str(e)) from e
        except Exception:
            # JournalFault (bad PRAGMA) and JournalCorrupt/StopMarkerPresent
            # (verify_tail) are not sqlite3.Error -- catch everything else
            # here so no exception path leaves `con` open.
            if con is not None:
                con.close()
            raise
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
        one line -- a JSON line for `row` (N4: NOT `types.canonical_sha256`'s
        canonical form -- this uses `default=str`, so an Enum serialises as
        e.g. `"MarketType.PERP"`, not `_canon`'s `"perp"`; consistent with
        `checksum()`, and never raising on an unexpected value is the right
        property for a last-resort log) + "\\n" -- via a write loop (F5: a
        short write, e.g. a signal or ENOSPC mid-line, must not leave a
        torn, unparseable line behind) followed by fsync, so the line lands
        durably or not at all.

        On an OSError opening or writing the out-of-band path itself (I1:
        e.g. the mount is missing, or full) the line is written to stderr
        instead and this never raises -- an EMERGENCY action's record must
        not be lost just because the one place designed to catch failures
        has itself failed."""
        path = Path(path)
        def _append(row: dict[str, Any]) -> None:
            line = (json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_SYNC, 0o600)
                try:
                    off = 0
                    while off < len(line):
                        off += os.write(fd, line[off:])
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                sys.stderr.write(line.decode("utf-8", "replace"))
                sys.stderr.flush()
        return _append

    # --- helpers --------------------------------------------------------------
    def _exec(self, sql: str, params=()):
        try:
            return self.con.execute(sql, params)
        except sqlite3.Error as e:
            raise JournalFault(f"{sql[:40]}...: {e}") from e

    def _insert(self, table: str, row: dict[str, Any], *, or_ignore: bool = False):
        """Plain insert for the non-checksummed tables (epochs,
        runtime_configs, reconciles, incidents, stops, checks): no
        stored-row checksum to compute, just the created_ms default."""
        row = dict(row); row.setdefault("created_ms", _now())
        cols = ",".join(row); qs = ",".join("?" for _ in row)
        self._exec(f"INSERT {'OR IGNORE' if or_ignore else ''} INTO {table}({cols}) VALUES({qs})", tuple(row.values()))

    def _coerced(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """Round-trips `row` through a throwaway TEMP table sharing
        `table`'s column affinities, to see exactly how SQLite would
        coerce/store it (REAL/INTEGER affinity) -- without touching `table`
        itself or requiring its natural key to be free. Used to compare a
        conflicting append against the row already on disk on an
        apples-to-apples (both post-coercion) basis (finding 4/5).

        N9: the scratch TEMP table is built via `CREATE ... AS SELECT ...
        WHERE 0`, which copies column affinity but not `DEFAULT`/`NOT
        NULL` -- a column the caller omits comes back `None` here even if
        `table` declares a non-NULL default for it, which a comparison
        against the stored row (which does carry the default) will then
        judge as a conflict."""
        scratch = f"_scratch_{table}"
        self._exec(f"CREATE TEMP TABLE IF NOT EXISTS {scratch} AS SELECT * FROM {table} WHERE 0")
        cols = list(row); placeholders = ",".join("?" for _ in cols)
        try:
            cur = self._exec(f"INSERT INTO {scratch}({','.join(cols)}) VALUES({placeholders}) RETURNING *", tuple(row.values()))
            return dict(cur.fetchone())
        finally:
            self._exec(f"DELETE FROM {scratch}")

    def _insert_checksummed(self, table: str, row: dict[str, Any], *, conflict_cols: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Insert into a CHECKSUMMED table with the checksum computed over
        the STORED row, not the caller's Python values (finding 2/4):
        insert a placeholder checksum, read the row back via
        `INSERT ... RETURNING *` (so REAL/INTEGER affinity coercion has
        already happened), compute the real checksum from that, then
        `UPDATE ... SET checksum=?` -- all inside one explicit transaction,
        so a crash between the two statements leaves nothing committed.

        When `conflict_cols` names a natural key and a row with that key
        already exists (finding 5): a byte-for-byte idempotent re-append
        (same checksum domain, ignoring the append-time created_ms) returns
        the existing row quietly; a row that differs raises JournalConflict.

        N10: `self.con` uses `isolation_level=None` (SQLite's implicit
        transaction handling disabled), which invites a caller to wrap its
        own `BEGIN`/`COMMIT` around several journal calls -- but nested use
        is unsupported: calling this from inside a caller-opened
        transaction fails loud with `JournalFault: cannot start a
        transaction within a transaction` at the `BEGIN IMMEDIATE` below,
        leaving the caller's own transaction untouched (not rolled back).
        """
        row = dict(row)
        row.setdefault("created_ms", _now())
        _reject_unchecksummable(row)
        cols = list(row) + ["checksum"]
        placeholders = ",".join("?" for _ in cols)
        conflict_clause = f" ON CONFLICT({','.join(conflict_cols)}) DO NOTHING" if conflict_cols else ""
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders}){conflict_clause} RETURNING *"
        self._exec("BEGIN IMMEDIATE")
        try:
            cur = self._exec(sql, (*row.values(), ""))
            inserted = cur.fetchone()
            if inserted is None:
                self._exec("ROLLBACK")
                return self._resolve_conflict(table, row, conflict_cols)
            stored = dict(inserted)
            chk = checksum(_domain(table, stored))
            self._exec(f"UPDATE {table} SET checksum=? WHERE rowid=?", (chk, cur.lastrowid))
            self._exec("COMMIT")
            stored["checksum"] = chk
            return stored
        except Exception:
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _resolve_conflict(self, table: str, row: dict[str, Any], conflict_cols: tuple[str, ...]) -> dict[str, Any]:
        existing = self._exec(
            f"SELECT * FROM {table} WHERE {' AND '.join(f'{c}=?' for c in conflict_cols)}",
            tuple(row[c] for c in conflict_cols),
        ).fetchone()
        if existing is None:
            raise JournalFault(f"{table}: conflict on {conflict_cols} but no existing row found")
        existing = dict(existing)
        coerced = self._coerced(table, {k: v for k, v in row.items() if k != "checksum"})
        if _domain(table, existing, for_identity=True) == _domain(table, coerced, for_identity=True):
            return existing
        key = dict(zip(conflict_cols, (row[c] for c in conflict_cols)))
        raise JournalConflict(f"{table}: conflicting row for {key}")

    def rows(self, table: str, where: str, params) -> list[dict[str, Any]]:
        """N7: hash columns (HEX_HASH_COLUMNS) come back DECODED to Python
        `int` here, not the hex TEXT actually stored -- so `checksum(row)`
        recomputed from a `rows()` result will not match the stored
        checksum (the domain diverges from what was hashed at insert time).
        This is not a defect of `verify_tail`, which reads the raw
        `SELECT *` row; it is the API contract of `rows`/`settlement`/
        `last_settlement`. To verify a specific row's checksum, use
        `verify_tail()` (last row of every checksummed table) rather than
        recomputing from these readers' output."""
        return [_decode_hashes(dict(r)) for r in self._exec(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", tuple(params))]

    # --- writers ----------------------------------------------------------------
    def append_epoch(self, epoch_hash: str, spec_json: str):
        self._insert("epochs", {"epoch_hash": epoch_hash, "spec_json": spec_json}, or_ignore=True)
    def append_runtime_config(self, h: str, config_json: str):
        self._insert("runtime_configs", {"runtime_config_hash": h, "config_json": config_json}, or_ignore=True)
    def append_bar(self, epoch_hash: str, bar: T.NormalizedBar, bars_hash: int) -> dict[str, Any]:
        """N6/finding 5 design note: `(epoch_hash, ts_open)` is the natural
        key. A byte-for-byte idempotent re-append of the same bar is
        silently accepted; a *Revised* bar -- same key, different OHLCV --
        raises JournalConflict BY DESIGN, it does not overwrite the stored
        row. A Revised settled bar is not this layer's call to make: per
        spec §4.1 it is an incident + STOP(FLAT_ONLY), so the runtime (B2)
        must catch JournalConflict here, journal it via
        `append_incident("bars_divergence", ...)`, and never attempt to
        rewrite the bar through this method."""
        return self._insert_checksummed(
            "bars",
            {"epoch_hash": epoch_hash, "ts_open": bar.ts_open, "o": bar.o, "h": bar.h, "l": bar.l, "c": bar.c,
             "v": bar.v, "trade_count": bar.trade_count, "synthesized": int(bar.synthesized),
             "bars_hash": _hash_to_hex(bars_hash)},
            conflict_cols=("epoch_hash", "ts_open"),
        )
    def append_settlement(self, row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        for k in HEX_HASH_COLUMNS:
            if k in row and row[k] is not None:
                row[k] = _hash_to_hex(row[k])
        return self._insert_checksummed("settlements", row, conflict_cols=("epoch_hash", "bar_index"))
    def append_evaluation(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._insert_checksummed("evaluations", row)
    def append_intent(self, epoch_hash: str, intent_key: str, state: str, payload_json: str):
        self._exec("INSERT OR REPLACE INTO intents VALUES(?,?,?,?,?)", (epoch_hash, intent_key, state, payload_json, _now()))
    def append_action(self, row: dict[str, Any]) -> dict[str, Any]:
        # actions is append-only (finding 2): no `terminal` column to set,
        # so a written row's checksum is never invalidated by a later update.
        return self._insert_checksummed("actions", row)
    def update_order_state(self, client_id: str, state_json: str, *, terminal: bool = False) -> None:
        # Finding 2: the only place "is this order done" is recorded now --
        # actions itself is never mutated. actions_non_terminal() reads this
        # back by taking the latest order_states row per client_id.
        self._exec(
            "INSERT INTO order_states(client_id, state_json, terminal, updated_ms) VALUES(?,?,?,?)",
            (client_id, state_json, int(terminal), _now()),
        )
    def append_order_id(self, client_id: str, venue_order_id: str):
        self._exec("INSERT OR IGNORE INTO order_ids VALUES(?,?)", (client_id, venue_order_id))
    def append_fill(self, row: dict[str, Any]) -> dict[str, Any]:
        """`fills` is not checksummed (a venue trade id is the natural
        identity, and a re-poll legitimately repeats them), but a
        conflicting row for the same venue_trade_id is a divergence worth
        surfacing (finding 5) rather than silently keeping whichever value
        arrived first."""
        row = dict(row)
        row.setdefault("created_ms", _now())
        cols = list(row); placeholders = ",".join("?" for _ in cols)
        cur = self._exec(
            f"INSERT INTO fills({','.join(cols)}) VALUES({placeholders}) ON CONFLICT(venue_trade_id) DO NOTHING RETURNING *",
            tuple(row.values()),
        )
        inserted = cur.fetchone()
        if inserted is not None:
            return dict(inserted)
        existing = dict(self._exec("SELECT * FROM fills WHERE venue_trade_id=?", (row["venue_trade_id"],)).fetchone())
        coerced = self._coerced("fills", row)
        ident = lambda r: {k: v for k, v in r.items() if k != "created_ms"}
        if ident(existing) == ident(coerced):
            return existing
        raise JournalConflict(f"fills: conflicting row for venue_trade_id={row['venue_trade_id']!r}")
    def append_reconcile(self, row: dict[str, Any]):
        self._insert("reconciles", row)
    def append_incident(self, kind: str, detail: dict[str, Any]):
        self._insert("incidents", {"kind": kind, "detail_json": json.dumps(detail, sort_keys=True)})
    def append_stop(self, level: str, disposition: str, cause: str):
        self._insert("stops", {"level": level, "disposition": disposition, "cause": cause})
    def append_stop_cleared(self, cause: str) -> bool:
        """Finding 12/N5: StopMarker.clear() has no journal handle to write
        through, so the runtime calls this afterward to leave a journal
        trace of the operator's clear -- `cleared_ms` AND `cleared_cause` on
        the latest still-open (cleared_ms IS NULL) `stops` row, so who/why
        is auditable rather than discarded. Returns True when a row was
        actually cleared, False (never a raise) when there is no open stop
        to clear -- calling this when nothing is open, or a second time
        after someone else already cleared it, is a query the caller can
        act on, not a fault."""
        cur = self._exec(
            "UPDATE stops SET cleared_ms=?, cleared_cause=? WHERE id=(SELECT id FROM stops WHERE cleared_ms IS NULL ORDER BY id DESC LIMIT 1)",
            (_now(), cause),
        )
        return cur.rowcount > 0
    def append_check(self, fencing_token: int, lease_expiry_ms: int, row: dict[str, Any] | None = None):
        self._insert("checks", {"fencing_token": fencing_token, "lease_expiry_ms": lease_expiry_ms, "row_json": json.dumps(row or {}, sort_keys=True)})

    # --- readers ----------------------------------------------------------------
    def last_settlement(self, epoch_hash: str) -> dict[str, Any] | None:
        r = self._exec("SELECT * FROM settlements WHERE epoch_hash=? ORDER BY bar_index DESC LIMIT 1", (epoch_hash,)).fetchone()
        return _decode_hashes(dict(r)) if r else None
    def settlement(self, epoch_hash: str, bar_index: int) -> dict[str, Any] | None:
        r = self._exec("SELECT * FROM settlements WHERE epoch_hash=? AND bar_index=?", (epoch_hash, bar_index)).fetchone()
        return _decode_hashes(dict(r)) if r else None
    def actions_non_terminal(self) -> list[dict[str, Any]]:
        """Finding 2: actions carries no `terminal` column of its own
        anymore -- non-terminal means "the latest order_states row for this
        client_id (if any) has terminal=0", so an action with no
        order_states row yet is non-terminal by definition."""
        sql = """
            SELECT a.* FROM actions a
            LEFT JOIN (
                SELECT os.client_id, os.terminal
                FROM order_states os
                WHERE os.rowid = (SELECT MAX(os2.rowid) FROM order_states os2 WHERE os2.client_id = os.client_id)
            ) latest ON latest.client_id = a.client_id
            WHERE COALESCE(latest.terminal, 0) = 0
            ORDER BY a.rowid
        """
        return [dict(r) for r in self._exec(sql).fetchall()]
    def max_fencing_token(self) -> int:
        r = self._exec("SELECT COALESCE(MAX(fencing_token),0) AS m FROM checks").fetchone(); return int(r["m"])
    def live_check(self, now_ms: int) -> dict[str, Any] | None:
        """N4: the `checks` table is the durable record of every lease ever
        granted -- unlike the lock file, it cannot be deleted by an
        operator or a tmp cleaner out from under a live holder.
        FencedLease.acquire() consults this in addition to the lock file so
        a lease stays enforced even when its lock file is gone: the
        highest-token row whose lease_expiry_ms is still in the future
        (i.e. not yet renewed-away or expired), or None if none is live."""
        r = self._exec(
            "SELECT * FROM checks WHERE lease_expiry_ms > ? ORDER BY fencing_token DESC LIMIT 1", (now_ms,)
        ).fetchone()
        return dict(r) if r else None

    def verify_tail(self):
        """Spec §6: the last row of every checksummed table must verify, else refuse.

        Explicit (called by the runtime's restart path, Plan B2/B3) -- also
        invoked by open() whenever the journal file already existed, so a
        reopened journal refuses a torn tail immediately rather than
        trusting a partially-written last row (finding 6).
        """
        for table in CHECKSUMMED:
            r = self._exec(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            if r is None:
                continue
            row = dict(r); stored = row.get("checksum")
            if checksum(_domain(table, row)) != stored:
                raise JournalCorrupt(f"{table}: last row checksum mismatch (torn or edited write)")
