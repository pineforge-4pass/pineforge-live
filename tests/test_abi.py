import csv, re, threading
from pathlib import Path
import pytest
from pineforge_live.engine import abi
from pineforge_live.engine.handle import EngineHandle
from tests.helpers import load_bars as load_normalized_bars

PKG = Path(__file__).resolve().parents[1] / "pineforge_live"

# Finding 7: the header also exports the fill-deciding `run_backtest(`
# (non-full, `pineforge.h:461`) alongside `run_backtest_full` -- G0 must
# keep BOTH that and the streaming lifecycle out of every module, and out
# of the bound ABI surface itself.
_RUN_BACKTEST_NON_FULL = re.compile(r"\brun_backtest\b(?!_full)")

def test_g0_no_stream_symbols():
    stream_offenders = [p for p in PKG.rglob("*.py") if "strategy_stream" in p.read_text()]
    assert stream_offenders == [], f"G0 violated (strategy_stream): {stream_offenders}"
    run_backtest_offenders = [p for p in PKG.rglob("*.py") if _RUN_BACKTEST_NON_FULL.search(p.read_text())]
    assert run_backtest_offenders == [], f"G0 violated (run_backtest, non-full): {run_backtest_offenders}"
    run_keys = [k for k in abi._PROTOTYPES if k.startswith("run_")]
    assert run_keys == ["run_backtest_full"], f"G0 violated: unexpected run_* export bound: {run_keys}"

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
    # Final 5: the prefix matching above (":-1") is vacuously true even if
    # the tail flags were silently dropped by the replay -- pin that the
    # flags are actually OBSERVABLE: at this bar count they change the
    # range-end trade (open_at_end 1 -> 0), so the last hash and/or the
    # trade list must differ from the untailed run.
    assert tail.broker_state_hash[-1] != base.broker_state_hash[-1] or base.trades != tail.trades
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
    # Final 5: at 4000 bars ta-sma-152 has ZERO pending orders -- the old
    # `if r.pending_orders:` guard made this test's body vacuous. 3000 bars
    # has exactly one (verified), so exercise it unconditionally.
    bars = load_bars(test_feed, 3000)
    with EngineHandle(test_so) as h:
        r = h.run_full(bars, "15")
        assert len(r.pending_orders) == h.lib.strategy_pending_orders_len(h._s)
        assert r.pending_orders
        po = r.pending_orders[0]
        rc, qty, close_only, part = h.probe_fill_qty(po["index"], bars[-1][4])
        assert rc == 0  # a live entry order fills (rc=0) at the close price (verified)

def test_abort_returns_not_completed(test_so, test_feed):
    # Full feed (~222k bars). Building the BarC array first takes ~110ms, so
    # a request made before run_full() reaches the C call would be discarded
    # by the idle engine. Engine v1.0.0 also discards a request that arrives
    # inside the C call before the run begins: it copies and checks the bars
    # first (more than 3ms for this feed in a fresh process), then consumes
    # any pending request. So request the abort repeatedly from the C call's
    # entry until it returns; a request made after the run begins is observed.
    bars = load_bars(test_feed)
    with EngineHandle(test_so) as h:
        original_run = h.lib.run_backtest_full
        returned = threading.Event()

        def request_until_returned():
            while not returned.wait(0.0005):
                h.request_abort()

        def run_and_request_abort(*args, **kwargs):
            requester = threading.Thread(target=request_until_returned)
            requester.start()
            try:
                return original_run(*args, **kwargs)
            finally:
                returned.set()
                requester.join()

        h.lib.run_backtest_full = run_and_request_abort
        r = h.run_full(bars, "15")
        h.lib.run_backtest_full = original_run  # restore (keeps its argtypes/restype)
        assert r.status == 1
        assert r.trades == []  # NOT_COMPLETED: the report is discarded by the caller
        r2 = h.run_full(bars[:2000], "15")  # an idle abort never leaks into the next run
        assert r2.status == 0

