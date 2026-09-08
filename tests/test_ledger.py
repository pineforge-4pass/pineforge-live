import math, pytest
from pineforge_live.core.ledger import Ledger, LedgerDivergence, BarsDivergence, LedgerGap, RecomputeAborted
from pineforge_live import types as T
from tests.helpers import load_bars, make_handle, corpus_spec, corpus_spec_bracket, open_journal

@pytest.fixture
def env(test_so, test_feed, tmp_path):
    spec = corpus_spec(); h = make_handle(test_so, spec); j, m = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    return spec, h, j, load_bars(test_feed, 2200)

@pytest.fixture
def env_bracket(test_so_bracket, test_feed, tmp_path):
    """The corpus bracket probe (`ta-pivot-atr-stop-target-01`): unlike the
    always-in-market SMA probe (`env`), it goes flat between trades, so its
    2000-2600 window exercises flat->open ENTRY fills and close-to-flat
    bars with NO reversals -- the complement of `env`'s reversal-heavy
    window, per N1's re-review pin."""
    spec = corpus_spec_bracket(); h = make_handle(test_so_bracket, spec); j, m = open_journal(tmp_path)
    j.append_epoch(spec.epoch_hash(), "{}")
    return spec, h, j, load_bars(test_feed, 2600)

def test_seed_then_settle_g1_holds(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, runtime_config_hash="rc")
    s0 = L.seed(bars[:2000])
    assert s0.bar_index == 1999 and len(s0.hashes) == 2000 and L.n == 2000
    # Controller ruling: with realtime_tail on (corpus_spec's default), the
    # corpus run never produces an open_at_end row -- pinned here so a
    # future engine/config change that starts emitting one is caught.
    assert not any(t.open_at_end for t in s0.trades)
    assert s0.position_delta == 0.0 and s0.prev_position_size == s0.position_size and s0.entry_fills == []
    for i in range(2000, 2010):
        s = L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)
        assert s.bar_index == i and len(s.hashes) == i + 1
        assert not any(t.open_at_end for t in s.trades)
        assert j.last_settlement(spec.epoch_hash())["bar_index"] == i
    row = j.last_settlement(spec.epoch_hash())
    assert row["broker_state_hash"] == L.last.hashes[-1] and row["trades_sha256"] == L.last.trades_sha256
    assert not math.isnan(row["equity"]) and row["position"] == L.last.position_size

def test_seed_mismatch_raises(env):
    spec, h, j, bars = env
    with pytest.raises(LedgerDivergence) as e:
        Ledger(h, spec, j, "rc").seed(bars[:2000], expected_trades_sha256="0" * 64)
    assert e.value.cause == "seed_mismatch"

def test_seed_journal_conflict_is_seed_conflict(env):
    spec, h, j, bars = env
    Ledger(h, spec, j, "rc").seed(bars[:2000])
    # Same history (so the bar row is byte-for-byte idempotent and never
    # conflicts) but a different runtime_config_hash -> the settlement row
    # conflicts. seed() must label this seed_conflict regardless of which
    # of the two rows it came from (unlike settle(), it has no "revised
    # bar" case to distinguish).
    with pytest.raises(LedgerDivergence) as e:
        Ledger(h, spec, j, "rc2").seed(bars[:2000])
    assert e.value.cause == "seed_conflict"

def test_revised_bar_is_bars_divergence(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000]); L.settle(bars[2000], 0)
    revised = T.NormalizedBar(bars[2000].ts_open, bars[2000].o, bars[2000].h + 1, bars[2000].l, bars[2000].c, bars[2000].v, 0)
    with pytest.raises(BarsDivergence):
        L.settle(revised, 0)
    assert j.rows("incidents", "kind=?", ("bars_divergence",))

