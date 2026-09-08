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

def test_weekly_buckets_are_monday_anchored():
    # 2020-01-01T00:00:00Z is a Wednesday; the week's Monday is 2019-12-30T00:00:00Z.
    ts = 1_577_836_800_000  # 2020-01-01T00:00:00Z
    monday = 1_577_664_000_000  # 2019-12-30T00:00:00Z
    assert policy.bucket_start(ts, "1W") == monday
    assert policy.bucket_start(monday, "1W") == monday
    assert policy.bucket_start(monday + 7 * 86_400_000 - 1, "1W") == monday

def test_tf_ms_rejects_bad_input():
    for bad in ("", "0", "abc", "1H", "M", "1M", "15m"):
        with pytest.raises(ValueError):
            policy.tf_ms(bad)

def test_tf_ms_rejects_process_abort_escapes():
    # F1: both strings used to reach tf_ms's old accept-anything-int()-can-
    # parse path and only crash later inside the engine's own stoi cast
    # (an uncaught C++ exception that aborts the whole process): a non-ASCII
    # decimal digit int()/str.isdigit() both accept ("١٥", Arabic-Indic 15) and a
    # multiplier stoi can't hold ("99999999999"). Both must now be rejected
    # in Python, before any engine call.
    #
    # Finding 1: Python's `$` in re.match admits a trailing newline, so the
    # grammar must use fullmatch (or \Z) -- "15\n" used to be accepted (a
    # different epoch_hash from "15" even though the engine treats them the
    # same) and "D\n" raised KeyError instead of ValueError. Pinned here too.
    for bad in ("١٥", "99999999999", "15 ", " 15", "15\n", "D\n"):
        with pytest.raises(ValueError):
            policy.tf_ms(bad)

def test_tf_ms_bounds_seconds_not_just_the_multiplier():
    # Finding 2: the engine computes tf-in-seconds in a signed 32-bit int
    # (day/week multipliers get multiplied by 86400/604800 with no
    # widening), so a multiplier within _MAX_MULT can still overflow once
    # converted to seconds. tf_ms must bound the product, not just the
    # multiplier: 24855D/3550W fit; 24856D/3551W overflow and must raise.
    assert policy.tf_ms("24855D") == 24855 * 86_400_000
    assert policy.tf_ms("3550W") == 3550 * 604_800_000  # N5: pin the W-side edge symmetrically
    for bad in ("24856D", "3551W"):
        with pytest.raises(ValueError):
            policy.tf_ms(bad)

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
