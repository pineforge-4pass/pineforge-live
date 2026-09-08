"""pineforge-live CLI: version/engine-info/journal-inspect (operator
diagnostics) and tape-smoke (the B1 tape -> engine end-to-end smoke, no
journal writes). Plan B4 adds `replay`; Plan B3 adds `run`/`check`."""
from __future__ import annotations
import argparse, asyncio, contextlib, sys
from pathlib import Path
from pineforge_live import __version__, ADAPTER_API_VERSION
from pineforge_live.bars.policy import BAR_POLICY_VERSION


def cmd_version(_):
    """Print `pineforge-live <version> adapter-api <n> bar-policy <policy>`."""
    print(f"pineforge-live {__version__} adapter-api {ADAPTER_API_VERSION} bar-policy {BAR_POLICY_VERSION}")
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
    non-terminal action client_ids, and the stop marker's state -- a
    read-only diagnostic that never writes to the journal."""
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
    `ArgumentTypeError` (argparse turns this into an exit-2 usage error)."""
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--bars must be a positive integer, got {s!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"--bars must be a positive integer, got {s!r}")
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


def main(argv=None) -> int:
    """Parse argv and dispatch to the matching `cmd_*` handler; exit codes
    are 0 ok, 2 usage (argparse), 1 error (any exception from a handler)."""
    p = argparse.ArgumentParser(
        prog="pineforge-live",
        description="pineforge-live operator CLI (Plan B1): version/engine-info/"
                     "journal-inspect diagnostics, and the tape-smoke end-to-end self-test.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print the CLI/adapter-api/bar-policy version line").set_defaults(fn=cmd_version)

    s = sub.add_parser("engine-info", description="Print the engine .so's ABI version, build version, "
                        "export coverage, and pending-order field layout.",
                        help="print engine ABI/version/export/pending-order-field info")
    s.add_argument("so", type=Path, help="path to the engine's compiled .so")
    s.set_defaults(fn=cmd_engine_info)

    s = sub.add_parser("journal-inspect", description="Print a journal's per-table row counts, last "
                        "settlement, non-terminal actions, and stop-marker state. Read-only.",
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

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