def test_run_full_rejects_bad_script_tf(test_so, test_feed):
    """A bad script_tf must be rejected in Python BEFORE any engine call --
    the engine aborts the whole process on a non-numeric timeframe (uncaught
    C++ stoi), and a strategy must not leak on the way to raising."""
    bars = load_bars(test_feed, 100)
    with EngineHandle(test_so) as h:
        create_calls, free_calls = [], []
        original_create, original_free = h.lib.strategy_create, h.lib.strategy_free

        def counted_create(*args, **kwargs):
            s = original_create(*args, **kwargs)
            create_calls.append(s)
            return s

        def counted_free(*args, **kwargs):
            free_calls.append(args[0] if args else None)
            return original_free(*args, **kwargs)

        h.lib.strategy_create = counted_create
        h.lib.strategy_free = counted_free
        live_s = h._s
        try:
            for bad in ("", "abc", None, "99999999999", "١٥"):  # F1: process-abort escapes must be rejected here too
                with pytest.raises(ValueError):
                    h.run_full(bars, bad)
        finally:
            h.lib.strategy_create = original_create
            h.lib.strategy_free = original_free
        assert create_calls == []  # no strategy was ever created for a bad script_tf
        assert free_calls == []
        assert h._s is live_s  # the handle's live strategy is unchanged

def test_accessors_and_pending_book(test_so, test_feed):
    # Final 5: 3000 bars (not 4000, which has zero pending orders) so the
    # `for po in r.pending_orders` loop below is not vacuous.
    bars = load_bars(test_feed, 3000)
    with EngineHandle(test_so) as h:
        r = h.run_full(bars, "15")
        assert all(t.close_cause in range(0, 7) for t in r.trades)
        assert all(isinstance(t.entry_id, str) for t in r.trades)
        assert r.pending_orders
        for po in r.pending_orders:
            assert po["struct_version"] == 1 and "id" in po and "type" in po
            rc, qty, close_only, partition = h.probe_fill_qty(po["index"], bars[-1][4])
            assert rc == 0  # a live entry order fills (rc=0) at the close price (verified)
            assert h.level_resolved(po["index"]) == 1  # an entry order resolves to 1 (verified)
        assert h.effective_levels(999)[0] == -1 and h.probe_fill_qty(999, 1.0)[0] == -1

def test_run_full_accepts_bar_objects_exposing_ohlcv(test_so, test_feed):
    """Prelim (Task 0 review finding 1): run_full() accepts a bar object
    exposing .ohlcv() (e.g. types.NormalizedBar) directly, not just an
    already-unpacked 6-tuple -- and produces an identical run either way."""
    tuples = load_bars(test_feed, 2000)
    objects = load_normalized_bars(test_feed, 2000)
    assert not isinstance(objects[0], tuple) and hasattr(objects[0], "ohlcv")
    with EngineHandle(test_so) as h:
        h.set_broker_state_hash_recording(True)
        r1 = h.run_full(tuples, "15")
    with EngineHandle(test_so) as h2:
        h2.set_broker_state_hash_recording(True)
        r2 = h2.run_full(objects, "15")
    assert r1.trades == r2.trades and r1.broker_state_hash == r2.broker_state_hash

def test_run_full_per_run_flags_do_not_grow_setter_log(test_so, test_feed):
    """Final 2: probe-only flags passed via `run_full(..., per_run=...)`
    must reach the engine (change the run) without ever being appended to
    `setter_log` -- a naive B2 that called the public
    set_probe_suppress_tail_logic/set_path_order methods per probe/evaluate
    call would grow the replay list without bound and diverge setter_log
    from EpochSpec.setter_sequence()."""
    bars = load_bars(test_feed, 3000)
    with EngineHandle(test_so) as h:
        h.set_broker_state_hash_recording(True)
        epoch_prefix = list(h.setter_log)
        base = h.run_full(bars, "15")
        assert h.setter_log == epoch_prefix  # a plain run never touches setter_log either
        for _ in range(3):
            tail = h.run_full(bars, "15", per_run=[("set_probe_suppress_tail_logic", (True,)),
                                                     ("set_path_order", (2,))])
            assert h.setter_log == epoch_prefix  # per_run never appended, no matter how many runs
        # per_run actually reached the engine and changed the run.
        assert tail.trades != base.trades or tail.broker_state_hash != base.broker_state_hash
