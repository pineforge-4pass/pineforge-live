"""Broker-neutral strategy and webhook configuration, with no account prerequisites."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from pineforge_live import types as T
from pineforge_live.epoch import CodeIdentity, EpochSpec
from pineforge_live.sources.base import SourceConfig, SourceError, load_history, validate_url


class ConfigError(ValueError):
    """Configuration refusal. Messages identify fields without echoing secret values."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def _object(value, name, required=(), optional=()):
    if not isinstance(value, dict):
        raise ConfigError(f'{name}: expected an object')
    if set(value) - set(required) - set(optional):
        raise ConfigError(f'{name}: unknown fields are forbidden')
    missing = set(required) - set(value)
    if missing:
        raise ConfigError(f'{name}: missing fields: {", ".join(sorted(missing))}')
    return dict(value)


def _string(value, name, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or '\x00' in value:
        raise ConfigError(f'{name}: expected a string')
    return value


def _number(value, name, *, integer=False, minimum=0, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise ConfigError(f'{name}: expected a finite {"integer" if integer else "number"}')
    try:
        valid = math.isfinite(value) and (value > minimum if positive else value >= minimum)
    except OverflowError:
        valid = False
    if not valid:
        raise ConfigError(f'{name}: numeric bound violated')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('JSON: duplicate object key')
        result[key] = value
    return result


def read_json(path: str | Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('JSON: nonfinite number')))
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError('JSON: could not read a valid UTF-8 document') from None


def _path(value, name, base):
    path = Path(_string(value, name)).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _canonical(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


@dataclass(frozen=True)
class WebhookConfig:
    target_url: str
    secret_env: str | None = None
    timeout_ms: int = 10_000
    max_attempts: int = 8
    backoff_initial_ms: int = 1_000
    backoff_max_ms: int = 60_000
    retry_after_max_ms: int = 300_000
    allow_insecure_http: bool = False

    def __post_init__(self):
        if not isinstance(self.allow_insecure_http, bool):
            raise ConfigError('webhook.allow_insecure_http: expected boolean')
        try:
            validate_url(self.target_url, schemes=('https', 'http'), allow_insecure=self.allow_insecure_http)
        except SourceError:
            raise ConfigError('webhook.target_url: use HTTPS, local HTTP, or explicit insecure opt-in; no credentials or fragments') from None
        if self.secret_env is not None and (not isinstance(self.secret_env, str) or not re.fullmatch('[A-Z_][A-Z0-9_]*', self.secret_env)):
            raise ConfigError('webhook.secret_env: expected an environment variable name')
        for key in ('timeout_ms', 'max_attempts', 'backoff_initial_ms', 'backoff_max_ms', 'retry_after_max_ms'):
            _number(getattr(self, key), 'webhook.' + key, integer=True, positive=True)
        if self.backoff_initial_ms > self.backoff_max_ms:
            raise ConfigError('webhook.backoff_initial_ms: exceeds maximum backoff')


@dataclass(frozen=True)
class SignalConfig:
    path: Path
    strategy_path: Path
    strategy_name: str
    strategy_source_path: Path | None
    history_path: Path
    journal_path: Path
    mode: str
    script_tf: str
    instrument: T.InstrumentId
    syminfo: T.EngineSyminfo
    inputs: tuple[tuple[str, str], ...]
    overrides: tuple[tuple[str, str], ...]
    horizon_bars: int
    trigger_mode: str
    webhook: WebhookConfig
    source: SourceConfig
    epoch: EpochSpec
    config_hash: str
    history: tuple[T.NormalizedBar, ...]
    max_eval_rate: int = 5

    @property
    def library_path(self):
        return self.strategy_path


def load_signal_config(path: str | Path) -> SignalConfig:
    """Validate a complete local strategy + historical feed; no network or env reads.

    The compiled library is content-addressed, not executed by this loader.
    Unknown source/compiler provenance is explicitly recorded as unrecorded.
    """
    path = Path(path).expanduser().resolve()
    base = path.parent
    d = _object(read_json(path), 'config',
                ('strategy_path', 'strategy_name', 'history_path', 'journal_path', 'script_tf', 'instrument', 'syminfo', 'webhook', 'source'),
                ('schema_version', 'strategy_source_path', 'mode', 'inputs', 'overrides', 'horizon_bars', 'trigger_mode', 'max_eval_rate'))
    if type(d.get('schema_version', 1)) is not int or d.get('schema_version', 1) != 1:
        raise ConfigError('schema_version: unsupported version')
    strategy = _path(d['strategy_path'], 'strategy_path', base)
    history_path = _path(d['history_path'], 'history_path', base)
    journal_path = _path(d['journal_path'], 'journal_path', base)
    strategy_source = _path(d['strategy_source_path'], 'strategy_source_path', base) if d.get('strategy_source_path') is not None else None
    if strategy.suffix not in {'.so', '.dylib'} or not strategy.is_file():
        raise ConfigError('strategy_path: expected an existing compiled .so or .dylib file')
    if strategy_source is not None and not strategy_source.is_file():
        raise ConfigError('strategy_source_path: expected an existing source file')
    protected_paths = {path, strategy, history_path, strategy_source}
    if journal_path in protected_paths:
        raise ConfigError('journal_path: must differ from configuration, strategy and input files')
    _string(d['strategy_name'], 'strategy_name')
    _string(d['script_tf'], 'script_tf')
    mode = d.get('mode', 'stream')
    trigger_mode = d.get('trigger_mode', 'settled')
    if not isinstance(mode, str) or mode not in {'stream', 'check'}:
        raise ConfigError('mode: expected stream or check')
    if not isinstance(trigger_mode, str) or trigger_mode not in {'settled', 'intrabar'}:
        raise ConfigError('trigger_mode: expected settled or intrabar')
    max_eval_rate = _number(d.get('max_eval_rate', 5), 'max_eval_rate', integer=True, positive=True)
    horizon = _number(d.get('horizon_bars', 1_000_000), 'horizon_bars', integer=True, positive=True)
    if horizon > 2**31 - 1:
        raise ConfigError('horizon_bars: exceeds engine ABI integer range')
    inst = _object(d['instrument'], 'instrument', ('venue', 'market_type', 'symbol'))
    for key in ('venue', 'market_type', 'symbol'):
        _string(inst[key], 'instrument.' + key)
    try:
        inst['market_type'] = T.MarketType(inst['market_type'])
    except ValueError:
        raise ConfigError('instrument.market_type: expected spot, perp or future') from None
    instrument = T.InstrumentId(**inst)
    required_syminfo = [f.name for f in fields(T.EngineSyminfo) if f.name not in {'numeric_metadata', 'string_metadata'}]
    sy = _object(d['syminfo'], 'syminfo', required_syminfo, ('numeric_metadata', 'string_metadata'))
    for key in required_syminfo:
        if key in {'mintick', 'pointvalue', 'pricescale', 'minmove'}:
            _number(sy[key], 'syminfo.' + key, integer=key in {'pricescale', 'minmove'}, positive=True)
        else:
            _string(sy[key], 'syminfo.' + key, empty=key in {'prefix', 'root', 'description', 'volumetype'})
    for kind in ('numeric_metadata', 'string_metadata'):
        metadata = sy.setdefault(kind, {})
        if not isinstance(metadata, dict):
            raise ConfigError('syminfo.' + kind + ': expected an object')
        for key, value in metadata.items():
            _string(key, 'syminfo metadata key')
            if kind == 'numeric_metadata':
                _number(value, 'syminfo numeric metadata value', minimum=-float('inf'))
            else:
                _string(value, 'syminfo string metadata value', empty=True)
    try:
        syminfo = T.EngineSyminfo(**sy)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError('syminfo: invalid or conflicting metadata') from None
    settings = {}
    for key in ('inputs', 'overrides'):
        value = d.get(key, [])
        if not isinstance(value, list) or any(not isinstance(pair, list) or len(pair) != 2 for pair in value):
            raise ConfigError(key + ': expected ordered string pairs')
        for setting, val in value:
            _string(setting, key + '.key')
            _string(val, key + '.value', empty=True)
        if len({pair[0] for pair in value}) != len(value):
            raise ConfigError(key + ': duplicate setting')
        settings[key] = tuple(tuple(pair) for pair in value)
    web = _object(d['webhook'], 'webhook', ('target_url',), [f.name for f in fields(WebhookConfig) if f.name != 'target_url'])
    webhook = WebhookConfig(**web)
    src = _object(d['source'], 'source', ('kind',), [f.name for f in fields(SourceConfig) if f.name != 'kind'])
    if src.get('path') is not None:
        src['path'] = _path(src['path'], 'source.path', base)
    try:
        source = SourceConfig(**src)
        history = tuple(load_history(history_path, d['script_tf']))
    except SourceError as exc:
        raise ConfigError(str(exc)) from None
    runtime_paths={journal_path,journal_path.with_suffix('.report.json'),
                   Path(str(journal_path)+'.stop'),Path(str(journal_path)+'.lock'),
                   Path(str(journal_path)+'.lock.flock'),Path(str(journal_path)+'.tmp'),
                   Path(str(journal_path)+'-wal'),Path(str(journal_path)+'-shm')}
    if {p.resolve() for p in runtime_paths} & (protected_paths | {source.path}):
        raise ConfigError('journal_path: runtime state/report paths must not overlap configuration or input files')
    if horizon <= len(history):
        raise ConfigError('horizon_bars: must exceed seeded history length')
    try:
        library_sha = file_sha256(strategy)
        history_sha = file_sha256(history_path)
        source_sha = file_sha256(strategy_source) if strategy_source else 'unrecorded'
    except OSError:
        raise ConfigError('identity: could not read strategy or history file') from None
    source_identity=_canonical(asdict(source))
    if source.path is not None:
        source_identity['path']=Path(os.path.normpath(d['source']['path'])).as_posix()
    receipt = {'library_sha256': library_sha, 'compiler_id': 'unrecorded', 'codegen_sha': 'unrecorded',
               'source_sha': source_sha, 'source_config_sha256': T.canonical_sha256(source_identity),
               'strategy_name': d['strategy_name'], 'provenance': 'user-supplied compiled strategy'}
    code = CodeIdentity(library_sha, 'unrecorded', source_sha, receipt)
    epoch = EpochSpec(instrument.venue, instrument, d['script_tf'], history[0].ts_open, horizon,
                      code, syminfo, history_sha, inputs=settings['inputs'], overrides=settings['overrides'])
    # Normalize defaults and paths so implicit defaults do not create spurious identities.
    identity = {'strategy_name': d['strategy_name'], 'epoch_hash': epoch.epoch_hash(), 'mode': mode,
                'trigger_mode': trigger_mode, 'max_eval_rate': max_eval_rate, 'webhook': asdict(webhook), 'source': source_identity}
    return SignalConfig(path, strategy, d['strategy_name'], strategy_source, history_path, journal_path,
                        mode, d['script_tf'], instrument, syminfo, settings['inputs'], settings['overrides'],
                        horizon, trigger_mode, webhook, source, epoch, T.canonical_sha256(_canonical(identity)), history, max_eval_rate)
