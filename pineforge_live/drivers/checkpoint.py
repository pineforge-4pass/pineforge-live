"""Atomic core decisions and restart state, with no network inside SQLite transactions."""
from __future__ import annotations

import dataclasses
import enum
import json
from collections import defaultdict, deque

from pineforge_live import types as T
from pineforge_live.core.classify import ClassifiedFill, EmulatedFill, FillClass, VenueFill
from pineforge_live.core.live import ActionRequest, CoreOutput
from pineforge_live.core.probe import ProbeFill
from pineforge_live.journal import JournalCorrupt


class RecoveryRequired(RuntimeError):
    """The process has uncertain memory; restart from its last durable checkpoint."""


class _SeedRefused(Exception):
    def __init__(self, output):
        self.output = output


_CLASSES = {c.__name__: c for c in (ActionRequest, ClassifiedFill, EmulatedFill, VenueFill, ProbeFill,
                                    T.NormalizedBar)}
_ENUMS = {c.__name__: c for c in (T.Side, T.FillCause, T.StopLevel, T.StopDisposition, FillClass)}
_FIELDS = (
    '_day', 'mirror_early_today', 'reconciles_today', 'pending_market', '_triggered_bar', '_triggered',
    '_our_fills', 'missed_since', '_carried_missed', '_not_quiescent_streak', '_disagree_alerted',
    '_hard_flat_issued', '_horizon_alerted', '_bar_committed_qty',
)


def _encode(value):
    """Closed JSON vocabulary; no pickle, imports, or arbitrary constructors."""
    if isinstance(value, enum.Enum):
        return {'enum': type(value).__name__, 'value': value.value}
    if dataclasses.is_dataclass(value):
        if type(value).__name__ not in _CLASSES:
            raise TypeError(f'unsupported checkpoint class {type(value).__name__}')
        return {'class': type(value).__name__, 'fields': {f.name: _encode(getattr(value, f.name))
                                                        for f in dataclasses.fields(value)}}
    if isinstance(value, dict):
        return {'map': [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, (tuple, set)):
        items = [_encode(v) for v in value]
        if isinstance(value, set):
            items.sort(key=lambda x: json.dumps(x, sort_keys=True))
        return {'tuple' if isinstance(value, tuple) else 'set': items}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'unsupported checkpoint value {type(value).__name__}')


def _decode(value):
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {'enum', 'value'}:
        return _ENUMS[value['enum']](value['value'])
    if set(value) == {'class', 'fields'}:
        return _CLASSES[value['class']](**{k: _decode(v) for k, v in value['fields'].items()})
    if set(value) == {'map'}:
        return {_decode(k): _decode(v) for k, v in value['map']}
    if set(value) == {'tuple'}:
        return tuple(_decode(v) for v in value['tuple'])
    if set(value) == {'set'}:
        return set(_decode(v) for v in value['set'])
    raise ValueError('unknown checkpoint encoding')


