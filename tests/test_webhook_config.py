import copy
import json
from dataclasses import asdict

import pytest

from pineforge_live.config import ConfigError, WebhookConfig, file_sha256, load_signal_config
from pineforge_live.sources.base import SourceConfig


def config_document(tmp_path):
    (tmp_path / 'strategy.so').write_bytes(b'local fixture library; ABI validation belongs to runtime')
    (tmp_path / 'history.csv').write_text('timestamp,open,high,low,close,volume\n0,100,102,99,101,4\n60000,101,103,100,102,5\n')
    return {
        'strategy_path': 'strategy.so', 'strategy_name': 'sample', 'history_path': 'history.csv',
        'journal_path': 'state/signals.sqlite3', 'script_tf': '1',
        'instrument': {'venue': 'custom', 'market_type': 'spot', 'symbol': 'SAMPLE'},
        'syminfo': {'ticker': 'SAMPLE', 'tickerid': 'custom:SAMPLE', 'prefix': 'custom', 'root': 'SAMPLE',
                    'type': 'stock', 'currency': 'USD', 'basecurrency': 'SAMPLE', 'mintick': 0.01,
                    'pricescale': 100, 'pointvalue': 1, 'minmove': 1, 'session': '24x7',
                    'timezone': 'UTC', 'volumetype': 'base', 'description': 'User supplied sample'},
        'webhook': {'target_url': 'http://127.0.0.1:8765/webhook'}, 'source': {'kind': 'stdin'},
    }


def write_config(tmp_path, document):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(document))
    return path


def test_minimal_config_ready_without_account_network_or_credentials(tmp_path, monkeypatch):
    document = config_document(tmp_path)
    monkeypatch.setenv('DO_NOT_READ', 'this must never enter a report')
    document['webhook']['secret_env'] = 'DO_NOT_READ'
    config = load_signal_config(write_config(tmp_path, document))
    assert config.mode == 'stream' and config.trigger_mode == 'settled'
    assert config.strategy_path == (tmp_path / 'strategy.so').resolve()
    assert config.history[-1].c == 102
    assert config.epoch.code_identity.build_receipt['library_sha256'] == file_sha256(config.strategy_path)
    assert config.epoch.code_identity.codegen_bundle_sha == 'unrecorded'
    assert config.epoch.code_identity.strategy_source_sha == 'unrecorded'
    assert 'this must never enter a report' not in repr(config)
    assert config.source == SourceConfig('stdin')


def test_hashes_change_for_library_history_settings_metadata_and_source(tmp_path):
    d = config_document(tmp_path)
    path = write_config(tmp_path, d)
    baseline = load_signal_config(path)
    (tmp_path / 'strategy.so').write_bytes(b'changed bytes')
    assert load_signal_config(path).epoch.epoch_hash() != baseline.epoch.epoch_hash()
    config_document(tmp_path)
    for section, field, value in [('syminfo', 'pointvalue', 2), ('source', 'poll_interval_ms', 2000)]:
        changed = copy.deepcopy(d)
        changed[section][field] = value
        current = load_signal_config(write_config(tmp_path, changed))
        assert current.config_hash != baseline.config_hash
        assert current.epoch.epoch_hash() != baseline.epoch.epoch_hash()
    changed = copy.deepcopy(d)
    changed['inputs'] = [['length', '21']]
    assert load_signal_config(write_config(tmp_path, changed)).epoch.epoch_hash() != baseline.epoch.epoch_hash()
    (tmp_path / 'history.csv').write_text('timestamp,open,high,low,close,volume\n0,100,102,99,101,9\n60000,101,103,100,102,5\n')
    assert load_signal_config(write_config(tmp_path, d)).epoch.epoch_hash() != baseline.epoch.epoch_hash()


def test_explicit_source_digest_and_normalized_defaults(tmp_path):
    d = config_document(tmp_path)
    (tmp_path / 'strategy.pine').write_text('strategy("sample")')
    d['strategy_source_path'] = 'strategy.pine'
    baseline = load_signal_config(write_config(tmp_path, d))
    assert baseline.epoch.code_identity.strategy_source_sha == file_sha256(tmp_path / 'strategy.pine')
    d.update(mode='stream', trigger_mode='settled', horizon_bars=1_000_000, inputs=[], overrides=[])
    d['webhook'] = asdict(baseline.webhook)
    d['source'] = asdict(baseline.source)
    assert load_signal_config(write_config(tmp_path, d)).config_hash == baseline.config_hash


@pytest.mark.parametrize('field,value', [('horizon_bars', 2), ('horizon_bars', True), ('horizon_bars', float('inf')),
                                           ('mode', 'trade'), ('mode', []), ('trigger_mode', {}), ('horizon_bars', 2**31), ('trigger_mode', 'oracle'), ('script_tf', '1S'),
                                           ('unknown', 'do-not-echo-this'), ('inputs', [['length', '2'], ['length', '3']])])
def test_bad_top_level_values_rejected_without_echo(tmp_path, field, value):
    d = config_document(tmp_path)
    d[field] = value
    with pytest.raises(ConfigError) as error:
        load_signal_config(write_config(tmp_path, d))
    assert 'do-not-echo-this' not in str(error.value)


@pytest.mark.parametrize('field,value', [('mintick', True), ('pointvalue', -1), ('pricescale', 1.5),
                                        ('numeric_metadata', {'foo': float('nan')}),
                                        ('numeric_metadata', {'minmove': 2}), ('timezone', '')])