def test_identical_redelivery_of_last_bar_is_idempotent_noop(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    before = L.last
    same = T.NormalizedBar(bars[1999].ts_open, bars[1999].o, bars[1999].h, bars[1999].l, bars[1999].c, bars[1999].v, bars[1999].trade_count)
    result = L.settle(same, 0)
    assert result is before
    assert L.n == 2000 and L.last is before
    assert j.last_settlement(spec.epoch_hash())["bar_index"] == 1999
    assert not j.rows("incidents", "kind=?", ("bars_divergence",))

def test_non_contiguous_bar_raises_ledger_gap(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    skip = T.NormalizedBar(bars[2001].ts_open, bars[2001].o, bars[2001].h, bars[2001].l, bars[2001].c, bars[2001].v, bars[2001].trade_count)
    with pytest.raises(LedgerGap) as e:
        L.settle(skip, 0)
    assert e.value.expected == bars[2000].ts_open and e.value.got == skip.ts_open and not e.value.forming
    assert L.n == 2000 and L.last.bar_index == 1999  # staged state discarded, not committed

def test_off_grid_bar_raises_ledger_gap(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    off = T.NormalizedBar(bars[2000].ts_open + 1, bars[2000].o, bars[2000].h, bars[2000].l, bars[2000].c, bars[2000].v, bars[2000].trade_count)
    with pytest.raises(LedgerGap):
        L.settle(off, 0)
    assert L.n == 2000

def test_forming_bar_raises_ledger_gap(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    forming = T.NormalizedBar(bars[2000].ts_open, bars[2000].o, bars[2000].h, bars[2000].l, bars[2000].c, bars[2000].v,
                              bars[2000].trade_count, is_forming=True)
    with pytest.raises(LedgerGap) as e:
        L.settle(forming, 0)
    assert e.value.forming is True and e.value.expected == e.value.got == bars[2000].ts_open
    assert L.n == 2000

def test_abort_then_retry_same_bar_succeeds(env, monkeypatch):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    real_run_full = h.run_full
    calls = {"n": 0}
    def flaky(bar_list, tf, **kw):
        calls["n"] += 1
        r = real_run_full(bar_list, tf, **kw)
        if calls["n"] == 1:
            r.status = 1
        return r
    monkeypatch.setattr(h, "run_full", flaky)
    with pytest.raises(RecomputeAborted):
        L.settle(bars[2000], 0)
    assert L.n == 2000 and L.last.bar_index == 1999  # unretried abort leaves the ledger unmoved
    s = L.settle(bars[2000], 0)  # retry the SAME bar -> succeeds
    assert s.bar_index == 2000 and L.n == 2001 and L.last is s

def test_g1_hash_mismatch_detected(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    # tamper the journaled hash of bar 1999 (simulates a mutant .so / divergent recompute)
    j._exec("UPDATE settlements SET broker_state_hash=? WHERE bar_index=1999", (f"{123:016x}",))
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "g1_hash"
    assert L.n - 1 == L.last.bar_index  # a divergence never commits the staged state

def test_g1_trades_mismatch_detected(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    j._exec("UPDATE settlements SET trades_sha256=? WHERE bar_index=1999", ("0" * 64,))
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "g1_trades"
    assert L.n - 1 == L.last.bar_index

def test_g1_bars_hash_mismatch_detected(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    j._exec("UPDATE settlements SET bars_hash=? WHERE bar_index=1999", (f"{999:016x}",))
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "g1_bars"
    assert L.n - 1 == L.last.bar_index

def test_g1_prefix_mismatch_detail_is_actionable(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    L.last.keys.pop()  # diverge the in-memory prefix from what settle() will recompute
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "g1_prefix"
    assert e.value.detail["prefix_len"] != e.value.detail["prev_len"]
    assert "index" in e.value.detail
    assert L.n - 1 == L.last.bar_index

def test_bar_journal_conflict_is_bars_divergence(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    # Pre-insert a conflicting `bars` row for the NEXT bar's ts_open -- the
    # ledger's own in-memory history has never seen this ts_open, so only
    # the journal-layer conflict (finding 4) can catch it.
    fake = T.NormalizedBar(bars[2000].ts_open, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    j.append_bar(spec.epoch_hash(), fake, 12345)
    with pytest.raises(BarsDivergence):
        L.settle(bars[2000], 0)
    assert j.rows("incidents", "kind=?", ("bars_divergence",))
    assert L.n - 1 == L.last.bar_index

def test_settlement_journal_conflict_is_settlement_conflict(env):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    # Pre-insert a conflicting `settlements` row for bar_index 2000 -- the
    # bar row itself journals cleanly (no bar conflict), so this isolates
    # the settlement-row conflict path, which finding 4 requires to be
    # LedgerDivergence("settlement_conflict", ...), not BarsDivergence.
    j.append_settlement({"bar_index": 2000, "epoch_hash": spec.epoch_hash(), "runtime_config_hash": "other-rc",
                         "bars_hash": 999, "broker_state_hash": 999, "trades_len": 0, "position": 0.0,
                         "equity": 0.0, "trades_sha256": "0" * 64})
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "settlement_conflict"
    assert L.n - 1 == L.last.bar_index

def test_hash_len_mismatch_detected_on_settle(env, monkeypatch):
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    real_run_full = h.run_full
    def truncated(bar_list, tf, **kw):
        r = real_run_full(bar_list, tf, **kw)
        r.broker_state_hash = r.broker_state_hash[:-1]
        return r
    monkeypatch.setattr(h, "run_full", truncated)
    with pytest.raises(LedgerDivergence) as e:
        L.settle(bars[2000], 0)
    assert e.value.cause == "hash_len"

def test_entry_fills_explain_position_delta(env):
    """N1 pin: on the always-in-market SMA probe, EVERY bar past the first
    entry is a reversal (prev and now opposite non-zero signs) -- bar 2001
    is one (prev -1 -> now +1, avg ~=168.4). The old magnitude-based rule
    mislabelled every one of these as an EXIT of the OLD side at the
    close; the fixed direction-based rule must label all of them ENTRY of
    the NEW side at position_avg_price, with 0 mislabels across the window."""
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    saw_fill = False
    saw_reversal = False
    for i in range(2000, 2061):
        prev_pos = L.last.position_size
        s = L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)
        sign = lambda k: k.qty if k.is_long else -k.qty
        explained = sum(sign(k) for k in s.keys if k.entry_bar == s.bar_index) - sum(sign(k) for k in s.keys if k.exit_bar == s.bar_index)
        assert s.position_delta == pytest.approx(s.position_size - prev_pos)
        assert s.prev_position_size == pytest.approx(prev_pos)
        assert explained + (s.position_delta - explained) == pytest.approx(s.position_delta)
        unexplained = s.position_delta - explained
        now = s.position_size
        if abs(unexplained) > 1e-12:
            assert len(s.entry_fills) == 1
            fill = s.entry_fills[0]
            assert fill["qty"] == pytest.approx(abs(unexplained))
            assert fill["bar_index"] == s.bar_index and fill["intent"] is None
            if now == 0.0:
                pytest.fail(f"bar {i}: unexplained fill but position is fully flat")
            elif prev_pos * now < 0:
                # Reversal: ENTRY of the NEW side at position_avg_price.
                assert fill["leg"] == "ENTRY"
                assert fill["is_long"] == (now > 0)
                assert fill["price"] == pytest.approx(s.position_avg_price)
                saw_reversal = True
                if i == 2001:
                    # The re-review's concrete pin: prev -1 -> +1, avg ~168.4.
                    assert fill["is_long"] is True
                    assert fill["price"] == pytest.approx(168.4, abs=0.05)
                    assert fill["qty"] == pytest.approx(1.0)
            elif prev_pos == 0.0:
                # Flat -> open: ENTRY of the new side, full size, at avg price.
                assert fill["leg"] == "ENTRY"
                assert fill["is_long"] == (now > 0)
                assert fill["qty"] == pytest.approx(abs(now))
                assert fill["price"] == pytest.approx(s.position_avg_price)
            else:
                # In-place reduce (same side, still open) or pyramid add:
                # the general direction rule -- checked against the SAME
                # semantics N1 specifies, not the implementation's exact
                # expression, so this still catches a regression to the
                # magnitude-based rule.
                moves_toward_final = (unexplained > 0) == (now > 0)
                if moves_toward_final:
                    assert fill["leg"] == "ENTRY"
                    assert fill["is_long"] == (now > 0)
                    assert fill["price"] == pytest.approx(s.position_avg_price)
                else:
                    assert fill["leg"] == "EXIT"
                    assert fill["is_long"] == (prev_pos > 0)
                    assert fill["price"] == pytest.approx(s.bar.c)
            saw_fill = True
        else:
            assert s.entry_fills == []
    assert saw_fill, "expected at least one entry_fills-producing bar in [2000, 2060]"
    assert saw_reversal, "expected at least one reversal bar (the SMA probe's defining case) in [2000, 2060]"

def test_entry_fills_bracket_opens_and_closes(env_bracket):
    """N1 pin (complement of the SMA test): the bracket probe goes flat
    between trades, so its 2000-2600 window is all flat<->open ENTRY/close
    pairs and zero reversals -- 21 opens, 21 closes, 0 mislabels."""
    spec, h, j, bars = env_bracket
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    opens = closes = reversals = 0
    for i in range(2000, 2600):
        prev_pos = L.last.position_size
        s = L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)
        sign = lambda k: k.qty if k.is_long else -k.qty
        explained = sum(sign(k) for k in s.keys if k.entry_bar == s.bar_index) - sum(sign(k) for k in s.keys if k.exit_bar == s.bar_index)
        unexplained = s.position_delta - explained
        now = s.position_size
        if prev_pos * now < 0:
            reversals += 1
        if abs(unexplained) > 1e-12:
            assert len(s.entry_fills) == 1
            fill = s.entry_fills[0]
            assert prev_pos == 0.0, f"bar {i}: expected a flat->open bar, prev={prev_pos} now={now}"
            assert fill["leg"] == "ENTRY"
            assert fill["is_long"] == (now > 0)
            assert fill["qty"] == pytest.approx(abs(now))
            assert fill["price"] == pytest.approx(s.position_avg_price)
            opens += 1
        else:
            assert s.entry_fills == []
            if prev_pos != 0.0 and now == 0.0:
                assert any(k.exit_bar == s.bar_index for k in s.new_closed)
                closes += 1
    assert reversals == 0
    assert opens == 21 and closes == 21

def test_seed_shifted_history_conflict_leaves_no_bars_row(env):
    """N2 pin: a shifted-history seed that conflicts must leave no `bars`
    row behind -- the poison row that used to break the CORRECT restart's
    settle() (misdiagnosed as a revised bar) because the old code wrote
    the bar row before checking whether the settlement row would conflict."""
    spec, h, j, bars = env
    Ledger(h, spec, j, "rc").seed(bars[:2000])
    with pytest.raises(LedgerDivergence) as e:
        Ledger(h, spec, j, "rc").seed(bars[1:2001])
    # n7: the shifted history is now caught by the FULL hash-vector check
    # (every journaled settlement row inside the history's own length),
    # which runs before the settlement pre-check and names the first bar
    # whose recomputed hash disagrees -- a strictly better diagnosis of a
    # shifted chain than "a row for bar N already exists".
    assert e.value.cause == "seed_hashes"
    assert not j.rows("bars", "epoch_hash=? AND ts_open=?", (spec.epoch_hash(), bars[2000].ts_open))
    # The correct history then re-seeds cleanly (idempotent) and settle()
    # continues onto bars[2000] with no incident.
    L2 = Ledger(h, spec, j, "rc")
    L2.seed(bars[:2000])
    s = L2.settle(bars[2000], 0)
    assert s.bar_index == 2000
    assert not j.rows("incidents", "kind=?", ("bars_divergence",))

def test_forming_bar_with_last_ts_open_raises_ledger_gap_not_divergence(env):
    """N3 pin: a forming bar sharing the ledger's LAST settled ts_open must
    raise LedgerGap (it was never settled -- it's still forming), not fall
    into the revised-bar branch and raise BarsDivergence + an incident."""
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    forming = T.NormalizedBar(bars[1999].ts_open, bars[1999].o, bars[1999].h, bars[1999].l, bars[1999].c + 1,
                              bars[1999].v, bars[1999].trade_count, is_forming=True)
    with pytest.raises(LedgerGap) as e:
        L.settle(forming, 0)
    assert e.value.forming is True
    assert e.value.expected == e.value.got == bars[1999].ts_open
    assert not j.rows("incidents", "kind=?", ("bars_divergence",))
    assert L.n == 2000 and L.last.bar_index == 1999

def test_mtm_sign_and_new_closed_pin(env):
    """Finding 10 pin: equity_mtm's sign convention, and new_closed's count
    on the bar-2001 reversal (its old side's exit closes exactly 1 trade)."""
    spec, h, j, bars = env
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    expected_diff = {2000: -2.34, 2001: -0.16, 2002: -0.04}
    for i in (2000, 2001, 2002):
        s = L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)
        avg = s.position_avg_price if s.position_avg_price is not None else 0.0
        diff = s.equity_mtm - s.equity
        assert diff == pytest.approx(s.position_size * (s.bar.c - avg))
        assert diff == pytest.approx(expected_diff[i], abs=0.02)
        if i == 2001:
            assert len(s.new_closed) == 1

def test_settle_result_book_captured_and_survives_later_run(env_bracket):
    """N5 pin (task-2 re-review 2): the original version of this test
    could not actually detect a stale book -- it compared `set(keys) ==
    set(keys)` (always true regardless of WHEN the levels were read) and
    `s.book == before` where `before = dict(s.book)` (the SAME captured
    object, so trivially equal to itself). On the SMA fixture's sole
    always-`(None, None, None)` MARKET row, even a genuinely stale read
    would pass both. Fixed here on the bracket fixture (real numeric
    stop/limit levels, per N1's re-review pin) with two discriminating
    checks: (1) FULL VALUE equality -- not just keys -- between
    `s.book` and a `settled_book()` taken immediately after the same run
    (the accessor-window contract's own claim); (2) after a FOREIGN
    `run_full()` on the same handle, a FRESH `settled_book()` read must no
    longer agree with what `s.book` captured -- either because it now
    describes the foreign run's mirror instead (a value disagreement), or
    because the foreign run resized the mirror out from under the old
    index entirely, in which case `settled_book`'s restored strict
    contract (Task 6 prelim) raises `RuntimeError` rather than fabricating
    a plausible-but-wrong Intent. `s.book` itself -- already-resolved,
    frozen dataclasses, not a live view -- must be unaffected either way."""
    from pineforge_live.core.book import settled_book
    spec, h, j, bars = env_bracket
    L = Ledger(h, spec, j, "rc"); L.seed(bars[:2000])
    L.settle(bars[2000], 0)
    s = None
    for i in range(2001, 2007):
        s = L.settle(bars[i], now_ms=bars[i].ts_open + 900_000)
    assert s.bar_index == 2006
    assert s.book, "expected >=1 resting bracket intent by bar 2006 (position opened at bar 2005)"

    fresh = settled_book(h, s)  # taken immediately after the same run -- the valid accessor window
    assert s.book == fresh  # full VALUE equality, not just matching keys

    before = dict(s.book)
    h.run_full([b.ohlcv() for b in bars[:2000]], spec.script_tf)  # a FOREIGN run on the SAME handle
    assert s.book == before  # the captured SettleResult itself is untouched

    try:
        drifted = settled_book(h, s)
    except RuntimeError:
        pass  # the foreign run resized the mirror -- a fresh read can no longer even resolve it
    else:
        assert drifted != before, "a fresh settled_book() after a foreign run must not still agree with the captured book"
