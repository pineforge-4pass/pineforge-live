import csv, threading, time
from pathlib import Path
import pytest
from pineforge_live.engine import abi
from pineforge_live.engine.handle import EngineHandle

PKG = Path(__file__).resolve().parents[1] / "pineforge_live"

def test_g0_no_stream_symbols():
    offenders = [p for p in PKG.rglob("*.py") if "strategy_stream" in p.read_text()]
    assert offenders == [], f"G0 violated: {offenders}"

def load_bars(feed: Path, limit: int | None = None):
    out = []
    with feed.open() as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            if limit is not None and i >= limit:
                break
            out.append((int(r["timestamp"]), float(r["open"]), float(r["high"]),
                        float(r["low"]), float(r["close"]), float(r["volume"])))
    return out

def test_abi_version_and_symbols(test_so):
    lib = abi.load_library(test_so)
    assert lib.pf_abi_version() == 4
    for name in abi.V4_EXPORTS:
        assert getattr(lib, name) is not None

def test_run_is_deterministic_and_hash_recorded(test_so, test_feed):
    bars = load_bars(test_feed, 5000)
    with EngineHandle(test_so) as h:
        h.set_broker_state_hash_recording(True)
        r1 = h.run_full(bars, "15")
        r2 = h.run_full(bars, "15")
    assert r1.status == 0 and r2.status == 0
    assert r1.trades == r2.trades and len(r1.trades) > 0
    assert r1.broker_state_hash == r2.broker_state_hash
    assert len(r1.broker_state_hash) == r1.script_bars_processed == 5000

def test_tail_flags_keep_prefix(test_so, test_feed):
    bars = load_bars(test_feed, 3000)
    with EngineHandle(test_so) as h:
        h.set_broker_state_hash_recording(True)
        base = h.run_full(bars, "15")
        h.set_realtime_tail(True, 2 * len(bars))
        h.set_probe_suppress_tail_logic(True)
        tail = h.run_full(bars, "15")
        h.set_realtime_tail(False, 0)
        h.set_probe_suppress_tail_logic(False)
        back = h.run_full(bars, "15")
    assert tail.status == 0
    assert tail.broker_state_hash[:-1] == base.broker_state_hash[:-1]
    assert back.trades == base.trades and back.broker_state_hash == base.broker_state_hash

def test_abort_returns_not_completed(test_so, test_feed):
    bars = load_bars(test_feed)  # full feed (~222k bars) so the run lasts long enough
    with EngineHandle(test_so) as h:
        t = threading.Timer(0.01, h.request_abort)
        t.start()
        r = h.run_full(bars, "15")
        t.join()
        assert r.status in (0, 1)
        if r.status == 1:
            assert r.trades == []  # NOT_COMPLETED: the report is discarded by the caller
        r2 = h.run_full(bars[:2000], "15")  # an idle abort never leaks into the next run
        assert r2.status == 0

def test_accessors_and_pending_book(test_so, test_feed):
    bars = load_bars(test_feed, 4000)
    with EngineHandle(test_so) as h:
        r = h.run_full(bars, "15")
        assert all(t.close_cause in range(0, 7) for t in r.trades)
        assert all(isinstance(t.entry_id, str) for t in r.trades)
        for po in r.pending_orders:
            assert po["struct_version"] == 1 and "id" in po and "type" in po
            rc, qty, close_only, partition = h.probe_fill_qty(po["index"], bars[-1][4])
            assert rc in (0, 1)
            assert h.level_resolved(po["index"]) in (0, 1)
        assert h.effective_levels(999)[0] == -1 and h.probe_fill_qty(999, 1.0)[0] == -1
