"""The backtest ledger produces alerts; webhook delivery is not a venue fill.

Default settled alerts are authoritative engine fills. Optional intrabar
alerts are provisional and receive explicit confirmation/change/retraction
updates at settlement. No account position or broker API is required.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from pineforge_live import types as T
from pineforge_live.bars.builder import bars_hash_all
from pineforge_live.bars.policy import tf_ms
from pineforge_live.core.book import book_diff
from pineforge_live.core.classify import emulated_from_settle, _side_for_leg, CLOSE_CAUSE_NAMES
from pineforge_live.core.ledger import Ledger, RecomputeAborted
from pineforge_live.core.probe import Probe
from pineforge_live.core.riskguard import StopController
from pineforge_live.journal import JournalCorrupt


class SignalRecoveryRequired(RuntimeError):
    pass


class _StagedJournal:
    """Buffer engine diagnostic/ledger writes until the decision commits.

    The C++ recompute runs without a SQLite writer lock, allowing the owning
    runtime to renew its lease. Reads still observe the validated journal;
    the caller rechecks writer authority inside the short commit transaction.
    """
    def __init__(self,journal):self.journal,self.writes=journal,[]
    def __getattr__(self,name):
        if name in ('append_bar','append_settlement','append_evaluation','append_incident'):
            def stage(*args,**kwargs):self.writes.append((name,args,kwargs))
            return stage
        return getattr(self.journal,name)
    def flush(self):
        for name,args,kwargs in self.writes:getattr(self.journal,name)(*args,**kwargs)


@dataclass(frozen=True)
class SignalResult:
    event_ids: tuple[str, ...]
    bar_index: int
    completed: bool = True
    duplicate: bool = False
    recompute_ms: float = 0


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False, default=T._canon)


class SignalEngine:
    """One serialized engine and one transactional webhook outbox.

    Recreate this object after any durable-write fault. Seed from the exact
    confirmed history requested by checkpoint_bar_index; old bars never emit
    retrospective trading alerts on startup.
    """
    def __init__(self, handle, spec, journal, marker, outbox, *, strategy_name: str,
                 mode='settled', config_hash=None, message=None, authority=None):
        if mode not in ('settled', 'intrabar'):
            raise ValueError('signal mode must be settled or intrabar')
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError('strategy_name must be nonempty')
        if journal.con.in_transaction:
            raise SignalRecoveryRequired('construct signals outside a transaction')
        if outbox.j is not journal:
            raise SignalRecoveryRequired('signal ledger and outbox must share their journal')
        self.h, self.spec, self.j, self.marker, self.outbox = handle, spec, journal, marker, outbox
        self.epoch = spec.epoch_hash()
        if outbox.epoch_hash != self.epoch:
            raise SignalRecoveryRequired('signal/outbox epoch mismatch')
        self.mode, self.strategy_name, self.message = mode, strategy_name, message
        self.authority=authority
        self.config_hash = config_hash or T.canonical_sha256([self.epoch, strategy_name, mode, message])
        self.ledger = Ledger(handle, spec, journal, self.config_hash)
        self.probe = Probe(handle, spec, self.ledger, spec.trail_refresh_policy)
        self.stop = StopController(journal, marker)
        self.stop.restore()
        self.ready = False
        self.poisoned = False
        self.signals = {}
        self.last_seq = None
        self.forming = None
        self.last_eval_ms = None
        self.forming_from_ticks=False
        self.last_tick=None
        journal.con.executescript('''
CREATE TABLE IF NOT EXISTS signal_checkpoints(
 epoch_hash TEXT PRIMARY KEY, bar_index INTEGER NOT NULL, config_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, checksum TEXT NOT NULL);
''')
        journal.append_epoch(self.epoch, _canonical(asdict(spec)))
        journal.append_runtime_config(self.config_hash, _canonical({'strategy_name':strategy_name,'mode':mode,'message':message}))

    def _check(self):
        if self.j.con.in_transaction:
            raise SignalRecoveryRequired('signal operation must own its transaction')
        if self.poisoned:
            raise SignalRecoveryRequired('failed signal decision; restart from checkpoint')
        if self.stop.level is not T.StopLevel.NONE or self.marker.exists():
            raise SignalRecoveryRequired('STOP is active; explicit operator recovery required')

    def _row(self):
        row = self.j._exec('SELECT * FROM signal_checkpoints WHERE epoch_hash=?', (self.epoch,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        domain = [row['epoch_hash'], row['bar_index'], row['config_hash'], row['payload_json']]
        if row['checksum'] != T.canonical_sha256(domain):
            self.poisoned = True
            raise JournalCorrupt('signal checkpoint checksum mismatch')
        if row['config_hash'] != self.config_hash:
            self.poisoned = True
            raise SignalRecoveryRequired('signal checkpoint configuration changed')
        return row

    @property
    def checkpoint_bar_index(self):
        row = self._row()
        return row['bar_index'] if row else None

    def _save(self):
        payload = _canonical({'version':1,'signals':self.signals,'last_seq':self.last_seq,
                              'forming':asdict(self.forming) if self.forming else None,
                              'last_eval_ms':self.last_eval_ms,
                              'forming_from_ticks':self.forming_from_ticks,
                              'last_tick':asdict(self.last_tick) if self.last_tick else None,
                              'bars_hash':self.ledger.bars_hash,'broker_hash':self.ledger.last.hashes[-1]})
        fields = [self.epoch, self.ledger.n - 1, self.config_hash, payload]
        self.j._exec('INSERT OR REPLACE INTO signal_checkpoints VALUES(?,?,?,?,?)',
                     (*fields, T.canonical_sha256(fields)))

    def _authorize_commit(self):
        if self.authority is not None and self.authority() is False:
            raise SignalRecoveryRequired('writer lease lost before signal commit')

    def _horizon(self):
        if self.ledger.n >= self.spec.horizon_bars:
            self._authorize_commit()
            self.stop.raise_stop(T.StopLevel.FLAT_ONLY,T.StopDisposition.NONE,'horizon')
            raise SignalRecoveryRequired('epoch horizon exhausted')

    def _fail(self, exc):
        self.poisoned = True
        try:
            if self.authority is not None and self.authority() is False:
                return
            self.stop.raise_stop(T.StopLevel.HARD, T.StopDisposition.HOLD,
                                 'signal:' + type(exc).__name__)
        except Exception:
            # The original journal/engine error remains primary; the STOP
            # controller also keeps its in-memory state on marker failure.
            pass

    def seed(self, history):
        self._check()
        if self.ready:
            raise SignalRecoveryRequired('seed once per runtime')
        if not history:
            raise ValueError('confirmed warmup history is required')
        if history[0].ts_open != self.spec.history_start_ms:
            raise ValueError('history must begin at epoch.history_start_ms')
        width = tf_ms(self.spec.script_tf)
        if any(b.is_forming for b in history) or any(b.ts_open != a.ts_open + width for a,b in zip(history,history[1:])):
            raise ValueError('warmup history must contain contiguous confirmed script bars')
        row = self._row()
        if row is None and self.j.last_settlement(self.epoch) is not None:
            raise SignalRecoveryRequired('journal has settlements but no signal checkpoint')
        if row is not None and len(history) != row['bar_index'] + 1:
            raise SignalRecoveryRequired('supply exactly the checkpoint history before catch-up')
        try:
            staged=_StagedJournal(self.j)
            self.ledger.j=staged
            try:result=self.ledger.seed(history)
            finally:self.ledger.j=self.j
            with self.j.transaction():
                self._authorize_commit()
                staged.flush()
                if row:
                    state = json.loads(row['payload_json'])
                    if state['version'] != 1 or state['bars_hash'] != bars_hash_all(history) or state['broker_hash'] != result.hashes[-1]:
                        raise JournalCorrupt('signal checkpoint engine/history mismatch')
                    self.signals = state['signals']
                    self.last_seq = state['last_seq']
                    self.forming = T.NormalizedBar(**state['forming']) if state['forming'] else None
                    self.last_eval_ms = state['last_eval_ms']
                    self.forming_from_ticks=state.get('forming_from_ticks',False)
                    tick=state.get('last_tick')
                    if tick:
                        tick['side']=T.Side(tick['side']) if tick.get('side') else None
                    self.last_tick=T.NormalizedTick(**tick) if tick else None
                else:
                    self._save()
            self.ready = True
            return SignalResult((), result.bar_index, recompute_ms=result.recompute_ms)
        except BaseException as exc:
            self._fail(exc)
            raise

    def _identity(self, fill, ordinal):
        return _canonical([fill.bar_index, fill.intent, fill.leg, fill.is_long, ordinal])

    def _order(self, fill, *, path_variant=False):
        if not math.isfinite(fill.qty) or fill.qty <= 0 or not math.isfinite(fill.price):
            raise ValueError('engine returned invalid signal quantity or price')
        cause = getattr(fill,'close_cause',0)
        return {'id':None if fill.intent in ('?', '') else fill.intent,
                'action':_side_for_leg(fill.is_long,fill.leg).value.lower(),
                'contracts':fill.qty,'price':fill.price,'leg':fill.leg.lower(),
                'reduce_only':fill.leg == 'EXIT',
                'close_cause':CLOSE_CAUSE_NAMES[cause] if 0 <= cause < len(CLOSE_CAUSE_NAMES) else 'UNKNOWN',
                'path_variant':bool(path_variant),'identity_resolved':fill.intent not in ('?', '')}

    def _emit(self, slot, order, bar, now_ms, status, *, original=None, version=0):
        event = 'order_update' if original else 'order_action'
        event_id = T.canonical_sha256([self.epoch, slot, event, version, status])
        payload = {'schema_version':1,'event':event,'event_id':event_id,'status':status,
                   'strategy':{'name':self.strategy_name,'epoch':self.epoch},
                   'instrument':{'venue':self.spec.instrument.venue,'market_type':self.spec.instrument.market_type.value,
                                 'symbol':self.spec.instrument.symbol,'ticker':self.spec.syminfo.tickerid},
                   'timeframe':self.spec.script_tf,'timestamp':now_ms,
                   'bar':{'index':json.loads(slot)[0],'time':bar.ts_open,'confirmed':not bar.is_forming,
                          'open':bar.o,'high':bar.h,'low':bar.l,'close':bar.c,'volume':bar.v},
                   'order':order,'message':self.message}
        if original:
            payload['original_event_id'] = original
        self.outbox.enqueue(event_id,payload,now_ms)
        return event_id

    def settle(self, bar, now_ms):
        self._check()
        if not self.ready:
            raise SignalRecoveryRequired('seed first')
        self._horizon()
        previous = self.ledger.last
        try:
            staged=_StagedJournal(self.j)
            self.ledger.j=staged
            try:result=self.ledger.settle(bar,now_ms)
            finally:self.ledger.j=self.j
            with self.j.transaction():
                self._authorize_commit()
                if result is previous:
                    return SignalResult((),result.bar_index,duplicate=True)
                staged.flush()
                fills = emulated_from_settle(result,book_diff(previous.book,result.book),previous.book)
                counts, current, emitted = {}, {}, []
                for fill in fills:
                    key = (fill.intent,fill.leg,fill.is_long)
                    ordinal = counts.get(key,0);counts[key]=ordinal+1
                    slot = self._identity(fill,ordinal)
                    order = self._order(fill)
                    old = self.signals.get(slot)
                    if old:
                        # Confirmation never sends another buy/sell action.
                        comparable = {k:v for k,v in old['order'].items() if k not in ('path_variant','close_cause')}
                        settled = {k:v for k,v in order.items() if k not in ('path_variant','close_cause')}
                        status = 'confirmed' if comparable == settled and old['status'] != 'retracted' else 'changed'
                        event_id = self._emit(slot,order,bar,now_ms,status,original=old['event_id'],version=old['version']+1)
                    else:
                        event_id = self._emit(slot,order,bar,now_ms,'confirmed')
                    emitted.append(event_id);current[slot]=True
                for slot,old in self.signals.items():
                    if json.loads(slot)[0] == result.bar_index and slot not in current and old['status'] != 'retracted':
                        emitted.append(self._emit(slot,old['order'],bar,now_ms,'retracted',original=old['event_id'],version=old['version']+1))
                self.signals = {k:v for k,v in self.signals.items() if json.loads(k)[0] > result.bar_index}
                if self.forming and self.forming.ts_open <= bar.ts_open:
                    self.forming=None
                    self.forming_from_ticks=False
                self._save()
            return SignalResult(tuple(emitted),result.bar_index,recompute_ms=result.recompute_ms)
        except RecomputeAborted:
            return SignalResult((),self.ledger.n,completed=False)
        except BaseException as exc:
            self._fail(exc)
            raise

    def evaluate(self, forming, now_ms, *, last_seq=None, from_ticks=False, last_tick=None):
        self._check()
        if not self.ready:
            raise SignalRecoveryRequired('seed first')
        self._horizon()
        width=tf_ms(self.spec.script_tf)
        if not forming.is_forming or forming.ts_open != self.ledger.last.bar.ts_open + width:
            raise ValueError('forming bar must immediately follow the confirmed ledger')
        try:
            staged=_StagedJournal(self.j)
            pr=self.probe.evaluate(forming,now_ms,journal=staged) if self.mode == 'intrabar' else None
            with self.j.transaction():
                self._authorize_commit()
                staged.flush()
                if pr is not None and pr.aborted:
                    return SignalResult((),self.ledger.n,completed=False)
                emitted=[]
                if pr is not None:
                    counts,current={},set()
                    for f in pr.fills:
                        key=(f.intent,f.leg,f.is_long);ordinal=counts.get(key,0);counts[key]=ordinal+1
                        # Probe and settlement share the same slot identity.
                        fill=type('SignalFill',(),{'intent':f.intent,'leg':f.leg,'is_long':f.is_long,
                                                 'bar_index':pr.bar_index,'qty':f.qty,'price':f.price})()
                        slot=self._identity(fill,ordinal);current.add(slot)
                        order=self._order(fill,path_variant=f.path_variant)
                        old=self.signals.get(slot)
                        if old is None:
                            eid=self._emit(slot,order,forming,now_ms,'provisional')
                            self.signals[slot]={'event_id':eid,'order':order,'version':0,'status':'provisional'}
                            emitted.append(eid)
                        elif old['order'] != order or old['status'] == 'retracted':
                            old['version']+=1
                            emitted.append(self._emit(slot,order,forming,now_ms,'changed',original=old['event_id'],version=old['version']))
                            old['order'],old['status']=order,'provisional'
                    for slot,old in self.signals.items():
                        if json.loads(slot)[0] == pr.bar_index and slot not in current and old['status'] != 'retracted':
                            old['version']+=1
                            emitted.append(self._emit(slot,old['order'],forming,now_ms,'retracted',original=old['event_id'],version=old['version']))
                            old['status']='retracted'
                self.forming=forming
                self.forming_from_ticks=from_ticks
                if last_tick is not None:self.last_tick=last_tick
                if last_seq is not None:
                    self.last_seq=last_seq
                self.last_eval_ms=now_ms
                self._save()
            return SignalResult(tuple(emitted),self.ledger.n,recompute_ms=pr.recompute_ms if pr else 0)
        except BaseException as exc:
            self._fail(exc)
            raise

    def observe(self, forming, now_ms, *, last_seq=None, from_ticks=False, last_tick=None):
        """Persist a coalesced forming snapshot without generating alerts."""
        self._check()
        self._horizon()
        if not self.ready or forming.ts_open != self.ledger.last.bar.ts_open + tf_ms(self.spec.script_tf):
            raise ValueError('observation must follow the confirmed ledger')
        try:
            with self.j.transaction():
                self._authorize_commit()
                self.j.append_evaluation({'epoch_hash':self.epoch,'trigger':'coalesced',
                                         'tick_seq_from':self.last_seq,'tick_seq_to':last_seq,
                                         'forming_json':_canonical(asdict(forming)),
                                         'outcome':'dropped','recompute_ms':0,'created_ms':now_ms})
                self.forming=forming
                self.forming_from_ticks=from_ticks
                if last_tick is not None:self.last_tick=last_tick
                if last_seq is not None:self.last_seq=last_seq
                self._save()
            return SignalResult((),self.ledger.n)
        except BaseException as exc:
            self._fail(exc)
            raise