def test_invalid_syminfo_rejected(tmp_path, field, value):
    d = config_document(tmp_path)
    d['syminfo'][field] = value
    with pytest.raises(ConfigError):
        load_signal_config(write_config(tmp_path, d))


def test_unknown_credential_fields_and_secret_values_rejected(tmp_path):
    d = config_document(tmp_path)
    d['webhook']['secret'] = 'private-value'
    with pytest.raises(ConfigError, match='unknown fields') as error:
        load_signal_config(write_config(tmp_path, d))
    assert 'private-value' not in str(error.value)
    d['webhook'] = {'target_url': 'https://example.test/hook', 'secret_env': 'not a variable name'}
    with pytest.raises(ConfigError, match='environment variable name'):
        load_signal_config(write_config(tmp_path, d))


@pytest.mark.parametrize('url', ['http://example.test/hook', 'https://name:secret@example.test/hook',
                                  'https://example.test/hook#fragment', 'file:///tmp/hook', 'https://example.test:99999'])
def test_webhook_target_validation(url):
    with pytest.raises(ConfigError):
        WebhookConfig(url)


def test_local_http_and_explicit_insecure_are_usable():
    assert WebhookConfig('http://localhost:8765/hook').target_url
    assert WebhookConfig('http://[::1]:8765/hook').target_url
    assert WebhookConfig('http://service.lan/hook', allow_insecure_http=True).target_url
    with pytest.raises(ConfigError):
        WebhookConfig('https://example.test', allow_insecure_http='true')
    with pytest.raises(ConfigError):
        WebhookConfig('https://example.test', max_attempts=True)


def test_duplicate_json_keys_refused(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"strategy_path":"a.so","strategy_path":"b.so"}')
    with pytest.raises(ConfigError, match='duplicate'):
        load_signal_config(path)


def test_missing_library_history_and_journal_alias_refused(tmp_path):
    d = config_document(tmp_path)
    d['strategy_path'] = 'missing.so'
    with pytest.raises(ConfigError, match='strategy_path'):
        load_signal_config(write_config(tmp_path, d))
    d['strategy_path'] = 'strategy.so'
    d['history_path'] = 'missing.csv'
    with pytest.raises(ConfigError, match='history'):
        load_signal_config(write_config(tmp_path, d))
    d['history_path'] = 'history.csv'
    d['journal_path'] = 'history.csv'
    with pytest.raises(ConfigError, match='journal_path'):
        load_signal_config(write_config(tmp_path, d))


def test_committed_example_materializes_with_user_artifacts(tmp_path):
    from pathlib import Path
    template = Path(__file__).resolve().parents[1] / 'examples' / 'signal-config.json'
    document = json.loads(template.read_text())
    (tmp_path / 'strategy.so').write_bytes(b'example user-supplied binary for config-only validation')
    (tmp_path / 'history.csv').write_text('timestamp,open,high,low,close,volume\n0,100,102,99,101,4\n900000,101,103,100,102,5\n')
    config = load_signal_config(write_config(tmp_path, document))
    assert config.script_tf == '15'
    assert config.source.kind == 'stdin'
    assert config.webhook.secret_env == 'PINEFORGE_WEBHOOK_SECRET'


def test_max_eval_rate_is_explicit_positive_and_part_of_runtime_identity(tmp_path):
    d = config_document(tmp_path)
    default = load_signal_config(write_config(tmp_path, d))
    assert default.max_eval_rate == 5
    d['max_eval_rate'] = 7
    changed = load_signal_config(write_config(tmp_path, d))
    assert changed.max_eval_rate == 7 and changed.config_hash != default.config_hash
    assert changed.epoch.epoch_hash() == default.epoch.epoch_hash()
    for invalid in (0, -1, True, 1.5, '5'):
        d['max_eval_rate'] = invalid
        with pytest.raises(ConfigError, match='max_eval_rate'):
            load_signal_config(write_config(tmp_path, d))


@pytest.mark.parametrize('suffix',['.report.json','.stop','.lock','.lock.flock','.tmp','-wal','-shm'])
def test_runtime_sidecars_cannot_overwrite_configuration(tmp_path,suffix):
    d=config_document(tmp_path)
    d['journal_path']='signals.sqlite3'
    name='signals.report.json' if suffix=='.report.json' else 'signals.sqlite3'+suffix
    path=tmp_path/name;path.write_text(json.dumps(d))
    with pytest.raises(ConfigError,match='overlap'):
        load_signal_config(path)
    assert json.loads(path.read_text())==d


def test_runtime_report_cannot_overwrite_jsonl_source(tmp_path):
    d=config_document(tmp_path)
    d['journal_path']='signals.sqlite3'
    source=tmp_path/'signals.report.json';source.write_text('')
    d['source']={'kind':'jsonl','path':source.name}
    with pytest.raises(ConfigError,match='overlap'):
        load_signal_config(write_config(tmp_path,d))


def test_report_symlink_cannot_alias_configuration(tmp_path):
    d=config_document(tmp_path);d['journal_path']='signals.sqlite3'
    path=write_config(tmp_path,d)
    (tmp_path/'signals.report.json').symlink_to(path)
    with pytest.raises(ConfigError,match='overlap'):
        load_signal_config(path)
    assert json.loads(path.read_text())==d
