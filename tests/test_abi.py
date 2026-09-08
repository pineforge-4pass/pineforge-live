import csv, threading
from pathlib import Path
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

def test_reuse_hazard_is_contained(test_so, test_feed):
    """The reused-pf_strategy_t hazard (a compiled script's own indicator/series
    state is not reset by the engine's reset_run_state()) must not leak through
    the binding: two run_full() calls on ONE handle must equal one run_full()
    call on each of two SEPARATE fresh handles."""
    bars = load_bars(test_feed, 5000)
    with EngineHandle(test_so) as h:
        h.set_broker_state_hash_recording(True)
        r1 = h.run_full(bars, "15")
        r2 = h.run_full(bars, "15")
    with EngineHandle(test_so) as h1:
        h1.set_broker_state_hash_recording(True)
        s1 = h1.run_full(bars, "15")
    with EngineHandle(test_so) as h2:
        h2.set_broker_state_hash_recording(True)
        s2 = h2.run_full(bars, "15")
    assert r1.trades == s1.trades and r1.broker_state_hash == s1.broker_state_hash
    assert r2.trades == s2.trades and r2.broker_state_hash == s2.broker_state_hash

def test_accessors_valid_after_run(test_so, test_feed):
    """Pending-order / scalar accessors called after run_full() must read the
    strategy that produced THAT run, not a stale or already-freed one."""
    bars = load_bars(test_feed, 4000)
    with EngineHandle(test_so) as h:
        r = h.run_full(bars, "15")
        assert len(r.pending_orders) == h.lib.strategy_pending_orders_len(h._s)
        if r.pending_orders:
            po = r.pending_orders[0]
            rc, qty, close_only, part = h.probe_fill_qty(po["index"], bars[-1][4])
            assert rc in (0, 1)

def test_abort_returns_not_completed(test_so, test_feed):
    # Full feed (~222k bars); run_backtest_full itself takes ~29ms. Building the
    # BarC array first takes ~110ms, so a timer armed before run_full() would
    # fire (and be discarded by the engine, which is idle) long before the run
    # starts. Arm it instead at run entry by wrapping the bound C function, so
    # the 1ms delay lands inside the ~29ms run and the abort is observable.
    bars = load_bars(test_feed)
    with EngineHandle(test_so) as h:
        original_run = h.lib.run_backtest_full

        def run_and_arm_abort(*args, **kwargs):
            threading.Timer(0.001, h.request_abort).start()
            return original_run(*args, **kwargs)

        h.lib.run_backtest_full = run_and_arm_abort
        r = h.run_full(bars, "15")
        h.lib.run_backtest_full = original_run  # restore (keeps its argtypes/restype)
        assert r.status == 1
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
