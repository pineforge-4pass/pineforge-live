import asyncio, csv
import pytest
from pineforge_live import types as T
from pineforge_live.adapters import tape
from pineforge_live.bars import FormingBarBuilder, compare_bar

def bars():
    t0 = 1_577_836_800_000
    return [T.NormalizedBar(t0 + i * 900_000, 100 + i, 105 + i, 95 + i, 101 + i, 10.0, 0) for i in range(4)]

def collect(agen):
    async def run():
        out = []
        async for e in agen:
            out.append(e)
        return out
    return asyncio.run(run())

def test_bar_source_events_forming_then_confirmed_and_history():
    clock = tape.TapeClock(0)
    src = tape.TapeBarSource(bars(), "15", clock)
    ev = collect(src.events(T.InstrumentId("X", T.MarketType.PERP, "Y"), "15"))
    kinds = [type(e).__name__ for e in ev]
    assert kinds == ["Forming", "Confirmed"] * 4
    assert ev[0].bar.o == 100 and ev[0].bar.is_forming and ev[1].bar == bars()[0]
    hist, complete = asyncio.run(src.history(T.InstrumentId("X", T.MarketType.PERP, "Y"), "15", bars()[1].ts_open, bars()[3].ts_open))
    assert [b.ts_open for b in hist] == [bars()[1].ts_open, bars()[2].ts_open] and complete
    assert clock.now_ms() == bars()[3].ts_open + 900_000

def test_path4_reproduces_bar_and_probe_points():
    src = tape.TapeTickSource(bars(), "15", policy="path4", seed=1)
    ticks = [e.tick for e in collect(src.subscribe(T.InstrumentId("X", T.MarketType.PERP, "Y"), 0)) if isinstance(e, T.Tick)]
    assert len(ticks) == 16
    b = FormingBarBuilder("15"); done = []
    for t in ticks:
        done += b.push(t)
    done.append(b.forming())
    assert [compare_bar(d, ref) for d, ref in zip(done, bars())] == [[], [], [], []]
    first = ticks[:4]
    assert [t.ts - bars()[0].ts_open for t in first] == [60_000, 300_000, 600_000, 840_000]
    assert [t.price for t in first] == [100.0, 105.0, 95.0, 101.0]   # |H-O| == |O-L| -> high-first

def test_path4_reversed_and_dense_and_random_keep_ohlc():
    for policy in ("path4-reversed", "dense", "random-ohlc"):
        src = tape.TapeTickSource(bars(), "15", policy=policy, seed=7)
        ticks = [e.tick for e in collect(src.subscribe(T.InstrumentId("X", T.MarketType.PERP, "Y"), 0)) if isinstance(e, T.Tick)]
        b = FormingBarBuilder("15"); done = []
        for t in ticks:
            done += b.push(t)
        done.append(b.forming())
        assert [compare_bar(d, ref) for d, ref in zip(done, bars())] == [[], [], [], []], policy
    r1 = [e.tick.price for e in collect(tape.TapeTickSource(bars(), "15", "random-ohlc", 7).subscribe(T.InstrumentId("X", T.MarketType.PERP, "Y"), 0)) if isinstance(e, T.Tick)]
    r2 = [e.tick.price for e in collect(tape.TapeTickSource(bars(), "15", "random-ohlc", 7).subscribe(T.InstrumentId("X", T.MarketType.PERP, "Y"), 0)) if isinstance(e, T.Tick)]
    assert r1 == r2   # seeded, deterministic

def test_real_tick_policy_requires_ticks_csv():
    # item 2: failing late (only at subscribe()) with an opaque TypeError
    # is a bad diagnostic; __init__ must refuse immediately.
    with pytest.raises(ValueError):
        tape.TapeTickSource(bars(), "15", "real", ticks_csv=None)

def test_tape_bar_source_rejects_out_of_order_or_duplicate_bars():
    # item 3: a recorded feed must be strictly increasing by ts_open --
    # [B1, B0] or [B0, B0] must refuse construction, not produce a
    # nonsense Gap(from > to).
    b = bars()
    with pytest.raises(ValueError):
        tape.TapeBarSource([b[1], b[0]], "15", tape.TapeClock(0))
    with pytest.raises(ValueError):
        tape.TapeBarSource([b[0], b[0]], "15", tape.TapeClock(0))

def test_tape_bar_source_rejects_tf_mismatch():
    # item 5: `instrument`/`tf` were silently ignored; a caller passing the
    # wrong tf is a bug worth surfacing, not silently playing the 15m tape.
    src = tape.TapeBarSource(bars(), "15", tape.TapeClock(0))
    inst = T.InstrumentId("X", T.MarketType.PERP, "Y")
    with pytest.raises(ValueError):
        asyncio.run(src.history(inst, "60", 0, 1))
    with pytest.raises(ValueError):
        collect(src.events(inst, "60"))

def test_real_tick_policy_emits_tick_gap_on_seq_discontinuity(tmp_path):
    # item 9: a seq jump of more than 1 must surface as a TickGap, not pass
    # silently -- the gap-heal logic (a later task) needs it from the tape too.
    p = tmp_path / "ticks.csv"
    with p.open("w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["ts", "seq", "price", "qty"])
        for seq, ts, price in [(7, 1000, 100.0), (8, 1001, 100.5), (10, 1003, 101.0)]:
            w.writerow([ts, seq, price, 1.0])
    src = tape.TapeTickSource(bars(), "15", "real", ticks_csv=p)
    events = collect(src.subscribe(T.InstrumentId("X", T.MarketType.PERP, "Y"), 8))
    kinds = [(type(e).__name__, e.tick.seq if isinstance(e, T.Tick) else (e.from_seq, e.to_seq)) for e in events]
    assert kinds == [("Tick", 8), ("TickGap", (8, 10)), ("Tick", 10)]
    assert events[1].healed is False

    # Finding 6: a hole entirely below from_seq is not this subscriber's
    # concern -- from_seq=10 skips straight to Tick(10) with no TickGap,
    # even though the underlying file has a seq jump (8 -> 10) before it.
    src2 = tape.TapeTickSource(bars(), "15", "real", ticks_csv=p)
    events2 = collect(src2.subscribe(T.InstrumentId("X", T.MarketType.PERP, "Y"), 10))
    kinds2 = [(type(e).__name__, e.tick.seq if isinstance(e, T.Tick) else (e.from_seq, e.to_seq)) for e in events2]
    assert kinds2 == [("Tick", 10)]
