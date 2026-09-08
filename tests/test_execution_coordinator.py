import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pineforge_live import types as T
from pineforge_live.adapters.mock import MockExecutor, SubmitFault, default_constraints
from pineforge_live.adapters.tape import TapeClock
from pineforge_live.core.live import ActionRequest, CoreOutput
from pineforge_live.execution.coordinator import ExecutionCoordinator
from pineforge_live.execution.identity import client_id, floor_quantity
from pineforge_live.execution.types import ExecutionSafetyError
from pineforge_live.execution.store import decode_order
from pineforge_live.journal.journal import Journal, JournalConflict, JournalCorrupt


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def env(tmp_path):
    journal = Journal.open(tmp_path / "execution.sqlite3")
    instrument = T.InstrumentId("TAPE", T.MarketType.PERP, "ETH-USD")
    clock = TapeClock(1000)
    mock = MockExecutor(instrument)
    mock.advance(100.0, 1000)
    c = ExecutionCoordinator(journal, mock, clock, epoch_hash="e"*64, run_token=1,
                             instrument=instrument, constraints=default_constraints())
    yield c, mock, journal, clock
    journal.close()


def request(kind="TRIGGER", *, intent="L", qty=1.0, side=T.Side.BUY,
            reduce_only=False, bar=4, reason="confirmed", cls=None):
    return ActionRequest(kind, intent, side, qty, None if kind == "MARKET_AT_OPEN" else 100.0,
                         reduce_only, cls or kind, reason, bar)


def evaluation(*actions, bar=4, aborted=False, stop=None):
    out = CoreOutput(actions=list(actions), stop=stop)
    out.probe = SimpleNamespace(bar_index=bar, aborted=aborted,
                                forming=T.NormalizedBar(3600000, 100, 101, 99, 100, 5, 2, True))
    return out


def ingest(c, *actions, phase="settle", bar=4, cycle=0):
    return c.ingest(CoreOutput(actions=list(actions)), phase=phase, bar_index=bar, cycle_seq=cycle)


def test_ids_are_stable_and_physically_distinct():
    common = ("epoch", 'L|\\"漢', "market", 0, 1, 5)
    assert client_id(*common) == client_id(*common)
    assert len(client_id(*common)) == 36
    assert client_id(*common) != client_id("epoch", common[1], "stop", 0, 1, 5)
    assert client_id(*common) != client_id("epoch", common[1], "market", 0, 2, 5)
    assert client_id(*common) != client_id("epoch", common[1], "market", 0, 1, 6)
    with pytest.raises(ValueError):
        client_id(*common, max_length=20)


@pytest.mark.parametrize("qty,step,expected", [(0.3, 0.1, 0.3), (1.999, .01, 1.99), (.0099, .01, 0)])
def test_decimal_floor_never_rounds_up(qty, step, expected):
    assert floor_quantity(qty, step) == expected


@pytest.mark.parametrize("qty,step", [(float("nan"), .01), (float("inf"), .01), (-1, .01), (1, 0), (True, .01)])
def test_invalid_quantities_refuse(qty, step):
    with pytest.raises(ValueError):
        floor_quantity(qty, step)


def test_ingest_is_transactional_and_never_touches_venue(env):
    c, mock, j, _ = env
    with pytest.raises(RuntimeError):
        with j.transaction():
            ingest(c, request())
            assert len(c.store.requests()) == 1
            assert run(mock.position(c.instrument)) == 0
            raise RuntimeError("crash before core checkpoint commit")
    assert c.store.requests() == []
    assert j.rows("actions", "1=1", []) == []


def test_duplicate_ingest_submits_one_physical_order(env):
    c, mock, _, _ = env
    first = ingest(c, request())
    assert ingest(c, request()) == first
    run(c.drain()); run(c.drain())
    assert run(mock.position(c.instrument)) == 1
    fills, _ = run(mock.fills_since(c.instrument, None))
    assert len(fills) == 1
    assert c.snapshot(4).our_signed_fills == 1
    assert len(c.snapshot(4).ledger_fills) == 1


def test_client_id_collision_refuses_without_overwriting(env, monkeypatch):
    c, _, _, _ = env
    monkeypatch.setattr("pineforge_live.execution.coordinator.client_id", lambda *args: "collision")
    ingest(c, request())
    with pytest.raises(JournalConflict, match="collision"):
        ingest(c, request(intent="different"))
    assert len(c.store.requests()) == 1


