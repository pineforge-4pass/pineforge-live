"""Broker-neutral strategy webhooks, queue recovery and engine diagnostics."""
from __future__ import annotations
import argparse, asyncio, contextlib, sys
from pathlib import Path
from pineforge_live import __version__, ADAPTER_API_VERSION
from pineforge_live.bars.policy import BAR_POLICY_VERSION


def cmd_version(_):
    """Print `pineforge-live <version> adapter-api <n> bar-policy <policy>`."""
    print(f"pineforge-live {__version__} adapter-api {ADAPTER_API_VERSION} bar-policy {BAR_POLICY_VERSION}")
    return 0


def cmd_mock_feed(a):
    """Generate generic minute events; stdout can feed the existing run CLI."""
    import json,os,tempfile
    from pineforge_live.adapters.mock_feed import mock_events
    from pineforge_live.bars.calendar import ParentWindows
    from pineforge_live.config import read_json
    windows=ParentWindows(read_json(a.parent_windows)) if a.parent_windows else None
    events=mock_events(a.feed,mode=a.input_mode,policy=a.policy,seed=a.seed,start_seq=a.start_seq,
                       start_ms=a.start_ms,end_ms=a.end_ms,parent_windows=windows)
    def write(stream):
        for event in events:
            stream.write(json.dumps(event,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n')
    if a.output is None or str(a.output)=='-':
        write(sys.stdout)
        return 0
    target=a.output.resolve()
    if target.exists():raise ValueError('mock feed output already exists; choose a new path')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,
                                         prefix='.'+target.name+'.',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name)
            write(stream)
            stream.flush();os.fsync(stream.fileno())
        # Publish only complete validated output, without overwriting a file
        # another process created after the initial existence check.
        os.link(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return 0


def cmd_engine_info(a):
    """Print the engine .so's ABI version, `pf_version_string`, export
    coverage, and pending-order field layout."""
    from pineforge_live.engine import abi
    from pineforge_live.engine.report import pending_order_layout
    lib = abi.load_library(a.so)
    have = sum(1 for n in abi.V4_EXPORTS if getattr(lib, n, None) is not None)
    size, fields = pending_order_layout(lib)
    print(f"abi {lib.pf_abi_version()}\n"
          f"version {lib.pf_version_string().decode()}\n"
          f"exports {have}/{len(abi.V4_EXPORTS)}\n"
          f"pending_order_fields {len(fields)} ({size} bytes)")
    return 0


def cmd_journal_inspect(a):
    """Print `journal`'s per-table row counts, its last settlement, the
    non-terminal action client_ids, and the stop marker's state.
    Read-only on a v1 journal (opens the journal normally; refuses a torn
    tail) -- Journal.open() itself issues `PRAGMA journal_mode=WAL` and
    `executescript(DDL)` on every open, which are no-ops on an
    already-current v1 journal but are not literally "never writes"."""
    # NOTE: adapted to the journal API as of 1105a2c, which post-dates the
    # brief this command was drafted against:
    #  - actions has no `terminal` column any more; actions_non_terminal()
    #    derives "pending" from the latest order_states row per client_id.
    #  - hash columns (bars_hash/broker_state_hash) are stored as 16-hex TEXT
    #    and Journal's own readers (last_settlement/settlement/rows) decode
    #    them back to Python int -- nothing extra to do here.
    #  - Journal.open(path, create=False, stop_marker=...) refuses to open at
    #    all when the marker is present (or torn). This is a read-only
    #    inspection tool, so it must be able to report a set/torn marker
    #    rather than fail before it can -- open WITHOUT stop_marker, and
    #    check the marker separately purely for the printed line.
    from pineforge_live.journal import Journal, StopMarker
    from pineforge_live.journal.schema import DDL
    with contextlib.closing(Journal.open(a.journal, create=False)) as j:
        tables = [line.split("(")[0].split()[-1] for line in DDL.strip().splitlines() if line.startswith("CREATE TABLE")]
        for t in tables:
            n = j.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"{t}: {n}")
        ep = j.con.execute("SELECT epoch_hash FROM epochs ORDER BY created_ms DESC LIMIT 1").fetchone()
        print("last_settlement", j.last_settlement(ep[0]) if ep else None)
        print("non_terminal_actions", [x["client_id"] for x in j.actions_non_terminal()])
    marker = StopMarker(str(a.journal) + ".stop")
    payload = marker.read()
    if marker.exists() or (payload is not None and payload.get("unreadable")):
        # present: the sidecar's own "set" predicate (a payload with
        # `level`), or a torn/unreadable payload -- both are states
        # Journal.open(..., stop_marker=marker) would refuse to open on.
        state = "present"
    elif marker.path.exists():
        state = "armed"
    else:
        state = "absent"
    print(f"stop_marker {state}")
    return 0


def _positive_int(s: str) -> int:
    """argparse `type=` validator for `--bars`: a base-10 positive int, or
    `ArgumentTypeError` (argparse turns this into an exit-2 usage error).
    N2: argparse already prefixes the message with `argument --bars:`, so
    this message must not repeat `--bars` itself."""
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {s!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {s!r}")
    return n


def cmd_tape_smoke(a):
    """Replay `feed` (head-sliced to `--bars` bars) through the tape
    adapter into the engine at `so`, with no journal writes -- the B1
    tape -> engine end-to-end smoke test."""
    from pineforge_live.engine import EngineHandle
    from pineforge_live.adapters.tape import TapeBarSource, TapeClock, load_feed_csv
    from pineforge_live import types as T
    bars = load_feed_csv(a.feed)[: a.bars]
    if not bars:
        raise ValueError(f"tape-smoke: feed {a.feed} has no bars to replay")
    src = TapeBarSource(bars, a.tf, TapeClock(bars[0].ts_open))

    async def go():
        settled, hist, last = 0, [], None
        with EngineHandle(a.so) as h:
            h.set_broker_state_hash_recording(True)
            async for ev in src.events(T.InstrumentId("tape", T.MarketType.PERP, "tape"), a.tf):
                if isinstance(ev, T.Confirmed):
                    hist.append(ev.bar.ohlcv())
                    last = h.run_full(hist, a.tf)
                    settled += 1
                    if last.status != 0:
                        print("aborted", file=sys.stderr)
                        return 1
        print(f"settled {settled} bars, last hash {last.broker_state_hash[-1]:016x}, trades {len(last.trades)}")
        return 0

    return asyncio.run(go())


def cmd_execution_replay(a):
    """Run durable core decisions through the offline mock venue."""
    import json
    from pineforge_live.drivers.tape_execution import execute_tape
    report = asyncio.run(execute_tape(so=a.so, feed=a.feed, journal_dir=a.journal_dir,
                                      start=a.start, bars=a.bars, tf=a.tf, mode=a.mode,
                                      policy=a.policy, restart_after=a.restart_after, fault=a.fault))
    print(json.dumps({'summary': report['summary'], 'error': report['error'],
                      'report_errors': report['report_errors'],
                      'report': str(a.journal_dir / 'execution-report.json')}, sort_keys=True))
    return 1 if report['error'] else 0


def cmd_stop_clear(a):
    """Explicit operator clear, fenced against an active runtime writer."""
    import time
    from pineforge_live.journal import Journal, StopMarker
    from pineforge_live.journal.fence import FencedLease
    if not a.cause.strip():
        raise ValueError('STOP clear requires a nonempty cause')
    marker = StopMarker(a.marker or str(a.journal) + '.stop')
    with contextlib.closing(Journal.open(a.journal, create=False)) as j:
        lease = FencedLease(a.lock or a.journal.parent / 'runtime.lock', j)
        now = lambda: int(time.time() * 1000)
        lease.acquire(30_000, now())
        try:
            payload = marker.read()
            cleared = 0
            # The journal clear commits before the marker is removed.
            # An interruption at either boundary remains stopped until
            # an operator explicitly retries this command.
            with j.transaction():
                while j.append_stop_cleared(a.cause):
                    cleared += 1
                j.append_incident('operator_stop_clear', {'cause': a.cause, 'rows': cleared,
                                                         'marker': str(marker.path), 'prior_marker': payload})
            marker.clear()
            print(f'cleared {cleared} STOP rows; cause: {a.cause}')
        finally:
            lease.release(now())
    return 0


def cmd_signals(a):
    import json
    from pineforge_live.config import load_signal_config
    from pineforge_live.signals.runtime import run_signals
    config=load_signal_config(a.config)
    report=asyncio.run(run_signals(config,mode=a.signal_mode))
    print(json.dumps(report,sort_keys=True))
    return 1 if report['error'] else 0


def cmd_webhooks(a):
    import json
    from pineforge_live.config import read_json, WebhookConfig
    from pineforge_live.journal import Journal
    from pineforge_live.journal.fence import FencedLease
    from pineforge_live.signals.runtime import WallClock
    from pineforge_live.webhooks.store import Outbox
    from pineforge_live.webhooks.delivery import Dispatcher
    # Queue recovery must remain available after the strategy library or
    # original history moved. Select the stored epoch, never silently
    # initialize an empty queue from a freshly rebuilt strategy hash.
    config_path=a.config.expanduser().resolve()
    document=read_json(config_path)
    journal_path=Path(document['journal_path']).expanduser()
    if not journal_path.is_absolute():journal_path=(config_path.parent/journal_path).resolve()
    webhook=WebhookConfig(**document['webhook'])
    clock=WallClock()
    with contextlib.closing(Journal.open(journal_path,create=False)) as journal:
        has_queue=journal._exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='webhook_targets'").fetchone()
        if not has_queue:raise ValueError('journal has no initialized webhook queue')
        epochs=[r[0] for r in journal._exec('SELECT epoch_hash FROM webhook_targets ORDER BY rowid').fetchall()]
        selected=a.epoch
        if selected is None:
            if len(epochs)!=1:
                raise ValueError('select a stored --epoch: '+','.join(epochs))
            selected=epochs[0]
        if selected not in epochs:raise ValueError('requested epoch does not exist in the webhook journal')
        outbox=Outbox(journal,selected,webhook.target_url)
        if a.queue_action=='inspect':
            rows=outbox.inspect()
            print(json.dumps([{k:r[k] for k in ('sequence','event_id','state','attempts','total_attempts',
                                               'next_attempt_ms','http_status','error')}
                              for r in rows],sort_keys=True))
            return 0
        lease=FencedLease(journal_path.parent/(journal_path.name+'.lock'),journal)
        lease_ms=max(30_000,webhook.timeout_ms*3)
        lease.acquire(lease_ms,clock.now_ms())
        async def owned(timeout_ms):
            if lease.expired(clock.now_ms()):return False
            if clock.now_ms()+timeout_ms>=lease.expiry_ms:lease.renew(clock.now_ms(),lease_ms)
            return True
        try:
            if a.queue_action=='retry':outbox.retry(a.event_id,clock.now_ms())
            elif a.queue_action=='skip':outbox.skip(a.event_id,a.cause,clock.now_ms())
            report=asyncio.run(Dispatcher(outbox,webhook,clock,lease_check=owned).drain())
            from dataclasses import asdict
            print(json.dumps(asdict(report),sort_keys=True))
            return 1 if report.pending or report.failed else 0
        finally:
            if lease.token is not None:lease.release(clock.now_ms())


def main(argv=None) -> int:
    """Parse argv and dispatch to the matching `cmd_*` handler; exit codes
    are 0 ok, 2 usage (argparse), 1 error (any exception from a handler)."""
    p = argparse.ArgumentParser(
        prog="pineforge-live",
        description="Run compiled PineScript strategies and deliver broker-neutral order-action webhooks.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print the CLI/adapter-api/bar-policy version line").set_defaults(fn=cmd_version)

    s=sub.add_parser('mock-feed',help='generate direct 1m or synthetic tick JSONL from original 1m OHLCV')
    s.add_argument('feed',type=Path,help='original timestamp,open,high,low,close,volume 1m CSV')
    s.add_argument('--input-mode',choices=('ticks','bars'),default='ticks')
    s.add_argument('--policy',choices=('high-first','low-first','seeded'),default='high-first')
    s.add_argument('--seed',type=int,default=0)
    s.add_argument('--start-seq',type=_positive_int,default=1)
    s.add_argument('--start-ms',type=int,help='inclusive first minute; begin immediately after warmup history')
    s.add_argument('--end-ms',type=int,help='exclusive minute bound')
    s.add_argument('--parent-windows',type=Path,help='same price-independent calendar JSON used by the runtime')
    s.add_argument('--output',type=Path,help='new JSONL file; default or - writes stdout for piping to run')
    s.set_defaults(fn=cmd_mock_feed)

    for command,mode,help_text in (('run','stream','consume a market stream and emit strategy webhooks'),
                                  ('check','check','process one JSONL/HTTP snapshot, deliver webhooks, and exit')):
        s=sub.add_parser(command,help=help_text)
        s.add_argument('--config',type=Path,required=True)
        s.set_defaults(fn=cmd_signals,signal_mode=mode)
    for command,action in (('webhook-inspect','inspect'),('webhook-flush','flush'),
                           ('webhook-retry','retry'),('webhook-skip','skip')):
        s=sub.add_parser(command,help=f'{action} durable webhook delivery records')
        s.add_argument('--config',type=Path,required=True)
        s.add_argument('--epoch',help='stored epoch to recover when the journal contains multiple deployments')
        if action in ('retry','skip'):s.add_argument('--event-id',required=True)
        if action=='skip':s.add_argument('--cause',required=True)
        s.set_defaults(fn=cmd_webhooks,queue_action=action)

    s = sub.add_parser("engine-info", description="Print the engine .so's ABI version, build version, "
                        "export coverage, and pending-order field layout.",
                        help="print engine ABI/version/export/pending-order-field info")
    s.add_argument("so", type=Path, help="path to the engine's compiled .so")
    s.set_defaults(fn=cmd_engine_info)

    s = sub.add_parser("journal-inspect", description="Print a journal's per-table row counts, last "
                        "settlement, non-terminal actions, and stop-marker state. Read-only on a v1 "
                        "journal (opens the journal normally; refuses a torn tail).",
                        help="print a journal's table counts, last settlement, and stop-marker state")
    s.add_argument("journal", type=Path, help="path to the journal's sqlite3 file")
    s.set_defaults(fn=cmd_journal_inspect)

    s = sub.add_parser("tape-smoke", description="Replay a recorded feed through the tape adapter into "
                        "the engine and report the settled bar/hash/trade counts. No journal writes.",
                        help="replay a recorded feed through the tape adapter into the engine")
    s.add_argument("so", type=Path, help="path to the engine's compiled .so")
    s.add_argument("feed", type=Path, help="path to a timestamp,open,high,low,close,volume CSV feed")
    s.add_argument("--bars", type=_positive_int, default=500, help="head-slice the feed to this many bars (default 500)")
    s.add_argument("--tf", default="15", help="timeframe the feed's bars are bucketed at (default 15)")
    s.set_defaults(fn=cmd_tape_smoke)

    s = sub.add_parser('execution-replay', help='offline durable execution replay using the mock venue')
    s.add_argument('so', type=Path)
    s.add_argument('feed', type=Path)
    s.add_argument('--journal-dir', type=Path, required=True, help='fresh output/journal directory')
    s.add_argument('--start', type=_positive_int, default=2000)
    s.add_argument('--bars', type=_positive_int, default=200)
    s.add_argument('--tf', default='15')
    s.add_argument('--mode', choices=('stream', 'check'), default='stream')
    s.add_argument('--policy', choices=('path4', 'path4-reversed'), default='path4')
    s.add_argument('--restart-after', type=_positive_int, help='recreate runtime after this committed decision')
    s.add_argument('--fault', choices=('before_accept', 'after_accept'), help='inject one submit timeout')
    s.set_defaults(fn=cmd_execution_replay)

    s = sub.add_parser('stop-clear', help='explicitly clear STOP after taking the exclusive runtime lease')
    s.add_argument('journal', type=Path)
    s.add_argument('--cause', required=True)
    s.add_argument('--marker', type=Path, help='STOP marker path, including markers on another filesystem')
    s.add_argument('--lock', type=Path, help='runtime lock path (default: journal directory/runtime.lock)')
    s.set_defaults(fn=cmd_stop_clear)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
