import asyncio
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