def test_changed_request_for_reserved_physical_slot_refuses(env):
    c, _, _, _ = env
    ingest(c, request())
    with pytest.raises(JournalConflict):
        ingest(c, request(qty=2))


def test_market_notice_requires_non_aborted_target_evaluation(env):
    c, mock, _, _ = env
    ingest(c, request("MARKET_AT_OPEN"), bar=3)
    run(c.drain())
    assert run(mock.position(c.instrument)) == 0
    c.ingest(evaluation(aborted=True), phase="evaluate", bar_index=4, cycle_seq=0)
    run(c.drain())
    assert run(mock.position(c.instrument)) == 0
    assert c.store.requests() == []
    c.ingest(evaluation(), phase="evaluate", bar_index=4, cycle_seq=0)
    run(c.drain())
    assert run(mock.position(c.instrument)) == 1


def test_requote_supersedes_notice_and_repeated_success_fills_once(env):
    c, mock, _, _ = env
    ingest(c, request("MARKET_AT_OPEN", qty=1), bar=3)
    req = request("MARKET_AT_OPEN", qty=2, reason="open_requote")
    c.ingest(evaluation(req), phase="evaluate", bar_index=4, cycle_seq=0)
    c.ingest(evaluation(req), phase="evaluate", bar_index=4, cycle_seq=0)
    run(c.drain())
    assert run(mock.position(c.instrument)) == 2
    assert len(c.store.requests()) == 1


def test_notice_withdraw_prevents_every_later_release(env):
    c, mock, _, _ = env
    ingest(c, request("MARKET_AT_OPEN"), bar=3)
    cancel = request("MARKET_AT_OPEN", qty=0, reason="withdraw")
    c.ingest(evaluation(cancel), phase="evaluate", bar_index=4, cycle_seq=0)
    c.ingest(evaluation(), phase="evaluate", bar_index=4, cycle_seq=0)
    run(c.drain())
    assert run(mock.position(c.instrument)) == 0
    assert c.store.notices()[0]["state"] == "WITHDRAWN"


def test_persistent_stop_gate_rechecked_after_notice_release(env):
    c, mock, _, _ = env
    ingest(c, request("MARKET_AT_OPEN"), bar=3)
    c.ingest(evaluation(), phase="evaluate", bar_index=4, cycle_seq=0)
    c.permits = lambda order: order.reduce_only
    with pytest.raises(ExecutionSafetyError, match="STOP"):
        run(c.drain())
    assert run(mock.position(c.instrument)) == 0


def test_lease_loss_prevents_network_send(env):
    c, mock, _, _ = env
    ingest(c, request())
    c.lease_check = lambda: False
    with pytest.raises(ExecutionSafetyError, match="lease"):
        run(c.drain())
    assert run(mock.position(c.instrument)) == 0


def test_network_is_forbidden_inside_core_transaction(env):
    c, _, j, _ = env
    with j.transaction():
        with pytest.raises(ExecutionSafetyError, match="transaction"):
            run(c.poll())


def test_accepted_timeout_reopens_and_adopts_once(env):
    c, mock, j, clock = env
    mock.plan(SubmitFault(timeout="after_accept", omit_client_id=True))
    ids = ingest(c, request())
    run(c.drain())
    restarted = ExecutionCoordinator(j, mock, clock, epoch_hash=c.epoch_hash, run_token=2,
                                     instrument=c.instrument, constraints=c.constraints)
    run(restarted.recover()); run(restarted.drain())
    assert restarted.store.requests()[0]["client_id"] == ids[0]
    assert run(mock.position(c.instrument)) == 1
    assert len(run(mock.fills_since(c.instrument, None))[0]) == 1
    assert restarted.snapshot(4).our_signed_fills == 1


def test_not_found_after_timeout_never_guesses_and_resubmits(env):
    c, mock, _, _ = env
    mock.plan(SubmitFault(timeout="before_accept"))
    ingest(c, request())
    run(c.drain()); run(c.recover()); run(c.drain())
    assert run(mock.position(c.instrument)) == 0
    assert c.store.requests()[0]["state"] == "UNKNOWN"
    assert c.store.requests()[0]["attempts"] == 1
    ingest(c, request(intent="another", bar=5), bar=5)
    with pytest.raises(ExecutionSafetyError, match="unresolved"):
        run(c.drain())


