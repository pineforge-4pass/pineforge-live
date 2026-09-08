"""pineforge-live CLI: version/engine-info/journal-inspect (operator
diagnostics) and tape-smoke (the B1 tape -> engine end-to-end smoke, no
journal writes). Plan B4 adds `replay`; Plan B3 adds `run`/`check`."""
from __future__ import annotations
import argparse, asyncio, sys
from pathlib import Path
from pineforge_live import __version__, ADAPTER_API_VERSION
from pineforge_live.bars.policy import BAR_POLICY_VERSION


def cmd_version(_):
    print(f"pineforge-live {__version__} adapter-api {ADAPTER_API_VERSION} bar-policy {BAR_POLICY_VERSION}")
    return 0


def cmd_engine_info(a):
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
    j = Journal.open(a.journal, create=False)
    tables = [line.split("(")[0].split()[-1] for line in DDL.strip().splitlines() if line.startswith("CREATE TABLE")]
    for t in tables:
        n = j.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n}")
    ep = j.con.execute("SELECT epoch_hash FROM epochs ORDER BY created_ms DESC LIMIT 1").fetchone()
    print("last_settlement", j.last_settlement(ep[0]) if ep else None)
    print("non_terminal_actions", [x["client_id"] for x in j.actions_non_terminal()])
    marker = StopMarker(str(a.journal) + ".stop")
    payload = marker.read()
    if payload is None:
        state = "armed" if marker.path.exists() else "absent"
    else:
        # A parsed (set) payload and a torn/unreadable one are both states
        # Journal.open(..., stop_marker=marker) would refuse to open on --
        # both are "present" from an operator's point of view.
        state = "present"
    print(f"stop_marker {state}")
    j.close()
    return 0


def cmd_tape_smoke(a):
    from pineforge_live.engine import EngineHandle
    from pineforge_live.adapters.tape import TapeBarSource, TapeClock, load_feed_csv
    from pineforge_live import types as T
    bars = load_feed_csv(a.feed)[: a.bars]
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
    p = argparse.ArgumentParser(prog="pineforge-live")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version").set_defaults(fn=cmd_version)

    s = sub.add_parser("engine-info")
    s.add_argument("so", type=Path)
    s.set_defaults(fn=cmd_engine_info)

    s = sub.add_parser("journal-inspect")
    s.add_argument("journal", type=Path)
    s.set_defaults(fn=cmd_journal_inspect)

    s = sub.add_parser("tape-smoke")
    s.add_argument("so", type=Path)
    s.add_argument("feed", type=Path)
    s.add_argument("--bars", type=int, default=500)
    s.add_argument("--tf", default="15")
    s.set_defaults(fn=cmd_tape_smoke)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
