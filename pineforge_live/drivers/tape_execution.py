"""Offline stream/check execution replay through the actual mock Executor.

This lane measures durable dispatch and receipts. Its venue uses observed
prints, never the next ledger settlement. It is not an exchange adapter,
calibrated execution-cost lane, or admission proof.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from pineforge_live import types as T
from pineforge_live.adapters.mock import MockExecutor, SubmitFault, default_constraints
from pineforge_live.adapters.tape import TapeClock, TapeTickSource, load_feed_csv
from pineforge_live.bars import FormingBarBuilder
from pineforge_live.bars.policy import tf_ms
from pineforge_live.core.live import LiveCore
from pineforge_live.core.reconcile import DeadBand
from pineforge_live.core.riskguard import RiskLimits
from pineforge_live.drivers.checkpoint import DurableCore
from pineforge_live.epoch import RuntimeConfig
from pineforge_live.execution.coordinator import ExecutionCoordinator
from pineforge_live.execution.identity import canonical
from pineforge_live.execution.types import ExecutionSafetyError
from pineforge_live.harness import make_handle, open_journal, tape_spec, sha256_file
from pineforge_live.journal.fence import FencedLease


def local_limits():
    return RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)


async def execute_tape(*, so, feed, journal_dir, start=2000, bars=200, tf='15', mode='stream',
                       policy='path4', random_seed=1, restart_after=None, fault=None):
    """Run a fresh offline lane; optional restart keeps the mock venue alive.

    `stream` uses one renewable fenced owner. `check` acquires a fresh lease
    at each of the same recorded observation points. Both use identical
    forming-bar inputs, making their decision semantics comparable. This
    finite schedule does not model missed cron runs or concurrent sockets.
    """
    if mode not in {'stream', 'check'} or start < 1 or bars < 1:
        raise ValueError('mode must be stream/check, start and bars positive')
    if restart_after is not None and (not isinstance(restart_after, int) or restart_after < 1):
        raise ValueError('restart_after must be a positive decision count')
    target = Path(journal_dir)
    target.mkdir(parents=True, exist_ok=True)
    if (target / 'j.sqlite3').exists():
        raise ValueError('execution replay requires a fresh journal directory')
    history = load_feed_csv(feed, limit=start + bars)
    if len(history) < start + bars:
        raise ValueError('feed does not contain the requested complete window')
    width = tf_ms(tf)
    if any(b.ts_open != a.ts_open + width for a, b in zip(history, history[1:])):
        raise ValueError('execution replay feed has a noncontiguous bar')
    spec = tape_spec(tf, so=so)
    handle = make_handle(so, spec)
    initial = handle.run_full(history[:start], tf)
    if initial.status != 0:
        handle.close()
        raise ExecutionSafetyError('initial engine recompute aborted')
    clock = TapeClock(history[start].ts_open)
    venue = MockExecutor(spec.instrument, initial_position=initial.position_size, currency=spec.syminfo.currency)
    if fault:
        venue.plan(SubmitFault(timeout=fault))
    journal, marker = open_journal(target)
    limits = local_limits()
    config = RuntimeConfig(30_000, 5_000, 3_000, 2_000, asdict(limits))
    run_token = 0
    lease = None
    core = execution = durable = None
    decisions = restarts = 0
    rows = []
    error = None
    report_errors = []

    def bind():
        nonlocal core, execution, durable, lease, run_token
        core = LiveCore(handle, spec, journal, marker, config, limits, DeadBand(0.001, 0.001, 5.0), [])
        lease = FencedLease(target / 'runtime.lock', journal)
        run_token += 1
        execution = ExecutionCoordinator(journal, venue, clock, epoch_hash=spec.epoch_hash(),
                                         run_token=run_token, instrument=spec.instrument,
                                         constraints=default_constraints(),
                                         lease_check=lambda: lease.token is not None and not lease.expired(clock.now_ms()),
                                         permits=lambda order: core.stop.permits(
                                             'hard_flat' if order.cls in {'HARD_FLAT', 'FLATTEN'} else
                                             ('reduce' if order.reduce_only else 'entry'),
                                             not order.reduce_only, order.reduce_only))
        durable = DurableCore(core, execution)

    def own():
        # In the check lane the tape's next observation starts after the
        # bounded prior check lease expires; stream renews one owner.
        if lease.token is None or lease.expired(clock.now_ms()):
            token = lease.acquire(5_000 if mode == 'check' else 2 * width, clock.now_ms())
        else:
            lease.renew(clock.now_ms(), 5_000 if mode == 'check' else 2 * width)
            token = lease.token
        execution.run_token = token

    async def drain():
        snapshot = await execution.drain(deadline_ms=clock.now_ms() + config.drain_bound_ms)
        if snapshot.unresolved:
            raise ExecutionSafetyError('unresolved execution: ' + ','.join(snapshot.unresolved))
        if snapshot.terminal_residuals:
            raise ExecutionSafetyError('terminal execution residual requires reconciliation')
        return snapshot

    async def maybe_restart():
        nonlocal handle, journal, marker, restarts
        if restart_after is None or decisions != restart_after or restarts:
            return
        # A stopped process's lease must expire before another owner can
        # submit; the existing outbox keeps its original physical identities.
        saved_n = core.ledger.n
        lease.release(clock.now_ms())
        handle.close()
        journal.close()
        journal, marker = open_journal(target)
        handle = make_handle(so, spec)
        bind()
        durable.restore(history[:saved_n])
        own()
        await execution.recover()
        await drain()
        restarts += 1

    try:
        bind()
        own()
        seeded = durable.seed(history[:start], real_position=initial.position_size)
        if seeded.settle is None:
            raise ExecutionSafetyError('runtime seed refused')
        for index in range(start, start + bars):
            bar = history[index]
            builder = FormingBarBuilder(tf)
            source = TapeTickSource([bar], tf, policy=policy, seed=random_seed)
            tick_number = 0
            async for event in source.subscribe(spec.instrument, 0):
                if not isinstance(event, T.Tick):
                    raise ExecutionSafetyError('unhealed tick gap in execution replay')
                tick = event.tick
                tick_number += 1
                clock.advance_to(tick.ts)
                venue.advance(tick.price, max(tick.ts, clock.now_ms()), mark_price=tick.price)
                own()
                await execution.poll()
                builder.push(tick)
                result = durable.evaluate(f'tick:{index}:{tick_number}', builder.forming(), tick.ts)
                decisions += 1
                if result.output.probe is None or result.output.probe.aborted:
                    raise ExecutionSafetyError('evaluation aborted')
                await drain()
                await maybe_restart()
            clock.advance_to(bar.ts_open + width)
            own()
            await execution.poll()
            snapshot = execution.snapshot(index)
            if snapshot.late_receipts:
                raise ExecutionSafetyError('late ledger receipts require operator reconciliation')
            real = await venue.position(spec.instrument)
            result = durable.settle(f'bar:{index}', bar, snapshot.ledger_fills, snapshot.in_flight,
                                    snapshot.mirrored, real, clock.now_ms(),
                                    our_signed_fills=snapshot.our_signed_fills)
            decisions += 1
            output = result.output
            if output.settle is None:
                raise ExecutionSafetyError('settlement refused')
            rows.append({'bar_index': index, 'ledger_position': output.settle.position_size,
                         'venue_position_before_actions': real,
                         'classes': [f.cls.value for f in output.classified],
                         'action_receipts': len(snapshot.receipts),
                         'actions': [a.kind for a in output.actions],
                         'broker_hash': f'{output.settle.hashes[-1]:016x}'})
            await drain()
            if core.stop.level is not T.StopLevel.NONE:
                raise ExecutionSafetyError('core STOP: ' + core.stop.cause)
            await maybe_restart()
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        if core is not None:
            try:
                core.stop.raise_stop(T.StopLevel.FLAT_ONLY, T.StopDisposition.NONE,
                                     'execution_replay:' + type(exc).__name__)
            except Exception as stop_error:
                report_errors.append(f'STOP persistence: {type(stop_error).__name__}: {stop_error}')
    finally:
        def collect(label, operation, fallback):
            try:
                return operation()
            except Exception as exc:
                report_errors.append(f'{label}: {type(exc).__name__}: {exc}')
                return fallback
        try:
            request_rows = collect('requests', execution.store.requests, []) if execution else []
            receipt_rows = collect('receipts', execution.store.receipts, []) if execution else []
            snapshot = collect('snapshot', lambda: execution.snapshot(-1), None) if execution else None
            try:
                orders, _ = await venue.orders_since(spec.instrument, None)
                physical_count = len({o.venue_order_id for o in orders if o.venue_order_id})
            except Exception as exc:
                physical_count = None
                report_errors.append(f'orders: {type(exc).__name__}: {exc}')
            report = {'lane': 'offline-mock-execution', 'mode': mode, 'policy': policy,
                      'epoch_hash': spec.epoch_hash(), 'library_sha256': collect('library digest', lambda: sha256_file(so), None),
                      'feed_sha256': collect('feed digest', lambda: sha256_file(feed), None),
                      'start': start, 'requested_bars': bars,
                      'summary': {'bars': len(rows), 'decisions': decisions, 'restarts': restarts,
                                  'physical_orders': physical_count, 'receipts': len(receipt_rows),
                                  'action_receipts': sum(r['payload']['receipt_mode'] == 'ACTION_RECEIPT' for r in receipt_rows),
                                  'unresolved': len(snapshot.unresolved) if snapshot else None},
                      'error': error, 'report_errors': report_errors, 'bars': rows,
                      'orders': [r['payload']['request'] for r in request_rows if r['payload']['operation'] == 'submit'],
                      'evidence_limits': ['authored mock, no exchange conformance',
                                          'fixed account fixture, no PnL/funding calibration',
                                          'follow execution only, protective orders not submitted',
                                          'risk/protection components require an admitted live driver',
                                          'finite recorded schedule, no concurrent socket/cron claims']}
        finally:
            collect('engine close', handle.close, None)
            collect('journal close', journal.close, None)
        if report_errors and report['error'] is None:
            report['error'] = 'execution evidence could not be fully collected'
        try:
            (target / 'execution-report.json').write_text(canonical(report) + '\n')
        except OSError as exc:
            report['error'] = report['error'] or 'execution report could not be written'
            report_errors.append(f'report write: {type(exc).__name__}: {exc}')
    return report
