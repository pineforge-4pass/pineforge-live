"""Stream and scheduled checks for broker-neutral order-action webhooks."""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import time
from dataclasses import replace

from pineforge_live import types as T
from pineforge_live.bars import FormingBarBuilder
from pineforge_live.bars.builder import compare_bar
from pineforge_live.bars.minute_stream import MinuteStream
from pineforge_live.bars.calendar import ParentWindows
from pineforge_live.bars.policy import tf_ms
from pineforge_live.engine import EngineHandle
from pineforge_live.epoch import apply_epoch
from pineforge_live.journal import Journal, StopMarker, JournalFault
from pineforge_live.journal.fence import FencedLease, LeaseLost
from pineforge_live.signals.engine import SignalEngine, SignalRecoveryRequired
from pineforge_live.sources import create_source, JsonlSource, SourceError
from pineforge_live.sources.http import HttpSource
from pineforge_live.webhooks.store import Outbox


class WallClock:
    def now_ms(self):return time.time_ns()//1_000_000
    async def sleep_until(self,ms):await asyncio.sleep(max(0,(ms-self.now_ms())/1000))


class SignalWorker:
    """All engine access and its SQLite connection live on one worker thread."""
    def __init__(self,config,lease=None,clock=None):
        self.config,self.lease,self.clock=config,lease,clock
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='pineforge-signals')
        self.engine=None
        self.busy_evaluate=False

    async def _call(self,fn,*args,**kwargs):
        loop=asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool,lambda:fn(*args,**kwargs))

    def _open(self):
        c=self.config
        from pineforge_live.config import file_sha256
        if file_sha256(c.strategy_path)!=c.epoch.code_identity.build_receipt['library_sha256']:
            raise SignalRecoveryRequired('strategy library changed after configuration was loaded')
        marker=StopMarker(str(c.journal_path)+'.stop')
        journal=Journal.open(c.journal_path,stop_marker=marker)
        handle=None
        try:
            handle=EngineHandle(c.strategy_path)
            if c.auxiliary_history_path is not None:
                handle.set_auxiliary_history(c.auxiliary_history_path,c.epoch.auxiliary_history_sha256,
                                             start_ms=c.epoch.history_start_ms)
            apply_epoch(handle,c.epoch)
            outbox=Outbox(journal,c.epoch.epoch_hash(),c.webhook.target_url)
            def authority():
                if self.lease is None:return True
                token=self.lease.token
                if token is None:return False
                row=journal.live_check(self.clock.now_ms())
                return row is not None and row['fencing_token']==token and journal.max_fencing_token()==token
            self.engine=SignalEngine(handle,c.epoch,journal,marker,outbox,strategy_name=c.strategy_name,
                                     mode=c.trigger_mode,config_hash=c.config_hash,
                                     message=getattr(c,'message',None),authority=authority)
            checkpoint=self.engine.checkpoint_bar_index
            history=list(c.history)
            if checkpoint is not None:
                by_time={b.ts_open:b for b in history}
                for row in journal.rows('bars','epoch_hash=?',(c.epoch.epoch_hash(),)):
                    b=T.NormalizedBar(row['ts_open'],row['o'],row['h'],row['l'],row['c'],row['v'],
                                      row['trade_count'] or 0,False,bool(row['synthesized']))
                    if b.ts_open in by_time and compare_bar(by_time[b.ts_open],b):
                        raise SignalRecoveryRequired('configured history conflicts with journal')
                    by_time[b.ts_open]=b
                history=sorted(by_time.values(),key=lambda b:b.ts_open)[:checkpoint+1]
            self.engine.seed(history)
            return self._context()
        except BaseException:
            if handle:handle.close()
            journal.close()
            raise

    def _context(self):
        e=self.engine
        return {'bar':e.ledger.last.bar,'bar_index':e.ledger.n-1,'forming':e.forming,
                'last_seq':e.last_seq,'last_eval_ms':e.last_eval_ms,
                'forming_from_ticks':e.forming_from_ticks,'last_tick':e.last_tick,
                'input_state':e.input_state,'last_input_minute':e.last_input_minute}

    async def open(self):return await self._call(self._open)
    async def context(self):return await self._call(self._context)
    async def settle(self,bar,now_ms,**input_kwargs):return await self._call(self.engine.settle,bar,now_ms,**input_kwargs)
    async def evaluate(self,bar,now_ms,last_seq=None,*,from_ticks=False,last_tick=None,**input_kwargs):
        self.busy_evaluate=True
        try:return await self._call(self.engine.evaluate,bar,now_ms,last_seq=last_seq,from_ticks=from_ticks,last_tick=last_tick,**input_kwargs)
        finally:self.busy_evaluate=False
    async def observe(self,bar,now_ms,last_seq=None,*,from_ticks=False,last_tick=None,**input_kwargs):
        return await self._call(self.engine.observe,bar,now_ms,last_seq=last_seq,from_ticks=from_ticks,last_tick=last_tick,**input_kwargs)
    def abort_probe(self):
        if self.busy_evaluate and self.engine is not None:self.engine.h.request_abort()
    async def halt(self,cause):
        def owned_stop():
            if self.engine and (self.engine.authority is None or self.engine.authority()):
                self.engine.stop.raise_stop(T.StopLevel.FLAT_ONLY,T.StopDisposition.NONE,cause)
        await self._call(owned_stop)
    async def close(self):
        def close():
            if self.engine:
                self.engine.h.close();self.engine.j.close();self.engine=None
        try:await self._call(close)
        finally:self.pool.shutdown(wait=True,cancel_futures=True)


