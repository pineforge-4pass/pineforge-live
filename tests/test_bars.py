import pytest
from pineforge_live import types as T
from pineforge_live.bars import policy, builder

def tick(ts, price, qty=1.0, seq=None):
    return T.NormalizedTick(ts=ts, seq=seq if seq is not None else ts, price=price, qty=qty)

def test_policy_constants_and_buckets():
    assert policy.BAR_POLICY_VERSION == "v1:first-print-open/carry-forward-zero-volume"
    assert policy.tf_ms("15") == 900_000 and policy.tf_ms("1D") == 86_400_000
    assert policy.bucket_start(1_577_836_800_000 + 899_999, "15") == 1_577_836_800_000
    assert policy.bucket_start(1_577_836_800_000 + 900_000, "15") == 1_577_837_700_000

def test_forming_bar_open_is_first_print():
    b = builder.FormingBarBuilder("15")
    t0 = 1_577_836_800_000
    assert b.push(tick(t0 + 1000, 100.0)) == []
    assert b.push(tick(t0 + 2000, 101.0, 2.0)) == []
    assert b.push(tick(t0 + 3000, 99.5, 0.5)) == []
    f = b.forming()
    assert (f.o, f.h, f.l, f.c, f.v, f.trade_count, f.is_forming) == (100.0, 101.0, 99.5, 99.5, 3.5, 3, True)
    done = b.push(tick(t0 + 900_000, 99.0))
    assert len(done) == 1 and done[0].o == 100.0 and done[0].c == 99.5 and not done[0].is_forming
    assert b.forming().o == 99.0 and b.forming().ts_open == t0 + 900_000

def test_empty_interval_is_carry_forward_zero_volume():
    b = builder.FormingBarBuilder("15")
    t0 = 1_577_836_800_000
    b.push(tick(t0 + 1, 100.0)); done = b.push(tick(t0 + 3 * 900_000 + 1, 105.0))
    assert [d.ts_open for d in done] == [t0, t0 + 900_000, t0 + 2 * 900_000]
    assert done[1].synthesized and done[1].v == 0.0 and done[1].o == done[1].h == done[1].l == done[1].c == 100.0
    assert not done[0].synthesized

def test_compare_bar_volume_tolerance():
    a = T.NormalizedBar(0, 1, 2, 0.5, 1.5, 10.0, 3)
    b = T.NormalizedBar(0, 1, 2, 0.5, 1.5, 10.0 + 5e-7, 4)
    assert builder.compare_bar(a, b) == []
    assert builder.compare_bar(a, T.NormalizedBar(0, 1, 2, 0.5, 1.6, 10.0, 3)) == ["c"]
    assert builder.compare_bar(a, T.NormalizedBar(0, 1, 2, 0.5, 1.5, 10.01, 3)) == ["v"]

def test_bars_hash_is_order_sensitive_and_stable():
    bars = [T.NormalizedBar(i * 900_000, 1.0 + i, 2.0 + i, 0.5, 1.5, 10.0, 1) for i in range(5)]
    h1 = builder.bars_hash_all(bars); h2 = builder.bars_hash_all(bars)
    assert h1 == h2 and h1 != builder.bars_hash_all(list(reversed(bars)))
    assert builder.bars_hash_all(bars) == builder.bars_hash(builder.bars_hash_all(bars[:-1]), bars[-1])
