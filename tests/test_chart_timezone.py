"""Chart display/broker time and exchange time remain separate facts."""
from dataclasses import asdict, replace
import json

import pytest

from pineforge_live import types as T
from pineforge_live.config import ConfigError, load_signal_config
from pineforge_live.epoch import CodeIdentity, EpochSpec, apply_epoch
from pineforge_live.journal import Journal, StopMarker
from pineforge_live.signals.engine import SignalEngine
from pineforge_live.webhooks.store import Outbox
from tests.test_epoch import _FakeHandle, spec
from tests.test_webhook_config import config_document, write_config


def test_default_epoch_keeps_existing_identity_and_setters():
    original = spec()
    assert original.epoch_hash() == 'e0744e73690dec7193620cc0abef81f13250483d8d8467d243ad094d5b3e2f67'
    explicit = replace(original, chart_timezone=original.syminfo.timezone)
    assert explicit.chart_timezone is None
    assert explicit.setter_sequence() == original.setter_sequence()
    assert explicit.epoch_hash() == original.epoch_hash()


@pytest.mark.parametrize('chart_timezone', ['Asia/Taipei', ''])
def test_chart_setter_preserves_exchange_timezone(chart_timezone):
    original = spec()
    changed = replace(original, chart_timezone=chart_timezone)
    handle = _FakeHandle(reject_key='never')
    apply_epoch(handle, changed)
    assert handle.setter_log[:2] == [
        ('set_chart_timezone', (chart_timezone,)),
        ('set_syminfo_timezone', ('UTC',)),
    ]
    assert changed.syminfo.hash() == original.syminfo.hash()
    assert changed.epoch_hash() != original.epoch_hash()


def test_old_serialized_epoch_without_chart_timezone_restores():
    original = spec()
    document = asdict(original)
    document.pop('chart_timezone')
    document.pop('parent_windows')
    document['instrument'] = T.InstrumentId(**document['instrument'])
    document['syminfo'] = T.EngineSyminfo(**document['syminfo'])
    document['code_identity'] = CodeIdentity(**document['code_identity'])
    restored = EpochSpec(**document)
    assert restored.chart_timezone is None
    assert restored.epoch_hash() == original.epoch_hash()


def test_existing_journal_accepts_default_chart_timezone(tmp_path):
    original = spec()
    document = asdict(original)
    document.pop('chart_timezone')
    document.pop('parent_windows')
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False, default=T._canon)
    journal = Journal.open(tmp_path / 'journal.sqlite3')
    try:
        journal.append_epoch(original.epoch_hash(), encoded)
        marker = StopMarker(tmp_path / 'stop')
        marker.prepare()
        outbox = Outbox(journal, original.epoch_hash(), 'http://localhost:9999/hook')
        SignalEngine(object(), original, journal, marker, outbox, strategy_name='test')
        assert journal.rows('epochs', 'epoch_hash=?', (original.epoch_hash(),))[0]['spec_json'] == encoded
    finally:
        journal.close()


def test_config_chart_timezone_is_separate_and_normalizes_default(tmp_path):
    document = config_document(tmp_path)
    original = load_signal_config(write_config(tmp_path, document))
    document['chart_timezone'] = 'UTC'
    explicit = load_signal_config(write_config(tmp_path, document))
    assert explicit.config_hash == original.config_hash
    document['chart_timezone'] = 'Asia/Taipei'
    changed = load_signal_config(write_config(tmp_path, document))
    assert changed.epoch.chart_timezone == 'Asia/Taipei'
    assert changed.syminfo.timezone == 'UTC'
    assert changed.config_hash != original.config_hash
    document['chart_timezone'] = ''
    empty = load_signal_config(write_config(tmp_path, document))
    assert empty.epoch.chart_timezone == ''
    assert empty.epoch.setter_sequence()[0] == ('set_chart_timezone', ('',))


@pytest.mark.parametrize('value', [None, True, 8, {}, [], 'UTC\x00private'])
def test_invalid_chart_config_is_refused(tmp_path, value):
    document = config_document(tmp_path)
    document['chart_timezone'] = value
    with pytest.raises(ConfigError, match='chart_timezone'):
        load_signal_config(write_config(tmp_path, document))


@pytest.mark.parametrize('value', [True, 8, {}, [], 'UTC\x00private'])
def test_invalid_epoch_chart_timezone_is_refused(value):
    with pytest.raises(ValueError, match='chart_timezone'):
        spec(chart_timezone=value)


def test_empty_basecurrency_preserves_engine_default(tmp_path):
    document = config_document(tmp_path)
    document['syminfo']['basecurrency'] = ''
    config = load_signal_config(write_config(tmp_path, document))
    assert config.syminfo.basecurrency == ''
    assert not any(name == 'set_syminfo_string' and args[0] == 'basecurrency'
                   for name, args in config.epoch.setter_sequence())
