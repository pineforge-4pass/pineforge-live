"""Local B3 coordinator: commit requests first, submit/adopt outside SQLite.

The existing Executor protocol cannot prove exhaustive NOT_FOUND coverage
or authenticate observed order fields. Ambiguous absent orders therefore
remain UNKNOWN here; automatic NOT_FOUND resubmission is deliberately fenced.
"""
from __future__ import annotations

import json
import math

from pineforge_live import types as T
from pineforge_live.core.classify import VenueFill
from pineforge_live.core.live import ActionRequest
from pineforge_live.journal.journal import JournalConflict
from .identity import canonical, client_id, digest, floor_quantity
from .store import ExecutionStore, decode_order, transaction
from .types import ExecutionReceipt, ExecutionSafetyError, ExecutionSnapshot

_TERMINAL = {s.value for s in T.TERMINAL_STATUSES} | {"WITHDRAWN"}
_RECEIPT_KINDS = {"CORRECTION", "FLATTEN", "SYNTHETIC_CLOSE", "MARKET_NOW"}
_SUPPORTED = _RECEIPT_KINDS | {"TRIGGER", "MARKET_AT_OPEN", "CANCEL_STALE_CYCLE"}


def _request(value: dict) -> ActionRequest:
    v = dict(value); v["side"] = T.Side(v["side"])
    return ActionRequest(**v)