async def run_signals(config, *, mode=None, source=None, transport=None, clock=None, max_events=None):
    """Always replace the run report, including startup/STOP refusals."""
    try:
        return await _run_signals(config,mode=mode,source=source,transport=transport,clock=clock,max_events=max_events)
    except Exception as exc:
        report={'mode':mode or config.mode,'strategy':config.strategy_name,'epoch':config.epoch.epoch_hash(),
                'source_events':0,'settled_bars':0,'evaluations':0,'coalesced':0,'duplicate_ticks':0,
                'emitted':0,'delivered':0,'pending':None,'failed':None,'phase':'startup',
                'error':f'{type(exc).__name__}: {exc}'}
        with contextlib.suppress(OSError):
            config.journal_path.with_suffix('.report.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
        return report


async def _run_signals(config, *, mode=None, source=None, transport=None, clock=None, max_events=None):
    """Own a fenced runtime and deliver alerts to the user-configured endpoint.

    check consumes one finite JSONL/HTTP snapshot and exits; run consumes the
    continuous source. Both restore the same checkpoint and use the same
    transactional engine/outbox. No broker account or exchange key is read.
    """
    from pineforge_live.webhooks.delivery import Dispatcher
    mode=mode or config.mode
    if mode not in ('stream','check'):raise ValueError('mode must be stream or check')
    input_tf=getattr(config,'input_tf',None) or config.script_tf
    minute_mode=input_tf=='1' and tf_ms(config.script_tf)>60_000
    windows=getattr(config,'parent_windows',None)
    calendar=ParentWindows(windows) if windows is not None else None
    if windows!=getattr(config.epoch,'parent_windows',None):
        raise SourceError('runtime calendar does not match epoch identity')
    source=source or create_source(config.source,config.script_tf,input_tf=input_tf)
    if mode=='check' and not isinstance(source,(HttpSource,JsonlSource)) and not hasattr(source,'snapshot'):
        raise ValueError('check requires a finite JSONL or HTTP snapshot source')
    config.journal_path.parent.mkdir(parents=True,exist_ok=True)
    marker=StopMarker(str(config.journal_path)+'.stop');marker.prepare()
    clock=clock or WallClock()
    journal=Journal.open(config.journal_path,stop_marker=marker)
    lease=FencedLease(config.journal_path.parent/(config.journal_path.name+'.lock'),journal)
    lease_ms=max(30_000,config.webhook.timeout_ms*3,config.source.timeout_ms*3)
    worker=SignalWorker(config,lease,clock)
    heartbeat_task=producer_task=None
    status={'mode':mode,'strategy':config.strategy_name,'epoch':config.epoch.epoch_hash(),
            'source_events':0,'settled_bars':0,'evaluations':0,'coalesced':0,'duplicate_ticks':0,
            'emitted':0,'delivered':0,'pending':0,'failed':0,'error':None}
    lost=asyncio.Event()
    queue=asyncio.Queue(maxsize=4096)
    stop_requested=asyncio.Event()
    report_path=config.journal_path.with_suffix('.report.json')
    try:
        lease.acquire(lease_ms,clock.now_ms())
        outbox=Outbox(journal,config.epoch.epoch_hash(),config.webhook.target_url)
        async def fence(timeout_ms):
            if lost.is_set() or lease.token is None or lease.expiry_ms is None or clock.now_ms()+timeout_ms>=lease.expiry_ms:
                raise LeaseLost('lease does not cover webhook timeout')
            return True
        dispatcher=Dispatcher(outbox,config.webhook,clock,transport=transport,lease_check=fence)
        async def heartbeat():
            try:
                while not stop_requested.is_set():
                    await asyncio.sleep(lease_ms/3000)
                    if stop_requested.is_set():break
                    while True:
                        try:
                            lease.renew(clock.now_ms(),lease_ms)
                            break
                        except JournalFault as exc:
                            # The engine's atomic decision can briefly hold
                            # SQLite's writer lock. Busy is not lease loss
                            # while our durable lease remains live.
                            if ('locked' not in str(exc).lower() and 'busy' not in str(exc).lower()) or lease.expired(clock.now_ms()):
                                raise
                            await asyncio.sleep(.05)
                            if stop_requested.is_set():return
            except BaseException:
                if not stop_requested.is_set():
                    lost.set();worker.abort_probe()
                raise
        heartbeat_task=asyncio.create_task(heartbeat())
        context=await worker.open()
        builder=FormingBarBuilder(config.script_tf)
        builder._cur=context['forming']
        minute_stream=None
        if minute_mode:
            minute_stream=(MinuteStream.from_state(context['input_state'],parent_windows=calendar) if context.get('input_state')
                           else MinuteStream(config.script_tf,mode=getattr(config,'input_mode','mixed'),parent_windows=calendar,
                                             gap_policy=config.input_gap_policy))
            if minute_stream.mode != getattr(config,'input_mode','mixed'):
                raise SourceError('minute input checkpoint mode mismatch')
            if minute_stream.aggregator.gap_policy!=config.input_gap_policy:
                raise SourceError('minute input checkpoint gap policy mismatch')
            if context['forming'] is not None and context.get('input_state') is None:
                raise SourceError('minute runtime forming state has no aggregation checkpoint')
        last_seq=context['last_seq']
        last_tick=context['last_tick']
        last_bar=context['bar']
        last_eval=context['last_eval_ms']
        width=tf_ms(config.script_tf)
        min_eval_ms=1000/max(1,getattr(config,'max_eval_rate',5))
        previous_hlc=None
        builder_from_ticks=context['forming_from_ticks']
        confirmation_pending=set()

        def next_open():
            try:return calendar.next_open(last_bar.ts_open) if calendar else last_bar.ts_open+width
            except ValueError as exc:raise SourceError(str(exc)) from None

        async def deliver():
            report=await dispatcher.drain()
            status['delivered']+=report.delivered
            status['pending'],status['failed']=report.pending,report.failed
            if report.failed:raise RuntimeError('webhook delivery failed; inspect and retry the retained event')

        await deliver()  # Retry committed prior-run events before reading new input.

        async def produce():
            try:
                if mode=='check' and hasattr(source,'snapshot'):
                    items=await source.snapshot(None)
                    for event in items:
                        if isinstance(event,T.Confirmed) and not minute_mode:confirmation_pending.add(event.bar.ts_open)
                        await queue.put(event)
                else:
                    async for event in source.events(from_seq=None):
                        if isinstance(event,T.Confirmed) and not minute_mode:
                            confirmation_pending.add(event.bar.ts_open)
                            worker.abort_probe()
                        await queue.put(event)
                await queue.put(None)
            except BaseException as exc:
                if isinstance(exc,asyncio.CancelledError):raise
                await queue.put(exc)
        producer_task=asyncio.create_task(produce())

        async def settle(bar,ts,**input_kwargs):
            nonlocal last_bar,last_eval,previous_hlc
            if bar.ts_open<last_bar.ts_open:
                # Historical snapshots may replay bars; verify every row we
                # have instead of silently accepting an edited old candle.
                rows=journal.rows('bars','epoch_hash=? AND ts_open=?',(config.epoch.epoch_hash(),bar.ts_open))
                configured=next((b for b in config.history if b.ts_open==bar.ts_open),None)
                if rows:
                    r=rows[0];old=T.NormalizedBar(r['ts_open'],r['o'],r['h'],r['l'],r['c'],r['v'],r['trade_count'] or 0)
                else:old=configured
                if old is None or compare_bar(old,bar):raise SourceError('historical confirmed bar changed')
                return
            if bar.ts_open==last_bar.ts_open:
                if compare_bar(last_bar,bar):raise SourceError('confirmed bar changed after settlement')
                return
            result=await worker.settle(bar,ts,**input_kwargs)
            if not result.completed:
                # A settle owns priority; an engine abort has not consumed
                # the bar or its event IDs, so this exact bar can retry.
                result=await worker.settle(bar,ts,**input_kwargs)
            if not result.completed:raise RuntimeError('settlement recompute repeatedly aborted')
            status['emitted']+=len(result.event_ids);status['settled_bars']+=not result.duplicate
            last_bar=bar;last_eval=None;previous_hlc=None
            await deliver()

        while True:
            if lost.is_set():raise LeaseLost('runtime lease lost')
            try:
                event=await asyncio.wait_for(queue.get(),timeout=min(5,lease_ms/4000))
            except asyncio.TimeoutError:
                await deliver()
                if producer_task.done():break
                continue
            if event is None:break
            if isinstance(event,BaseException):raise event
            status['source_events']+=1
            if isinstance(event,T.TickGap):
                if last_seq is not None and event.to_seq<=last_seq:continue
                if not event.healed:raise SourceError('unhealed tick gap; resume from complete history')
                # A healed gap is only a notice; all missing ticks must
                # still be delivered in order before a newer sequence.
                continue
            input_kwargs={}
            if isinstance(event,T.Tick):
                t=event.tick
                if last_seq is not None and t.seq<=last_seq:
                    if t.seq==last_seq and last_tick is not None and t!=last_tick:
                        raise SourceError('conflicting duplicate tick after restart')
                    status['duplicate_ticks']+=1;continue
                if last_seq is not None and t.seq!=last_seq+1:raise SourceError('tick sequence gap')
                if t.ts<next_open():raise SourceError('new tick precedes settled history')
                if last_tick is not None and t.ts<last_tick.ts:raise SourceError('tick timestamp regressed')
                if minute_mode:
                    try:updates=minute_stream.push(event)
                    except ValueError as exc:raise SourceError(str(exc)) from None
                    forming=updates[-1].bar
                    input_kwargs={'input_state':minute_stream.export_state(compact=True)}
                    builder_from_ticks=True
                else:
                    if builder.forming() is None:builder_from_ticks=True
                    closed_bars=builder.push(t)
                    for closed in closed_bars:await settle(closed,closed.ts_open+width)
                    if closed_bars:builder_from_ticks=True
                    forming=builder.forming()
                last_seq=t.seq;last_tick=t;ts=t.ts
            elif isinstance(event,T.Confirmed) and minute_mode:
                bar=event.bar
                saved=journal._exec('SELECT payload_json,checksum FROM signal_input_minutes WHERE epoch_hash=? AND ts_open=?',
                                    (config.epoch.epoch_hash(),bar.ts_open)).fetchone()
                if saved is not None:
                    domain=[config.epoch.epoch_hash(),bar.ts_open,saved['payload_json']]
                    if saved['checksum'] != T.canonical_sha256(domain):
                        raise SourceError('input minute identity checksum mismatch')
                    if T.NormalizedBar(**json.loads(saved['payload_json'])) != bar:
                        raise SourceError('historical input minute changed')
                    if max_events and status['source_events']>=max_events:break
                    continue
                if bar.ts_open<next_open():
                    raise SourceError('input minute precedes settled history without a recorded identity')
                try:updates=minute_stream.push(event)
                except ValueError as exc:raise SourceError(str(exc)) from None
                input_kwargs={'input_state':minute_stream.export_state(compact=True),'input_minute':bar}
                ts=bar.ts_open+60_000
                if updates and isinstance(updates[-1],T.Confirmed):
                    for update in updates:await settle(update.bar,ts,**input_kwargs)
                    if max_events and status['source_events']>=max_events:break
                    continue
                if not updates:
                    raise SourceError('input minute replay has no durable identity')
                forming=updates[-1].bar
                builder_from_ticks=False
            elif isinstance(event,T.Forming):
                if minute_mode:raise SourceError('input_tf=1 accepts confirmed minute bars, not forming snapshots')
                forming=event.bar
                if forming.ts_open<next_open():continue
                if forming.ts_open!=next_open():raise SourceError('forming snapshot skipped confirmed bars')
                builder._cur=forming;builder_from_ticks=False;ts=max(clock.now_ms(),forming.ts_open)
            elif isinstance(event,T.Confirmed):
                bar=event.bar
                if builder.forming() and builder.forming().ts_open==bar.ts_open:
                    if builder_from_ticks and compare_bar(replace(builder.forming(),is_forming=False),bar):
                        raise SourceError('tick-built bar disagrees with confirmed bar')
                    builder._cur=None
                confirmation_pending.discard(bar.ts_open)
                await settle(bar,bar.ts_open+width)
                if max_events and status['source_events']>=max_events:break
                continue
            else:raise SourceError('unsupported source event')
            if forming.ts_open!=next_open():raise SourceError('input does not continue confirmed history')
            hlc=(forming.o,forming.h,forming.l,forming.c,forming.v,forming.trade_count)
            if forming.ts_open not in confirmation_pending and (last_eval is None or (hlc!=previous_hlc and ts-last_eval>=min_eval_ms)):
                result=await worker.evaluate(forming,ts,last_seq,from_ticks=builder_from_ticks,last_tick=last_tick,**input_kwargs)
                if result.completed:
                    status['evaluations']+=1;status['emitted']+=len(result.event_ids)
                    last_eval=ts;previous_hlc=hlc
                    await deliver()
                else:
                    await worker.observe(forming,ts,last_seq,from_ticks=builder_from_ticks,last_tick=last_tick,**input_kwargs)
            else:
                await worker.observe(forming,ts,last_seq,from_ticks=builder_from_ticks,last_tick=last_tick,**input_kwargs);status['coalesced']+=1
            if max_events and status['source_events']>=max_events:break
        # Finish the current delivery budget even for a finite check. A
        # failed head event remains a queue barrier; no later action jumps it.
        await deliver()
        status['pending']=len([r for r in outbox.inspect() if r['state'] not in ('DELIVERED','SKIPPED')])
        if status['pending']:status['error']='webhooks remain pending; next run retries the same event IDs'
    except asyncio.CancelledError:
        status['error']='runtime interrupted; committed webhooks retained'
        raise
    except Exception as exc:
        status['error']=f'{type(exc).__name__}: {exc}'
        # Delivery unavailability does not alter the backtest ledger. Feed,
        # engine and integrity faults retain a STOP for explicit recovery.
        if isinstance(exc,(SourceError,SignalRecoveryRequired)):
            with contextlib.suppress(Exception):await worker.halt(type(exc).__name__)
    finally:
        stop_requested.set()
        for task in (producer_task,heartbeat_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError,Exception):await task
        with contextlib.suppress(Exception):await worker.close()
        if lease.token is not None:
            with contextlib.suppress(Exception):lease.release(clock.now_ms())
        journal.close()
        try:report_path.write_text(json.dumps(status,sort_keys=True,indent=2)+'\n')
        except OSError:status['error']=status['error'] or 'runtime report write failed'
    return status
