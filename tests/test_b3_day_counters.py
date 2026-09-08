"""Daily caps follow bar event time, including restart across UTC midnight."""
import json
import sqlite3
from dataclasses import replace

import pytest

from pineforge_live.core.live import DAY_MS, LiveCore
from pineforge_live.journal import Journal, JournalFault
from tests.test_live_core import _core
from tests.helpers import load_bars


def test_restore_uses_latest_bar_day_and_admitted_actions(tmp_path):
    j = Journal.open(tmp_path / 'journal.sqlite3')
    spec = type('Spec', (), {'epoch_hash': lambda self: 'epoch'})()
    for bar_index, ts, wall, counters in [
        (0, DAY_MS - 1, 9 * DAY_MS, {'mirror_early': 7, 'admitted_reconciles': 6}),
        (1, DAY_MS, 9 * DAY_MS + 1, {'mirror_early': 2, 'admitted_reconciles': 3, 'flattened': 5}),
        (2, DAY_MS + 1, 10 * DAY_MS, {'mirror_early': 1, 'admitted_reconciles': 0, 'missed_corrected': 9}),
    ]:
        j.append_reconcile({'epoch_hash': 'epoch', 'bar_index': bar_index, 'bar_ts_open': ts,
                            'created_ms': wall, 'cause': 'test', 'detail_json': json.dumps(counters)})
    c = object.__new__(LiveCore)
    c.j, c.spec = j, spec
    assert c._restore_day_counters() == (3, 3)
    assert c._day == 1
    j.close()


def test_caps_reset_on_real_bar_day_change(test_so, test_feed, tmp_path):
    c, j = _core(test_so, tmp_path)
    bars = load_bars(test_feed, 2300)
    bar = bars[2000]
    c._roll_day(bar)
    c.mirror_early_today, c.reconciles_today = 2, 3
    c._roll_day(replace(bar, ts_open=bar.ts_open + 1))
    assert (c.mirror_early_today, c.reconciles_today) == (2, 3)
    c._roll_day(replace(bar, ts_open=(bar.ts_open // DAY_MS + 1) * DAY_MS))
    assert (c.mirror_early_today, c.reconciles_today) == (0, 0)
    c.h.close()
    j.close()


def test_v1_refusal_does_not_change_database_or_create_wal(tmp_path):
    p = tmp_path / 'old.sqlite3'
    con = sqlite3.connect(p)
    con.executescript('CREATE TABLE schema_meta(version INTEGER NOT NULL); INSERT INTO schema_meta VALUES(1);')
    con.close()
    before = p.read_bytes()
    with pytest.raises(JournalFault, match='expected 2'):
        Journal.open(p)
    assert p.read_bytes() == before
    assert not p.with_name(p.name + '-wal').exists()
    assert not p.with_name(p.name + '-shm').exists()