class ExecutionCoordinator:
    def __init__(self, journal, executor, clock, *, epoch_hash: str, run_token: int,
                 instrument: T.InstrumentId, constraints: T.VenueConstraints, lease_check=None,
                 permits=None):
        self.j, self.executor, self.clock = journal, executor, clock
        self.epoch_hash, self.run_token = epoch_hash, run_token
        self.instrument, self.constraints = instrument, constraints
        self.lease_check = lease_check
        self.permits = permits
        self.store = ExecutionStore(journal, epoch_hash)

    def _fence(self):
        if self.lease_check is not None and self.lease_check() is False:
            raise ExecutionSafetyError("execution lease/STOP gate refused submission")

    def _no_transaction(self):
        if self.j.con.in_transaction:
            raise ExecutionSafetyError("network execution inside a journal transaction is forbidden")

    def _slot(self, action: ActionRequest, cycle_seq: int) -> str:
        return canonical([self.epoch_hash, cycle_seq, action.intent,
                          "EXIT" if action.reduce_only else "ENTRY", action.target_bar_index])

    def _reserve(self, action: ActionRequest, *, cycle_seq: int, origin_bar_index: int,
                 decision_id: str | None, operation: str = "submit", target_client_id: str | None = None,
                 parent_client_id: str | None = None) -> str:
        if action.kind not in _SUPPORTED:
            raise ExecutionSafetyError(f"unsupported core action {action.kind!r}")
        if action.intent == "?" and not action.reduce_only:
            raise ExecutionSafetyError("ambiguous entry intent cannot become a venue order")
        leg = "EXIT" if action.reduce_only else "ENTRY"
        role = action.cls or action.kind
        logical_key = canonical([self.epoch_hash, cycle_seq, action.intent, leg,
                                 action.target_bar_index, action.kind, role, target_client_id])
        prior = self.store.by_logical_key(logical_key)
        if prior is not None and parent_client_id is None:
            # Completed parents disappear from pending-close discovery;
            # retries retain the dependency already bound to this order.
            parent_client_id = prior["payload"].get("parent_client_id")
        semantic_hash = digest({"request": action, "operation": operation,
                                "target_client_id": target_client_id, "parent_client_id": parent_client_id})
        if prior is not None:
            if prior["payload"]["semantic_hash"] != semantic_hash:
                raise JournalConflict("submitted logical action was changed; adopt before replacement")
            return prior["client_id"]
        qty = floor_quantity(action.qty, self.constraints.lot_step)
        if operation == "submit":
            if qty <= 0 or qty < self.constraints.min_qty:
                raise ExecutionSafetyError("quantity below the venue lot/minimum")
            if qty > self.constraints.market_max_qty:
                raise ExecutionSafetyError("market quantity needs sequenced chunks; not submitted")
            price = action.price_hint
            if price is not None and (not math.isfinite(price) or price <= 0):
                raise ExecutionSafetyError("invalid execution price hint")
            if (price is not None and qty * price < self.constraints.min_notional and
                    not (action.reduce_only and self.constraints.reduce_only_min_notional_exempt)):
                raise ExecutionSafetyError("quantity below venue min notional")
        seq = self.store.next_sequence()
        intent_key = canonical(["execution", action.intent, cycle_seq, origin_bar_index, role])
        cid = client_id(self.epoch_hash, intent_key, role, 0, seq, self.run_token)
        lane = (T.Lane.EMERGENCY if action.reduce_only or operation == "cancel" or
                action.kind in {"CORRECTION", "SYNTHETIC_CLOSE", "FLATTEN"} else T.Lane.DISCRETIONARY)
        order = T.OrderAction(cid, intent_key, 0, seq, T.OrderKind.MARKET, action.side, qty,
                              None, None, action.reduce_only, False, "BOTH", "GTC",
                              T.TriggerBasis.LAST, lane, role, action.reason)
        payload = {"request": json.loads(canonical(action)), "semantic_hash": semantic_hash,
                   "operation": operation, "target_client_id": target_client_id,
                   "parent_client_id": parent_client_id, "cycle_seq": cycle_seq,
                   "origin_bar_index": origin_bar_index,
                   "expected_ledger_bar_index": None if action.kind in _RECEIPT_KINDS else action.target_bar_index,
                   "receipt_mode": "ACTION_RECEIPT" if action.kind in _RECEIPT_KINDS else "LEDGER_FILL",
                   "decision_id": decision_id, "quantization_residual": action.qty - qty}
        return self.store.reserve(logical_key, payload, order, self.run_token, self.clock.now_ms())

    def ingest(self, output, *, phase: str, bar_index: int, cycle_seq: int,
               decision_id: str | None = None) -> list[str]:
        """Persist one core output, joining the caller's transaction; never await.

        A completed probe is required to release notices even when its action
        list is empty. Repeated ingestion reuses the durable logical order.
        """
        if phase not in {"seed", "settle", "evaluate", "recover"}:
            raise ValueError("unknown execution phase")
        ids: list[str] = []
        with transaction(self.j):
            actions = list(output.actions)
            for a in actions:
                if a.kind not in _SUPPORTED:
                    raise ExecutionSafetyError(f"unsupported core action {a.kind!r}")
            # Cancels/withdraws win over an advance or an entry in the batch.
            withdrawn, stale = set(), set()
            for a in actions:
                if a.kind != "CANCEL_STALE_CYCLE" and a.qty != 0:
                    continue
                if a.kind != "CANCEL_STALE_CYCLE":
                    slot = self._slot(a, cycle_seq)
                    withdrawn.add(slot)
                    self.store.finish_notice(slot, None)
                    continue
                stale.add((a.intent, a.target_bar_index))
                for notice in self.store.notices():
                    p = notice["payload"]; req = _request(p["request"])
                    if req.intent == a.intent and req.target_bar_index == a.target_bar_index:
                        self.store.finish_notice(notice["slot_key"], None)
                for row in self.store.requests():
                    p = row["payload"]; old = p["request"]
                    if (old["intent"] == a.intent and p["cycle_seq"] <= cycle_seq and
                            p["operation"] == "submit" and row["state"] not in _TERMINAL):
                        if row["state"] == "PREPARED":
                            self.store.set_state(row["client_id"], "WITHDRAWN")
                            continue
                        ids.append(self._reserve(a, cycle_seq=cycle_seq, origin_bar_index=bar_index,
                                                 decision_id=decision_id, operation="cancel",
                                                 target_client_id=row["client_id"]))
            normal = [a for a in actions if a.kind != "CANCEL_STALE_CYCLE" and a.qty > 0 and
                      (a.intent, a.target_bar_index) not in stale and
                      self._slot(a, cycle_seq) not in withdrawn]
            for a in normal:
                if a.kind != "MARKET_AT_OPEN":
                    continue
                slot = self._slot(a, cycle_seq)
                existing = next((n for n in self.store.notices() if n["slot_key"] == slot), None)
                payload = {"request": json.loads(canonical(a)), "cycle_seq": cycle_seq,
                           "origin_bar_index": bar_index if phase == "settle" else bar_index - 1}
                if existing is not None:
                    payload["origin_bar_index"] = existing["payload"]["origin_bar_index"]
                    if existing["state"] != "NOTICE":
                        old = existing["payload"]["request"]
                        if old["qty"] != a.qty or old["side"] != a.side.value:
                            raise JournalConflict("market request changed after release/withdrawal")
                        continue
                if phase == "settle" and existing is not None:
                    self.store.put_notice(slot, payload)
                else:
                    self.store.put_notice(slot, payload, replace=phase == "evaluate")
            # A two-leg reversal waits for its close's terminal receipt.
            closes: dict[tuple, list[str]] = {}
            def parent_for(a):
                if a.reduce_only:
                    return None
                candidates = set(closes.get((a.side, a.target_bar_index), []))
                for row in self.store.requests():
                    p = row["payload"]
                    if (p["operation"] == "submit" and p["request"]["reduce_only"] and
                            p["request"]["side"] == a.side.value and row["state"] not in _TERMINAL and
                            (row["state"] != "ACKED" or p["order"]["kind"] == T.OrderKind.MARKET.value)):
                        candidates.add(row["client_id"])
                if not candidates:
                    return None
                if len(candidates) != 1:
                    raise ExecutionSafetyError("multiple closing actions make reversal attribution ambiguous")
                return next(iter(candidates))
            direct = [a for a in normal if a.kind != "MARKET_AT_OPEN"]
            direct.sort(key=lambda a: not a.reduce_only)
            for a in direct:
                key = (a.side, a.target_bar_index)
                cid = self._reserve(a, cycle_seq=cycle_seq, origin_bar_index=bar_index,
                                    decision_id=decision_id,
                                    parent_client_id=parent_for(a))
                ids.append(cid)
                if a.reduce_only:
                    closes.setdefault(key, []).append(cid)
            probe = getattr(output, "probe", None)
            if phase == "evaluate" and probe is not None and not probe.aborted:
                if probe.bar_index != bar_index or not math.isfinite(probe.forming.o) or probe.forming.o <= 0:
                    raise ExecutionSafetyError("completed evaluation has no valid target-bar open")
                # A STOPped evaluation may withdraw notices, never release one.
                if getattr(output, "stop", None) is not None:
                    for n in self.store.notices():
                        if n["payload"]["request"]["target_bar_index"] == bar_index:
                            self.store.finish_notice(n["slot_key"], None)
                    return ids
                notices = [n for n in self.store.notices() if n["state"] == "NOTICE" and
                           n["payload"]["request"]["target_bar_index"] == bar_index]
                notices.sort(key=lambda n: not n["payload"]["request"]["reduce_only"])
                for n in notices:
                    p = n["payload"]; a = _request(p["request"])
                    key = (a.side, a.target_bar_index)
                    cid = self._reserve(a, cycle_seq=p["cycle_seq"], origin_bar_index=p["origin_bar_index"],
                                        decision_id=decision_id,
                                        parent_client_id=parent_for(a))
                    self.store.finish_notice(n["slot_key"], cid); ids.append(cid)
                    if a.reduce_only:
                        closes.setdefault(key, []).append(cid)
        return ids

    def anchor_basis(self, qty: float):
        if not math.isfinite(qty):
            raise ValueError("basis must be finite")
        with transaction(self.j):
            self.store.anchor_basis(qty)

    def _validate_state(self, row: dict, state: T.OrderState):
        order = decode_order(row["payload"]["order"])
        if any(not math.isfinite(x) or x < 0 for x in
               (state.filled_qty, state.submitted_qty, state.requested_qty)):
            raise ExecutionSafetyError("venue returned invalid order quantities")
        if state.filled_qty > order.qty + self.constraints.lot_step / 2:
            raise ExecutionSafetyError("venue order overfilled its submitted quantity")
        if state.submitted_qty > order.qty + self.constraints.lot_step / 2:
            raise ExecutionSafetyError("adopted venue quantity differs from durable action")
        if abs(state.requested_qty - order.qty) > self.constraints.lot_step / 2:
            raise ExecutionSafetyError("adopted requested quantity differs from durable action")
        if (state.status not in {T.OrderStatus.PENDING, T.OrderStatus.REJECTED, T.OrderStatus.UNKNOWN}
                and abs(state.submitted_qty - order.qty) > self.constraints.lot_step / 2):
            raise ExecutionSafetyError("adopted submitted quantity differs from durable action")

    async def _adopt(self, row: dict) -> bool:
        self._no_transaction()
        state = await self.executor.lookup(row["client_id"], None)
        if state is None:
            with transaction(self.j):
                self.store.set_state(row["client_id"], "UNKNOWN")
            return False
        self._validate_state(row, state)
        with transaction(self.j):
            self.store.record_state(row["client_id"], state)
        return True

    async def _send(self, row: dict):
        self._no_transaction(); self._fence()
        p = row["payload"]
        order = decode_order(p["order"])
        if p["operation"] == "submit" and self.permits is not None and not self.permits(order):
            raise ExecutionSafetyError("current STOP state refuses this order")
        parent = p.get("parent_client_id")
        if parent:
            state = self.store.latest_state(parent)
            if state is None or state.status is not T.OrderStatus.FILLED:
                return
            position = await self.executor.position(self.instrument)
            if abs(position) > self.constraints.lot_step / 2:
                raise ExecutionSafetyError("reversal close has not left venue flat")
        if not p["request"]["reduce_only"] and p["operation"] == "submit":
            closing = [r for r in self.store.requests()
                       if r["payload"]["operation"] == "submit" and r["payload"]["request"]["reduce_only"]
                       and (r["state"] in {"PREPARED", "SUBMITTING", "UNKNOWN", "PENDING", "PARTIAL"} or
                            (r["state"] == "ACKED" and r["payload"]["order"]["kind"] == T.OrderKind.MARKET.value))]
            if closing:
                return
            unknown = [r["client_id"] for r in self.store.requests()
                       if r["state"] in {"UNKNOWN", "SUBMITTING"} and r["client_id"] != row["client_id"]]
            if unknown:
                raise ExecutionSafetyError("unresolved previous submission fences new entry")
        self._fence()
        if p["operation"] == "submit" and self.permits is not None and not self.permits(order):
            raise ExecutionSafetyError("current STOP state refuses this order")
        with transaction(self.j):
            self.store.set_state(row["client_id"], "SUBMITTING", attempt_ms=self.clock.now_ms())
        try:
            if p["operation"] == "cancel":
                state = await self.executor.cancel(p["target_client_id"], None)
                target = self.store.request(p["target_client_id"])
                self._validate_state(target, state)
                with transaction(self.j):
                    self.store.record_state(p["target_client_id"], state)
                    self.store.set_state(row["client_id"], "FILLED")
            else:
                state = await self.executor.submit(order)
                self._validate_state(row, state)
                with transaction(self.j):
                    self.store.record_state(row["client_id"], state)
        except T.AdapterError as exc:
            with transaction(self.j):
                if exc.reason_class is T.ReasonClass.TERMINAL:
                    self.store.set_state(row["client_id"], "REJECTED")
                else:
                    self.store.set_state(row["client_id"], "UNKNOWN")
                self.j.append_incident("execution_submit_error", {"client_id": row["client_id"],
                                                               "reason_class": exc.reason_class.value})
        except (TimeoutError, ConnectionError, OSError):
            with transaction(self.j):
                self.store.set_state(row["client_id"], "UNKNOWN")

    def _ingest_fill(self, fill: T.Fill):
        if not fill.venue_trade_id or not fill.venue_order_id or not all(math.isfinite(x) for x in (fill.qty, fill.price, fill.fee)) or fill.qty <= 0 or fill.price <= 0:
            raise ExecutionSafetyError("invalid venue fill identity/quantity/price")
        mapped = self.store.client_for_order(fill.venue_order_id)
        if mapped and fill.client_id and mapped != fill.client_id:
            raise JournalConflict("fill client id conflicts with venue order map")
        cid = fill.client_id or mapped
        row = self.store.request(cid) if cid else None
        if row is not None:
            self.store.map_order(cid, fill.venue_order_id)
            p = row["payload"]; order = decode_order(p["order"])
            if fill.side is not order.side:
                raise ExecutionSafetyError("fill side differs from durable action")
            cause = (T.FillCause.OURS if fill.cause in {T.FillCause.OURS, T.FillCause.UNATTRIBUTED}
                     else fill.cause)
        else:
            p = {"request": {"intent": None, "reduce_only": False, "kind": "EXTERNAL"},
                 "origin_bar_index": None, "expected_ledger_bar_index": None,
                 "receipt_mode": "LEDGER_FILL"}
            cause = T.FillCause.UNATTRIBUTED if fill.cause is T.FillCause.OURS else fill.cause
        trade_key = canonical([self.instrument.key(), fill.venue_trade_id])
        receipt = {"trade_key": trade_key, "client_id": cid if row else None,
                   "venue_order_id": fill.venue_order_id, "origin_bar_index": p["origin_bar_index"],
                   "expected_ledger_bar_index": p["expected_ledger_bar_index"],
                   "observed_bar_index": -1, "receipt_mode": p["receipt_mode"],
                   "intent": p["request"]["intent"],
                   "leg": ("EXIT" if p["request"]["reduce_only"] else "ENTRY") if row else None,
                   "side": fill.side.value, "qty": fill.qty, "price": fill.price,
                   "fee": fill.fee, "cause": cause.value,
                   "executed_trigger": p["request"]["kind"] == "TRIGGER"}
        self.store.append_receipt(trade_key, receipt)
        # Verify the full normalized fill on duplicates too: timestamps
        # are journal provenance even though receipt matching uses bars.
        self.j.append_fill({"venue_trade_id": trade_key, "client_id": cid if row else None,
                                "venue_order_id": fill.venue_order_id, "ts": fill.ts, "side": fill.side.value,
                                "qty": fill.qty, "price": fill.price, "fee": fill.fee, "cause": cause.value,
                                "target_bar_index": p["expected_ledger_bar_index"], "cls": p["receipt_mode"]})
        if row is not None:
            total = sum(r["payload"]["qty"] for r in self.store.receipts()
                        if r["payload"]["client_id"] == cid)
            if total > order.qty + self.constraints.lot_step / 2:
                raise ExecutionSafetyError("venue trades overfill durable action")

    async def poll(self):
        """Persist each full history page before advancing its cursor."""
        self._no_transaction()
        states, next_cursor = await self.executor.orders_since(self.instrument, self.store.cursor("orders"))
        with transaction(self.j):
            for state in states:
                cid = state.client_id or self.store.client_for_order(state.venue_order_id)
                row = self.store.request(cid) if cid else None
                if row is not None:
                    self._validate_state(row, state); self.store.record_state(cid, state)
            self.store.set_cursor("orders", next_cursor)
        fills, next_cursor = await self.executor.fills_since(self.instrument, self.store.cursor("fills"))
        with transaction(self.j):
            for fill in fills:
                self._ingest_fill(fill)
            self.store.set_cursor("fills", next_cursor)

    async def recover(self):
        self._no_transaction()
        await self.poll()
        for row in self.store.requests():
            if row["state"] in {"UNKNOWN", "SUBMITTING", "PENDING", "PARTIAL", "ACKED"}:
                if row["payload"]["operation"] == "cancel":
                    # Cancellation is idempotent; use the original target.
                    await self._send(row)
                else:
                    await self._adopt(row)
        await self.poll()
        return self.snapshot(-1)

    async def drain(self, deadline_ms: int | None = None):
        """Drive prepared orders and adoption; a timeout returns unresolved ids."""
        self._no_transaction()
        for _ in range(1000):
            await self.poll()
            rows = self.store.requests()
            rows.sort(key=lambda r: (r["payload"]["operation"] != "cancel",
                                     not r["payload"]["request"]["reduce_only"]))
            for row in rows:
                if row["state"] == "PREPARED":
                    await self._send(row)
                elif row["state"] in {"UNKNOWN", "SUBMITTING"}:
                    if row["payload"]["operation"] == "cancel":
                        await self._send(row)
                    else:
                        await self._adopt(row)
            await self.poll()
            snap = self.snapshot(-1)
            if not snap.unresolved or deadline_ms is None or self.clock.now_ms() >= deadline_ms:
                return snap
            await self.clock.sleep_until(min(deadline_ms, self.clock.now_ms() + 50))
        raise ExecutionSafetyError("execution drain made no bounded progress")

    def snapshot(self, bar_index: int) -> ExecutionSnapshot:
        receipts, late, groups = [], [], {}
        rows = self.store.receipts(include_consumed=False)
        for row in rows:
            p = dict(row["payload"])
            p["side"] = T.Side(p["side"]); p["cause"] = T.FillCause(p["cause"])
            p["observed_bar_index"] = bar_index
            r = ExecutionReceipt(**p)
            if r.receipt_mode == "ACTION_RECEIPT":
                receipts.append(r)
            elif r.expected_ledger_bar_index is not None and r.expected_ledger_bar_index < bar_index:
                late.append(r)
            elif r.expected_ledger_bar_index is None or r.expected_ledger_bar_index == bar_index:
                key = (r.client_id, r.intent, r.leg, r.side, r.cause, r.executed_trigger)
                groups.setdefault(key, []).append(r)
        venue = []
        for key, fills in groups.items():
            cid, intent, leg, side, cause, trigger = key
            qty = sum(r.qty for r in fills)
            venue.append(VenueFill(intent, leg, side, qty, sum(r.qty*r.price for r in fills)/qty,
                                   bar_index, cause, cid, trigger))
        in_flight, mirrored, unresolved, residuals = set(), set(), [], {}
        all_receipts = self.store.receipts()
        for row in self.store.requests():
            if row["state"] in _TERMINAL:
                if row["state"] != "WITHDRAWN" and row["payload"]["operation"] == "submit":
                    order = decode_order(row["payload"]["order"])
                    filled = sum(r["payload"]["qty"] for r in all_receipts
                                 if r["payload"]["client_id"] == row["client_id"])
                    if order.qty - filled > self.constraints.lot_step / 2:
                        residuals[row["client_id"]] = order.qty - filled
                continue
            p = row["payload"]; intent = p["request"]["intent"]
            order = decode_order(p["order"])
            if row["state"] == "ACKED" and order.kind is not T.OrderKind.MARKET:
                if intent is not None:
                    mirrored.add(intent)
            else:
                unresolved.append(row["client_id"])
                if intent is not None:
                    in_flight.add(intent)
        return ExecutionSnapshot(venue, receipts, late, in_flight, mirrored, self.store.basis(),
                                 max((r["receipt_rowid"] for r in all_receipts), default=0), unresolved, residuals)

    def mark_settled(self, bar_index: int):
        """Consume only receipts this exact settlement has been given, atomically."""
        with transaction(self.j):
            keys = [r["trade_key"] for r in self.store.receipts(include_consumed=False)
                    if r["payload"]["receipt_mode"] == "ACTION_RECEIPT" or
                    r["payload"]["expected_ledger_bar_index"] in {None, bar_index}]
            self.store.consume(keys, bar_index)