def _json(value):
    return json.dumps(_encode(value), sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclasses.dataclass(frozen=True)
class CommittedStep:
    output: CoreOutput
    client_ids: tuple[str, ...]
    duplicate: bool = False


class DurableCore:
    """Persist core state and coordinator outbox in the same decision commit.

    ExecutionCoordinator.ingest must be synchronous and transaction-aware.
    Network submission is a separate call after this method returns. On any
    failed transaction this instance is poisoned: mutated engine/core memory
    must never be used against rolled-back durable state.
    """
    def __init__(self, core, coordinator):
        self.core, self.execution, self.j = core, coordinator, core.j
        self.poisoned = False
        self.ready = False
        if self.j.con.in_transaction:
            raise RecoveryRequired('construct runtime outside a decision transaction')
        self.epoch = core.spec.epoch_hash()
        if coordinator.j is not self.j:
            raise RecoveryRequired('core and execution must share one journal connection')
        if getattr(coordinator, 'epoch_hash', self.epoch) != self.epoch:
            raise RecoveryRequired('core and execution epoch mismatch')
        if getattr(coordinator, 'instrument', core.spec.instrument) != core.spec.instrument:
            raise RecoveryRequired('core and execution instrument mismatch')
        self.j.con.executescript('''
CREATE TABLE IF NOT EXISTS core_checkpoints(
 epoch_hash TEXT PRIMARY KEY, bar_index INTEGER NOT NULL, config_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS core_decisions(
 epoch_hash TEXT NOT NULL, decision_id TEXT NOT NULL, input_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, checksum TEXT NOT NULL, PRIMARY KEY(epoch_hash, decision_id));
''')

    def _state(self):
        c = self.core
        return {'version': 1, 'fields': {k: getattr(c, k) for k in _FIELDS},
                'guard': [c.guard._fills, c.guard._book_ops],
                'probe_fills': dict(c.probe.prev_fills), 'probe_retracts': dict(c.probe.retracted_history),
                'g3': {name: list(w._d) for name, w in c.g3_windows.items()},
                'bars_hash': c.ledger.bars_hash, 'broker_hash': c.ledger.last.hashes[-1]}

    def _config_hash(self):
        c = self.core
        return T.canonical_sha256([c.rc.hash(), dataclasses.asdict(c.limits),
                                   dataclasses.asdict(c.rcfg), dataclasses.asdict(c.dead_band),
                                   [dataclasses.asdict(b) for b in c.g3.breakers], c.probe.policy])

    def _save(self):
        payload = _json(self._state())
        self.j._exec('INSERT OR REPLACE INTO core_checkpoints VALUES(?,?,?,?,?)',
                     (self.epoch, self.core.ledger.n - 1, self._config_hash(), payload,
                      T.canonical_sha256([self.epoch, self.core.ledger.n - 1, self._config_hash(), payload])))

    def has_checkpoint(self):
        return self.j._exec('SELECT 1 FROM core_checkpoints WHERE epoch_hash=?', (self.epoch,)).fetchone() is not None

    def seed(self, history, *, real_position=None, adopt_position=False):
        self._usable()
        if self.has_checkpoint():
            raise RecoveryRequired('checkpoint exists; use restore with its exact settled history')
        if self.j.last_settlement(self.epoch) is not None:
            raise RecoveryRequired('legacy settlement has no atomic runtime checkpoint; preserve journal for audit')
        try:
            with self.j.transaction():
                output = self.core.seed(history, real_position=real_position, adopt_position=adopt_position)
                if output.settle is None:
                    raise _SeedRefused(output)
                self.execution.anchor_basis(self.core.our_signed_fills)
                self._save()
                self.ready = True
            return output
        except _SeedRefused as refusal:
            self.poisoned = True
            if refusal.output.stop is not None:
                level, disposition, cause = refusal.output.stop
                self.j.append_stop(level.value, disposition.value, cause)
            for incident in refusal.output.incidents:
                self.j.append_incident(incident['kind'], incident)
            return refusal.output
        except BaseException:
            self.poisoned = True
            raise

    def restore(self, history):
        """Recompute/G1 first, then restore only the matching committed state."""
        self._usable()
        self.ready = False
        row = self.j._exec('SELECT * FROM core_checkpoints WHERE epoch_hash=?', (self.epoch,)).fetchone()
        if row is None:
            raise RecoveryRequired('no runtime checkpoint')
        row = dict(row)
        if row['checksum'] != T.canonical_sha256([row['epoch_hash'], row['bar_index'], row['config_hash'], row['payload_json']]):
            self.poisoned = True
            raise JournalCorrupt('runtime checkpoint checksum mismatch')
        if row['config_hash'] != self._config_hash() or len(history) != row['bar_index'] + 1:
            self.poisoned = True
            raise RecoveryRequired('checkpoint configuration/history does not match')
        try:
            state = _decode(json.loads(row['payload_json']))
            if state['version'] != 1 or set(state['fields']) != set(_FIELDS):
                raise ValueError('unknown runtime state version/fields')
            output = self.core.seed(history, real_position=state['fields']['_our_fills'], adopt_position=True)
            if output.settle is None:
                raise RecoveryRequired('checkpoint G1 seed refused')
            if self.core.ledger.bars_hash != state['bars_hash'] or output.settle.hashes[-1] != state['broker_hash']:
                raise JournalCorrupt('checkpoint broker/bar identity mismatch')
            for key, value in state['fields'].items():
                setattr(self.core, key, value)
            self.core.guard._fills, self.core.guard._book_ops = state['guard']
            self.core.probe.prev_fills = defaultdict(dict, state['probe_fills'])
            self.core.probe.retracted_history = defaultdict(list, state['probe_retracts'])
            if set(state['g3']) != set(self.core.g3_windows):
                raise RecoveryRequired('checkpoint breaker configuration mismatch')
            for name, samples in state['g3'].items():
                w = self.core.g3_windows[name]
                if len(samples) > w._d.maxlen or any(type(x) is not bool for x in samples):
                    raise ValueError('invalid breaker samples')
                w._d, w._x = deque(samples, maxlen=w._d.maxlen), sum(samples)
            self.ready = True
            return output
        except BaseException:
            self.poisoned = True
            raise

    def _usable(self):
        if self.j.con.in_transaction:
            raise RecoveryRequired('core operations must own their transaction')
        if self.poisoned:
            raise RecoveryRequired('failed decision; restart before any further core operation')

    def _step(self, decision_id, phase, bar, inputs, operation):
        self._usable()
        if not self.ready or not self.has_checkpoint():
            raise RecoveryRequired('seed or restore the runtime before processing decisions')
        input_hash = T.canonical_sha256(_json([phase, bar, inputs]))
        previous = self.j._exec('SELECT * FROM core_decisions WHERE epoch_hash=? AND decision_id=?',
                                (self.epoch, decision_id)).fetchone()
        if previous is not None:
            if previous['input_hash'] != input_hash:
                self.poisoned = True
                raise JournalCorrupt('decision id reused with different inputs')
            if previous['checksum'] != T.canonical_sha256(
                    [self.epoch, decision_id, previous['input_hash'], previous['payload_json']]):
                self.poisoned = True
                raise JournalCorrupt('runtime decision checksum mismatch')
            saved = _decode(json.loads(previous['payload_json']))
            out = CoreOutput(actions=saved['actions'], incidents=[{'kind': 'duplicate_decision'}])
            return CommittedStep(out, tuple(saved['client_ids']), True)
        try:
            with self.j.transaction():
                output = operation()
                bar_index = (output.settle.bar_index if phase == 'settle' and output.settle is not None
                             else self.core.ledger.n)
                ids = self.execution.ingest(output, phase=phase, bar_index=bar_index,
                                            cycle_seq=self.core.ledger.last.cycle_seq, decision_id=decision_id)
                if phase == 'settle' and output.settle is not None:
                    self.execution.mark_settled(bar_index)
                self._save()
                payload = _json({'actions': output.actions, 'client_ids': list(ids)})
                self.j._exec('INSERT INTO core_decisions VALUES(?,?,?,?,?)',
                             (self.epoch, decision_id, input_hash, payload,
                              T.canonical_sha256([self.epoch, decision_id, input_hash, payload])))
            return CommittedStep(output, tuple(ids))
        except BaseException:
            self.poisoned = True
            raise

    def settle(self, decision_id, bar, venue_fills, in_flight, mirrored, real_position, now_ms, *, our_signed_fills=None):
        args = [venue_fills, in_flight, mirrored, real_position, now_ms, our_signed_fills]
        return self._step(decision_id, 'settle', bar, args,
                          lambda: self.core.settle(bar, venue_fills, in_flight, mirrored, real_position, now_ms,
                                                   our_signed_fills=our_signed_fills))

    def evaluate(self, decision_id, forming, now_ms):
        return self._step(decision_id, 'evaluate', forming, [now_ms],
                          lambda: self.core.evaluate(forming, now_ms))
