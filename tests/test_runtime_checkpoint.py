"""Crashes cannot leave a settled bar without its execution decision."""
from dataclasses import replace

import pytest

from pineforge_live.drivers.checkpoint import DurableCore, RecoveryRequired
from pineforge_live.journal import JournalCorrupt
from tests.test_live_core import _core, ticks_for
from tests.helpers import load_bars
from pineforge_live.bars import FormingBarBuilder


class Outbox:
    def __init__(self, journal):
        self.j = journal
        journal.con.execute('CREATE TABLE IF NOT EXISTS test_outbox(id TEXT PRIMARY KEY, actions INTEGER)')
    def anchor_basis(self, qty):
        self.basis = qty
    def ingest(self, output, *, phase, bar_index, cycle_seq, decision_id):
        self.j._exec('INSERT INTO test_outbox VALUES(?,?)', (decision_id, len(output.actions)))
        return [decision_id]
    def mark_settled(self, bar_index):
        pass


def runtime(test_so, tmp_path):
    c, j = _core(test_so, tmp_path, breakers=[])
    return DurableCore(c, Outbox(j))


def test_crash_after_settlement_rolls_back_and_replays_once(test_so, test_feed, tmp_path, monkeypatch):
    r = runtime(test_so, tmp_path)
    bars = load_bars(test_feed, 2002)
    assert r.seed(bars[:2000]).settle is not None
    monkeypatch.setattr(r, '_save', lambda: (_ for _ in ()).throw(SystemExit('crash after outbox')))
    with pytest.raises(SystemExit):
        r.settle('bar-2000', bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert r.j.settlement(r.epoch, 2000) is None
    assert r.j.rows('reconciles', '1=1', ()) == []
    assert r.j._exec('SELECT COUNT(*) FROM test_outbox').fetchone()[0] == 0
    with pytest.raises(RecoveryRequired):
        r.evaluate('tick', replace(bars[2001], is_forming=True), 0)
    r.core.h.close()
    r.j.close()
    restored = runtime(test_so, tmp_path)
    restored.restore(bars[:2000])
    result = restored.settle('bar-2000', bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert not result.duplicate and result.output.settle.bar_index == 2000
    again = restored.settle('bar-2000', bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert again.duplicate
    assert restored.j._exec('SELECT COUNT(*) FROM test_outbox').fetchone()[0] == 1
    assert len(restored.j.rows('reconciles', '1=1', ())) == 1


def test_restart_preserves_pending_notice_trigger_and_probe_state(test_so, test_feed, tmp_path):
    r = runtime(test_so, tmp_path)
    bars = load_bars(test_feed, 2003)
    r.seed(bars[:2000])
    r.settle('bar', bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    assert r.core.pending_market
    builder = FormingBarBuilder(r.core.spec.script_tf)
    tick = ticks_for(bars[2001], r.core.spec.script_tf)[0]
    builder.push(tick)
    r.evaluate('tick-1', builder.forming(), tick.ts)
    before = r._state()
    r.core.h.close()
    r.j.close()
    restored = runtime(test_so, tmp_path)
    restored.restore(bars[:2001])
    assert restored._state() == before
    again = restored.evaluate('tick-1', builder.forming(), tick.ts)
    assert again.duplicate
    assert restored._state() == before


def test_changed_decision_payload_and_corrupt_checkpoint_are_refused(test_so, test_feed, tmp_path):
    r = runtime(test_so, tmp_path)
    bars = load_bars(test_feed, 2002)
    r.seed(bars[:2000])
    r.settle('bar', bars[2000], [], set(), set(), -1.0, 0, our_signed_fills=-1.0)
    with pytest.raises(JournalCorrupt, match='different inputs'):
        r.settle('bar', bars[2000], [], set(), set(), 99.0, 0, our_signed_fills=-1.0)
    assert r.poisoned
    r.j._exec("UPDATE core_checkpoints SET payload_json='{}'")
    r.core.h.close()
    r.j.close()
    r = runtime(test_so, tmp_path)
    with pytest.raises(JournalCorrupt, match='checksum'):
        r.restore(bars[:2001])
    with pytest.raises(RecoveryRequired):
        r.evaluate('new', replace(bars[2001], is_forming=True), 0)


def test_legacy_settlement_is_not_silently_adopted(test_so, test_feed, tmp_path):
    r = runtime(test_so, tmp_path)
    bars = load_bars(test_feed, 2000)
    r.core.seed(bars)
    with pytest.raises(RecoveryRequired, match='legacy settlement'):
        r.seed(bars)


def test_nested_journal_transaction_failure_preserves_outer_transaction(tmp_path):
    from pineforge_live.journal import Journal
    from tests.test_journal import settlement
    j = Journal.open(tmp_path / 'transaction.sqlite3')
    with j.transaction():
        j.append_settlement(settlement(1))
        with pytest.raises(SystemExit):
            with j.transaction():
                j.append_settlement(settlement(2))
                raise SystemExit()
        assert j.con.in_transaction
        assert j.settlement('e1', 2) is None
    assert not j.con.in_transaction
    assert j.settlement('e1', 1) is not None


def test_core_cannot_return_success_from_an_uncommitted_outer_transaction(test_so, test_feed, tmp_path):
    r = runtime(test_so, tmp_path)
    bars = load_bars(test_feed, 2001)
    r.seed(bars[:2000])
    with r.j.transaction():
        with pytest.raises(RecoveryRequired, match='own their transaction'):
            r.settle('outer', bars[2000], [], set(), set(), -1, 0, our_signed_fills=-1)
    assert r.core.ledger.n == 2000
    assert r.j.settlement(r.epoch, 2000) is None


def test_separate_outbox_journal_is_refused(test_so, tmp_path):
    from pineforge_live.journal import Journal
    c, j = _core(test_so, tmp_path, breakers=[])
    other = Journal.open(tmp_path / 'other.sqlite3')
    with pytest.raises(RecoveryRequired, match='share one journal'):
        DurableCore(c, Outbox(other))
    c.h.close()
    j.close()
    other.close()


def test_refused_seed_rolls_back_ledger_and_retains_stop(test_so, test_feed, tmp_path):
    r = runtime(test_so, tmp_path)
    bars = load_bars(test_feed, 2000)
    output = r.seed(bars, real_position=99.0)
    assert output.settle is None and r.poisoned
    assert r.j.last_settlement(r.epoch) is None
    assert not r.has_checkpoint()
    assert r.core.marker.exists()
    assert r.j.rows('stops', 'cleared_ms IS NULL', ())
    # Explicit operator clear followed by a new instance can adopt; a
    # refused seed must not strand a newly created journal as legacy state.
    r.core.stop.clear('test operator reviewed initial account')
    r.core.h.close()
    r.j.close()
    r2 = runtime(test_so, tmp_path)
    assert r2.seed(bars, real_position=99.0, adopt_position=True).settle is not None
    assert r2.has_checkpoint()


def test_drained_lease_release_keeps_monotonic_tokens_and_fences_old_owner(tmp_path):
    from pineforge_live.journal import Journal
    from pineforge_live.journal.fence import FencedLease, LeaseLost
    j = Journal.open(tmp_path / 'leases.sqlite3')
    first = FencedLease(tmp_path / 'lease', j)
    assert first.acquire(1000, 0) == 1
    first.release(10)
    assert first.token is None and first.expired(10)
    second = FencedLease(tmp_path / 'lease', j)
    assert second.acquire(1000, 11) == 2
    with pytest.raises(LeaseLost):
        first.release(12)
    assert second.token == 2 and not second.expired(12)