def test_settle_correction_gets_receipt_not_fake_next_bar_fill(env):
    c, mock, _, _ = env
    ingest(c, request("CORRECTION", cls="TOP_UP"))
    run(c.drain())
    snap = c.snapshot(5)
    assert snap.ledger_fills == []
    assert len(snap.receipts) == 1
    assert snap.receipts[0].origin_bar_index == 4
    assert snap.receipts[0].expected_ledger_bar_index is None
    assert snap.receipts[0].observed_bar_index == 5
    assert snap.our_signed_fills == run(mock.position(c.instrument)) == 1
    c.mark_settled(5)
    assert c.snapshot(5).receipts == []
    assert c.snapshot(5).our_signed_fills == 1


def test_late_ledger_fill_remains_unconsumed_origin_receipt(env):
    c, _, _, _ = env
    ingest(c, request(bar=4))
    run(c.drain())
    snap = c.snapshot(5)
    assert snap.ledger_fills == []
    assert len(snap.late_receipts) == 1
    assert snap.late_receipts[0].expected_ledger_bar_index == 4
    c.mark_settled(5)
    assert len(c.snapshot(5).late_receipts) == 1


def test_partial_fills_aggregate_and_duplicate_poll_preserves_basis(env):
    c, mock, _, _ = env
    mock.plan(SubmitFault(partial_fill_ratio=.5, duplicate_events=1))
    ingest(c, request())
    run(c.drain())
    assert c.snapshot(4).in_flight == {"L"}
    mock.advance(102, 1001)
    run(c.poll())
    snap = c.snapshot(4)
    assert len(snap.ledger_fills) == 1
    assert snap.ledger_fills[0].qty == 1
    assert snap.ledger_fills[0].price == 101
    with c.j.transaction():
        c.store.set_cursor("fills", "fills:0")
    run(c.poll())
    assert c.snapshot(4).our_signed_fills == 1
    assert len(c.store.receipts()) == 2


def test_wrong_side_fill_rolls_back_inbox_and_cursor(env):
    c, mock, j, _ = env
    ingest(c, request())
    row = c.store.requests()[0]
    state = run(mock.submit(decode_order(row["payload"]["order"])))
    with j.transaction():
        c.store.record_state(row["client_id"], state)
    mock._fills[0] = replace(mock._fills[0], side=T.Side.SELL)
    with pytest.raises(ExecutionSafetyError, match="side"):
        run(c.poll())
    assert c.store.cursor("fills") is None
    assert c.store.receipts() == []


def test_reversal_different_ids_waits_for_close_fill(env):
    c, mock, _, _ = env
    mock._position = 2.0
    c.anchor_basis(2)
    mock.plan(SubmitFault(partial_fill_ratio=.5))
    close = request(intent="close-old", qty=2, side=T.Side.SELL, reduce_only=True)
    enter = request(intent="enter-short", qty=1, side=T.Side.SELL)
    ingest(c, enter, close)
    run(c.drain())
    assert run(mock.position(c.instrument)) == 1
    assert len(run(mock.orders_since(c.instrument, None))[0]) == 2  # ACK and partial, close only
    mock.advance(100, 1001)
    run(c.drain())
    assert run(mock.position(c.instrument)) == -1
    assert len(run(mock.fills_since(c.instrument, None))[0]) == 3


def test_cancel_prepared_action_never_sends_it(env):
    c, mock, _, _ = env
    ingest(c, request())
    ingest(c, request("CANCEL_STALE_CYCLE", qty=0), phase="evaluate")
    run(c.drain())
    assert run(mock.position(c.instrument)) == 0
    assert c.store.requests()[0]["state"] == "WITHDRAWN"
    assert c.snapshot(4).terminal_residuals == {}


def test_terminal_identity_is_validated_before_ignoring_delayed_event(env):
    c, _, j, _ = env
    ingest(c, request())
    run(c.drain())
    row = c.store.requests()[0]
    state = c.store.latest_state(row["client_id"])
    with pytest.raises(JournalConflict, match="different client"):
        with j.transaction():
            c.store.record_state(row["client_id"], replace(state, client_id="foreign"))


