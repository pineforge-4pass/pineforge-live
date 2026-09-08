"""SQLite outbox/inbox; methods join the caller's core-decision transaction.

No network operation belongs here. Immutable payload hashes are checked on
every read, including older rows rather than only the journal's latest tail.
"""
from __future__ import annotations

import contextlib
import json

from pineforge_live import types as T
from pineforge_live.journal.journal import JournalConflict, JournalCorrupt
from .identity import canonical, digest


@contextlib.contextmanager
def transaction(journal):
    """Join a core transaction; standalone methods still commit atomically."""
    if journal.con.in_transaction:
        yield
        return
    with journal.transaction():
        yield


def decode_order(value: dict) -> T.OrderAction:
    v = dict(value)
    for key, cls in (("kind", T.OrderKind), ("side", T.Side),
                     ("trigger_basis", T.TriggerBasis), ("lane", T.Lane)):
        v[key] = cls(v[key])
    return T.OrderAction(**v)


class ExecutionStore:
    def __init__(self, journal, epoch_hash: str):
        self.j, self.epoch_hash = journal, epoch_hash

    def _payload(self, row):
        result = dict(row)
        p = json.loads(result["payload_json"])
        if digest(p) != result["payload_hash"]:
            raise JournalCorrupt("execution payload checksum mismatch")
        result["payload"] = p
        return result

    def requests(self) -> list[dict]:
        rows = self.j._exec("SELECT * FROM execution_requests WHERE epoch_hash=? ORDER BY rowid",
                            (self.epoch_hash,)).fetchall()
        return [self._payload(r) for r in rows]

    def request(self, client_id: str) -> dict | None:
        row = self.j._exec("SELECT * FROM execution_requests WHERE epoch_hash=? AND client_id=?",
                           (self.epoch_hash, client_id)).fetchone()
        return self._payload(row) if row is not None else None

    def by_logical_key(self, logical_key: str) -> dict | None:
        row = self.j._exec("SELECT * FROM execution_requests WHERE epoch_hash=? AND logical_key=?",
                           (self.epoch_hash, logical_key)).fetchone()
        return self._payload(row) if row is not None else None

    def next_sequence(self) -> int:
        row = self.j._exec("SELECT COALESCE(MAX(action_seq), 0)+1 FROM actions WHERE epoch_hash=?",
                           (self.epoch_hash,)).fetchone()
        return int(row[0])

    def reserve(self, logical_key: str, payload: dict, order: T.OrderAction,
                run_token: int, now_ms: int) -> str:
        """One atomic reservation; repeats must agree on full request semantics."""
        with transaction(self.j):
            prior = self.by_logical_key(logical_key)
            if prior is not None:
                if prior["payload"]["semantic_hash"] != payload["semantic_hash"]:
                    raise JournalConflict("execution logical key reused with different request")
                return prior["client_id"]
            collision = self.j._exec("SELECT client_id FROM actions WHERE client_id=?", (order.client_id,)).fetchone()
            if collision is not None:
                raise JournalConflict("execution client-id collision")
            p = dict(payload, order=json.loads(canonical(order)))
            self.j.append_action({"client_id": order.client_id, "epoch_hash": self.epoch_hash,
                                  "intent_key": order.intent_key, "action_seq": order.action_seq,
                                  "level_version": order.level_version, "run_token": run_token,
                                  "cls": order.cls, "lane": order.lane.value,
                                  "payload_json": canonical(p), "created_ms": now_ms})
            self.j._exec("INSERT INTO execution_requests VALUES(?,?,?,?,?,'PREPARED',0,NULL,?)",
                         (order.client_id, self.epoch_hash, logical_key, canonical(p), digest(p), now_ms))
            return order.client_id

    def set_state(self, client_id: str, state: str, *, attempt_ms: int | None = None):
        if self.request(client_id) is None:
            raise JournalConflict("state for unknown execution action")
        if attempt_ms is None:
            self.j._exec("UPDATE execution_requests SET state=? WHERE client_id=?", (state, client_id))
        else:
            self.j._exec("UPDATE execution_requests SET state=?, attempts=attempts+1, last_attempt_ms=? WHERE client_id=?",
                         (state, attempt_ms, client_id))

    def notices(self) -> list[dict]:
        return [self._payload(r) for r in self.j._exec(
            "SELECT * FROM execution_notices WHERE epoch_hash=? ORDER BY rowid", (self.epoch_hash,)).fetchall()]

    def put_notice(self, slot_key: str, payload: dict, *, replace: bool = False):
        row = self.j._exec("SELECT * FROM execution_notices WHERE epoch_hash=? AND slot_key=?",
                           (self.epoch_hash, slot_key)).fetchone()
        if row is not None:
            prior = self._payload(row)
            if prior["payload"] == payload:
                return
            if not replace or prior["state"] != "NOTICE":
                raise JournalConflict("cannot replace a released/withdrawn market notice")
            self.j._exec("UPDATE execution_notices SET payload_json=?, payload_hash=? WHERE epoch_hash=? AND slot_key=?",
                         (canonical(payload), digest(payload), self.epoch_hash, slot_key))
        else:
            self.j._exec("INSERT INTO execution_notices VALUES(?,?,?,?,'NOTICE',NULL)",
                         (self.epoch_hash, slot_key, canonical(payload), digest(payload)))

    def finish_notice(self, slot: str, client_id: str | None):
        self.j._exec("UPDATE execution_notices SET state=?, released_client_id=? WHERE epoch_hash=? AND slot_key=? AND state='NOTICE'",
                     ("RELEASED" if client_id else "WITHDRAWN", client_id, self.epoch_hash, slot))

    def cursor(self, stream: str) -> str | None:
        row = self.j._exec("SELECT cursor FROM execution_cursors WHERE epoch_hash=? AND stream=?",
                           (self.epoch_hash, stream)).fetchone()
        return row[0] if row else None

    def set_cursor(self, stream: str, cursor: str):
        self.j._exec("INSERT INTO execution_cursors VALUES(?,?,?) ON CONFLICT(epoch_hash,stream) DO UPDATE SET cursor=excluded.cursor",
                     (self.epoch_hash, stream, cursor))

    def map_order(self, client_id: str, venue_order_id: str):
        if not venue_order_id:
            return
        row = self.j._exec("SELECT venue_order_id FROM order_ids WHERE client_id=?", (client_id,)).fetchone()
        reverse = self.j._exec("SELECT client_id FROM order_ids WHERE venue_order_id=?", (venue_order_id,)).fetchone()
        if (row and row[0] != venue_order_id) or (reverse and reverse[0] != client_id):
            raise JournalConflict("conflicting client/venue order-id mapping")
        self.j.append_order_id(client_id, venue_order_id)

    def client_for_order(self, venue_order_id: str) -> str | None:
        row = self.j._exec("SELECT client_id FROM order_ids WHERE venue_order_id=?", (venue_order_id,)).fetchone()
        return row[0] if row else None

    def record_state(self, client_id: str, state: T.OrderState):
        """Ignore a delayed state only when it contains no new fill information."""
        if state.client_id is not None and state.client_id != client_id:
            raise JournalConflict("order response has a different client id")
        self.map_order(client_id, state.venue_order_id)
        prior = self.latest_state(client_id)
        if prior is not None:
            if state.filled_qty < prior.filled_qty:
                return
            if prior.status in T.TERMINAL_STATUSES:
                if state.filled_qty == prior.filled_qty:
                    return
                raise JournalConflict("terminal order acquired additional unexplained fills")
        self.j.update_order_state(client_id, canonical(state), terminal=state.status in T.TERMINAL_STATUSES)
        self.set_state(client_id, state.status.value)

    def latest_state(self, client_id: str) -> T.OrderState | None:
        row = self.j._exec("SELECT state_json FROM order_states WHERE client_id=? ORDER BY rowid DESC LIMIT 1",
                           (client_id,)).fetchone()
        if row is None:
            return None
        p = json.loads(row[0]); p["status"] = T.OrderStatus(p["status"])
        if p.get("reason_class") is not None:
            p["reason_class"] = T.ReasonClass(p["reason_class"])
        return T.OrderState(**p)

    def append_receipt(self, trade_key: str, payload: dict) -> bool:
        row = self.j._exec("SELECT * FROM execution_receipts WHERE trade_key=?", (trade_key,)).fetchone()
        if row is not None:
            if self._payload(row)["payload"] != payload:
                raise JournalConflict("venue trade id repeated with conflicting payload")
            return False
        self.j._exec("INSERT INTO execution_receipts VALUES(?,?,?,?,NULL)",
                     (trade_key, self.epoch_hash, canonical(payload), digest(payload)))
        return True

    def receipts(self, *, include_consumed: bool = True) -> list[dict]:
        sql = "SELECT rowid AS receipt_rowid,* FROM execution_receipts WHERE epoch_hash=?"
        if not include_consumed:
            sql += " AND consumed_bar IS NULL"
        return [self._payload(r) for r in self.j._exec(sql + " ORDER BY rowid", (self.epoch_hash,)).fetchall()]

    def consume(self, trade_keys: list[str], bar_index: int):
        for key in trade_keys:
            self.j._exec("UPDATE execution_receipts SET consumed_bar=? WHERE trade_key=? AND consumed_bar IS NULL",
                         (bar_index, key))

    def anchor_basis(self, qty: float):
        """Explicit one-time adoption only; restarting must not silently re-anchor."""
        row = self.j._exec("SELECT anchor_qty FROM execution_basis WHERE epoch_hash=?", (self.epoch_hash,)).fetchone()
        if row is not None:
            if row[0] != qty:
                raise JournalConflict("execution basis already anchored; explicit rotation required")
            return
        watermark = max((r["receipt_rowid"] for r in self.receipts()), default=0)
        self.j._exec("INSERT INTO execution_basis VALUES(?,?,?)", (self.epoch_hash, qty, watermark))

    def basis(self) -> float:
        row = self.j._exec("SELECT anchor_qty,anchor_watermark FROM execution_basis WHERE epoch_hash=?", (self.epoch_hash,)).fetchone()
        qty, watermark = (row[0], row[1]) if row else (0.0, 0)
        for row in self.receipts():
            p = row["payload"]
            if row["receipt_rowid"] > watermark and p["cause"] == T.FillCause.OURS.value:
                qty += p["qty"] if p["side"] == T.Side.BUY.value else -p["qty"]
        return qty
