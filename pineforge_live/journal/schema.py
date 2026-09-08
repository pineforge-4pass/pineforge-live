"""Spec §6 tables. Every mutable-history table carries a checksum column.

Column notes:
- `bars.bars_hash` and `settlements.bars_hash`/`broker_state_hash` are
  declared TEXT and store the 64-bit FNV-1a hash as 16 lowercase hex digits
  (`f"{h:016x}"`, finding 3) so the full uint64 range is representable --
  SQLite's INTEGER storage class cannot hold values >= 2**63 without
  overflowing. `Journal` readers (`last_settlement`, `settlement`, `rows`)
  decode these columns back to Python `int` before returning a row.
- `bars`/`fills` carry `created_ms` (finding 1): journal write time is
  useful provenance even though it isn't part of either table's identity.
- `actions` has no `terminal` column (finding 2): it is append-only -- a
  written action row is never mutated again, so its checksum can never go
  stale. "Is this action still pending?" is answered from the latest
  `order_states` row per `client_id` instead (`order_states.terminal`,
  written by `Journal.update_order_state`).
- `schema_meta` (R4) holds exactly one row recording SCHEMA_VERSION at
  create time. v1 journals are not migrated: a journal that already
  existed on disk but has no `schema_meta` row (i.e. predates this table,
  before 57b9730) is refused by `Journal.open` with a clear message
  rather than silently backfilled.
"""
DDL = """
CREATE TABLE IF NOT EXISTS epochs(epoch_hash TEXT PRIMARY KEY, spec_json TEXT NOT NULL, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_configs(runtime_config_hash TEXT PRIMARY KEY, config_json TEXT NOT NULL, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bars(epoch_hash TEXT NOT NULL, ts_open INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,
  trade_count INTEGER, synthesized INTEGER NOT NULL DEFAULT 0, bars_hash TEXT NOT NULL, created_ms INTEGER NOT NULL,
  checksum TEXT NOT NULL, PRIMARY KEY(epoch_hash, ts_open));
CREATE TABLE IF NOT EXISTS settlements(epoch_hash TEXT NOT NULL, bar_index INTEGER NOT NULL, runtime_config_hash TEXT NOT NULL,
  bars_hash TEXT NOT NULL, broker_state_hash TEXT NOT NULL, trades_len INTEGER NOT NULL, position REAL NOT NULL,
  equity REAL NOT NULL, trades_sha256 TEXT NOT NULL, created_ms INTEGER NOT NULL, checksum TEXT NOT NULL, PRIMARY KEY(epoch_hash, bar_index));
CREATE TABLE IF NOT EXISTS evaluations(id INTEGER PRIMARY KEY AUTOINCREMENT, epoch_hash TEXT NOT NULL, trigger TEXT NOT NULL,
  tick_seq_from INTEGER, tick_seq_to INTEGER, forming_json TEXT NOT NULL, outcome TEXT NOT NULL, recompute_ms INTEGER,
  created_ms INTEGER NOT NULL, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents(epoch_hash TEXT NOT NULL, intent_key TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL,
  updated_ms INTEGER NOT NULL, PRIMARY KEY(epoch_hash, intent_key));
CREATE TABLE IF NOT EXISTS actions(client_id TEXT PRIMARY KEY, epoch_hash TEXT NOT NULL, intent_key TEXT NOT NULL,
  action_seq INTEGER NOT NULL, level_version INTEGER NOT NULL, run_token INTEGER NOT NULL, cls TEXT NOT NULL, lane TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_ms INTEGER NOT NULL, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_states(client_id TEXT NOT NULL, state_json TEXT NOT NULL, terminal INTEGER NOT NULL DEFAULT 0,
  updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_ids(client_id TEXT PRIMARY KEY, venue_order_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills(venue_trade_id TEXT PRIMARY KEY, client_id TEXT, venue_order_id TEXT, ts INTEGER, side TEXT,
  qty REAL, price REAL, fee REAL, cause TEXT, target_bar_index INTEGER, cls TEXT, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reconciles(id INTEGER PRIMARY KEY AUTOINCREMENT, epoch_hash TEXT NOT NULL, bar_index INTEGER, bar_ts_open INTEGER NOT NULL, cause TEXT,
  detail_json TEXT, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, detail_json TEXT, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS stops(id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, disposition TEXT NOT NULL, cause TEXT NOT NULL,
  cleared_ms INTEGER, cleared_cause TEXT, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS checks(fencing_token INTEGER PRIMARY KEY, lease_expiry_ms INTEGER NOT NULL, row_json TEXT, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ticks(seq INTEGER PRIMARY KEY, ts INTEGER NOT NULL, price REAL NOT NULL, qty REAL NOT NULL);
CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS execution_requests(
  client_id TEXT PRIMARY KEY, epoch_hash TEXT NOT NULL, logical_key TEXT NOT NULL,
  payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt_ms INTEGER,
  created_ms INTEGER NOT NULL, UNIQUE(epoch_hash, logical_key));
CREATE TABLE IF NOT EXISTS execution_notices(
  epoch_hash TEXT NOT NULL, slot_key TEXT NOT NULL, payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL, state TEXT NOT NULL, released_client_id TEXT,
  PRIMARY KEY(epoch_hash, slot_key));
CREATE TABLE IF NOT EXISTS execution_receipts(
  trade_key TEXT PRIMARY KEY, epoch_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL, consumed_bar INTEGER);
CREATE TABLE IF NOT EXISTS execution_cursors(
  epoch_hash TEXT NOT NULL, stream TEXT NOT NULL, cursor TEXT NOT NULL,
  PRIMARY KEY(epoch_hash, stream));
CREATE TABLE IF NOT EXISTS execution_basis(
  epoch_hash TEXT PRIMARY KEY, anchor_qty REAL NOT NULL, anchor_watermark INTEGER NOT NULL);
"""
CHECKSUMMED = ("settlements", "actions", "evaluations", "bars")

# Version 2 adds the settled bar timestamp to reconciles. Old pre-alpha
# journals are preserved and refused before DDL or WAL changes; they must
# not silently acquire fabricated trading-day provenance.
SCHEMA_VERSION = 2

# Columns excluded from a table's checksum domain even though they are part
# of the stored row (finding 2/4): `evaluations.id` is AUTOINCREMENT and not
# part of the row's logical content, so it is dropped from both the
# insert-time checksum and verify_tail's recompute.
CHECKSUM_EXCLUDE: dict[str, tuple[str, ...]] = {"evaluations": ("id",)}

# Hash columns stored as 16-hex TEXT (finding 3); decoded back to int by readers.
HEX_HASH_COLUMNS = ("bars_hash", "broker_state_hash")