def test_rejected_order_is_a_terminal_residual_and_never_retried(env):
    c, mock, _, _ = env
    mock.plan(SubmitFault(rejection=T.ReasonClass.TERMINAL))
    ids = ingest(c, request())
    run(c.drain()); run(c.drain())
    assert c.snapshot(4).terminal_residuals == {ids[0]: 1}
    assert c.store.requests()[0]["attempts"] == 1


def test_checksum_protects_old_outbox_rows(env):
    c, _, j, _ = env
    first = ingest(c, request())[0]
    ingest(c, request(intent="second"))
    j._exec("UPDATE execution_requests SET payload_hash='bad' WHERE client_id=?", (first,))
    with pytest.raises(JournalCorrupt):
        c.snapshot(4)


def test_basis_anchor_does_not_launder_restart_or_foreign_fill(env):
    c, mock, j, clock = env
    c.anchor_basis(2)
    with pytest.raises(JournalConflict):
        c.anchor_basis(0)
    restarted = ExecutionCoordinator(j, mock, clock, epoch_hash=c.epoch_hash, run_token=2,
                                     instrument=c.instrument, constraints=c.constraints)
    assert restarted.snapshot(4).our_signed_fills == 2


def test_mark_consumed_joins_transaction(env):
    c, _, j, _ = env
    ingest(c, request())
    run(c.drain())
    with pytest.raises(RuntimeError):
        with j.transaction():
            c.mark_settled(4)
            raise RuntimeError("checkpoint did not commit")
    assert len(c.snapshot(4).ledger_fills) == 1


@pytest.mark.parametrize("committed", [False, True])
def test_process_exit_preserves_exact_outbox_commit_boundary(tmp_path, committed):
    path = tmp_path / "crash.sqlite3"
    code = r'''
import os, sys
from pineforge_live import types as T
from pineforge_live.adapters.mock import MockExecutor, default_constraints
from pineforge_live.adapters.tape import TapeClock
from pineforge_live.core.live import ActionRequest, CoreOutput
from pineforge_live.execution.coordinator import ExecutionCoordinator
from pineforge_live.journal.journal import Journal
j = Journal.open(sys.argv[1])
i = T.InstrumentId("TAPE", T.MarketType.PERP, "ETH-USD")
c = ExecutionCoordinator(j, MockExecutor(i), TapeClock(1000), epoch_hash="e"*64,
                         run_token=1, instrument=i, constraints=default_constraints())
a = ActionRequest("TRIGGER", "L", T.Side.BUY, 1, 100, False, "TRIGGER", "confirmed", 4)
if sys.argv[2] == "commit":
    c.ingest(CoreOutput(actions=[a]), phase="settle", bar_index=4, cycle_seq=0)
    os._exit(23)
with j.transaction():
    c.ingest(CoreOutput(actions=[a]), phase="settle", bar_index=4, cycle_seq=0)
    os._exit(23)
'''
    proc = subprocess.run([sys.executable, "-c", code, str(path), "commit" if committed else "rollback"],
                          env=os.environ.copy(), capture_output=True, text=True)
    assert proc.returncode == 23, proc.stderr
    j = Journal.open(path)
    try:
        assert len(j.rows("actions", "1=1", [])) == int(committed)
        assert len(j.rows("execution_requests", "1=1", [])) == int(committed)
        j.verify_tail()
    finally:
        j.close()


def test_process_death_after_venue_acceptance_adopts_saved_identity(env, monkeypatch):
    c, mock, j, clock = env
    original = mock.submit
    async def accepted_then_killed(action):
        await original(action)
        raise SystemExit("process died before acknowledgement write")
    monkeypatch.setattr(mock, "submit", accepted_then_killed)
    ids = ingest(c, request())
    with pytest.raises(SystemExit):
        run(c.drain())
    assert c.store.requests()[0]["state"] == "SUBMITTING"
    assert run(mock.position(c.instrument)) == 1
    monkeypatch.setattr(mock, "submit", original)
    restarted = ExecutionCoordinator(j, mock, clock, epoch_hash=c.epoch_hash, run_token=2,
                                     instrument=c.instrument, constraints=c.constraints)
    run(restarted.recover()); run(restarted.drain())
    assert restarted.store.requests()[0]["client_id"] == ids[0]
    assert len(run(mock.fills_since(c.instrument, None))[0]) == 1
