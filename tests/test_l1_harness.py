"""`scripts/l1_harness.py` end to end (spec §10.2 L1, in miniature): the
script is run as a subprocess over a short window and its report read
back, so the harness the operator actually types is the thing under test
-- exit code, the summary line on stdout, and the JSON contract."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from pineforge_live.core.live import signed_qty

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "l1_harness.py"
SUMMARY_KEYS = {"bars", "settle_fills", "probe_fills", "retracts", "probe_not_settled", "path_variant",
                "superseded", "stops", "incidents", "non_confirmed", "g1_failures",
                "recompute_ms_p99_settle", "recompute_ms_p99_probe"}

def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(HARNESS), *map(str, args)], capture_output=True, text=True, cwd=cwd)

def test_l1_harness_over_30_bars_is_clean(test_so, test_feed, tmp_path):
    """The release-criterion cadence in miniature: 30 bars of the corpus
    tape, evaluating at the tape's k=4 probe points and settling at the
    close, against a perfect venue. Exit 0, no G1 failure, and every probe
    fill either settled or retracted."""
    out = tmp_path / "l1.json"
    p = _run("--so", test_so, "--feed", test_feed, "--start", 2000, "--bars", 30,
             "--journal", tmp_path / "j", "--out", out)
    assert p.returncode == 0, p.stdout + p.stderr
    doc = json.loads(out.read_text())
    s = doc["summary"]
    assert set(s) == SUMMARY_KEYS
    assert s["bars"] == 30 and s["g1_failures"] == 0 and s["probe_not_settled"] == 0
    assert s["settle_fills"] >= 1 and s["probe_fills"] >= 1        # the corpus reversal script trades in this window
    assert s["recompute_ms_p99_settle"] >= 1 and s["recompute_ms_p99_probe"] >= 1
    assert len(doc["bars"]) == 30 and {b["g1"] for b in doc["bars"]} == {"ok"}
    assert doc["aborted_at"] is None and doc["reference_run"]["bars_checked"] == 30
    assert doc["reference_run"]["hash_mismatches"] == [] and doc["reference_run"]["prefix_mismatches"] == []
    assert [b for b in doc["bars"] if b["stop"] is not None] == []
    # n7/m1: "clean" is not just G1 -- the run must also have raised no
    # STOP, journaled no incident and classified every settlement fill
    # CONFIRMED. Without these the M1 repro (a double-filling venue) read
    # as a clean run while STOPped on three bars out of four.
    assert s["stops"] == 0 and s["incidents"] == 0 and s["non_confirmed"] == 0
    assert [b for b in doc["bars"] if b["incidents"] or b["non_confirmed"]] == []
    assert "probe_not_settled 0" in p.stdout and "g1_failures 0" in p.stdout
    assert "stops 0" in p.stdout and "non_confirmed 0" in p.stdout

def test_l1_harness_rejects_a_window_past_the_end_of_the_feed(tmp_path):
    """The window is validated against the feed BEFORE the engine is
    loaded (so this needs no `.so`): a start/bars pair the feed cannot
    cover exits non-zero with a message rather than seeding a short
    ledger and reporting a clean run over nothing."""
    feed = tmp_path / "feed.csv"
    feed.write_text("timestamp,open,high,low,close,volume\n" +
                    "".join(f"{1_577_836_800_000 + i * 900_000},1,2,0.5,1.5,10\n" for i in range(10)))
    p = _run("--so", tmp_path / "nonexistent.dylib", "--feed", feed, "--start", 5, "--bars", 20)
    assert p.returncode != 0 and "feed" in (p.stdout + p.stderr)


# --- the harness's own pure parts (no engine needed) --------------------------

def _module():
    """`scripts/l1_harness.py` imported as a module (it is a script, not a
    package member). Registered in `sys.modules` BEFORE execution because
    `@dataclass` resolves a field's annotations through
    `sys.modules[cls.__module__]`, which is `None` for a module that was
    only ever `exec_module`d."""
    import importlib.util
    if "l1_harness" in sys.modules:
        return sys.modules["l1_harness"]
    spec = importlib.util.spec_from_file_location("l1_harness", HARNESS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["l1_harness"] = m
    spec.loader.exec_module(m)
    return m

def test_percentile_is_nearest_rank_and_never_rounds_a_latency_down():
    h = _module()
    assert h.percentile([], 0.99) == 0
    assert h.percentile([5], 0.99) == 5
    v = list(range(1, 101))
    assert h.percentile(v, 0.50) == 50 and h.percentile(v, 0.99) == 99 and h.percentile(v, 1.0) == 100
    # nearest-rank, not interpolated: the reported p99 is a sample that
    # actually happened, and never below one.
    assert h.percentile([1, 1, 1, 40], 0.99) == 40

def test_perfect_venue_echo_and_signed_position(tmp_path):
    """Controller ruling 1's venue model, pinned: every fill-producing
    action comes back as a fill on the bar it was requested on, carrying
    the action's OWN `target_bar_index` (the field the classifier
    identity-matches on -- rewriting it would hide the mismatch the
    harness exists to find); a `MARKET_AT_OPEN` fills at the bar's open, a
    `TRIGGER` at the probe's price; a cancel is not a fill; and the
    account position moves by the signed qty of exactly those fills."""
    h = _module()
    from pineforge_live import types as T
    from pineforge_live.core.live import ActionRequest
    bar = T.NormalizedBar(1_577_836_800_000, 10.0, 12.0, 9.0, 11.0, 100.0, 0)
    actions = [
        ActionRequest("MARKET_AT_OPEN", "L", T.Side.BUY, 2.0, None, False, "MARKET", "settled market", 7),
        ActionRequest("TRIGGER", "S", T.Side.SELL, 1.0, 11.5, True, "TRIGGER", "probe fill", 7),
        ActionRequest("CANCEL_STALE_CYCLE", "X", T.Side.BUY, 0.0, None, False, "CANCEL", "gone", 7),
    ]
    fills = h.echo(h.supersede(actions), bar, 7)
    assert [(f.intent, f.leg, f.side, f.qty, f.price, f.target_bar_index, f.cause, f.executed_trigger) for f in fills] == [
        ("L", "ENTRY", T.Side.BUY, 2.0, bar.o, 7, T.FillCause.OURS, False),
        ("S", "EXIT", T.Side.SELL, 1.0, 11.5, 7, T.FillCause.OURS, True)]
    assert signed_qty(fills) == 1.0                     # +2 bought, -1 sold: the account moves by exactly this


def test_the_venue_fills_only_the_last_request_per_supersede_key():
    """M1 / `ActionRequest`'s supersede contract: a later request for the
    same `(intent, leg, target_bar_index)` REPLACES the earlier one, so
    the perfect venue fills the LAST one and only that one.

    The producer in the real cadence is spec §4 settle 6's MARKET leg:
    `settle(n)` asks for it in advance off bar n's close, and the first
    `evaluate()` of bar n+1 re-quotes it at the open with the engine's own
    qty (`reason="open_requote"`). A venue that filled both would double
    every entry and reversal -- which is exactly what a run over bars
    2088-2091 did before this: six fills for two legs, and `STOP(FLAT_ONLY,
    unreconcilable_sides)` on three bars of four."""
    h = _module()
    from pineforge_live import types as T
    from pineforge_live.core.live import ActionRequest
    bar = T.NormalizedBar(1_577_836_800_000, 10.0, 12.0, 9.0, 11.0, 100.0, 0)
    advance = ActionRequest("MARKET_AT_OPEN", "L", T.Side.BUY, 2.0, None, False, "MARKET", "settled market", 8)
    requote = ActionRequest("MARKET_AT_OPEN", "L", T.Side.BUY, 3.0, None, False, "MARKET", "open_requote", 8)
    other = ActionRequest("MARKET_AT_OPEN", "L", T.Side.SELL, 1.0, None, True, "MARKET", "close leg", 8)
    kept = h.supersede([advance, requote, other])
    assert [(a.qty, a.reason, a.reduce_only) for a in kept] == [(3.0, "open_requote", False), (1.0, "close leg", True)]
    assert h.supersede([advance, requote, other]) == kept                 # pure
    fills = h.echo(kept, bar, 8)
    assert [(f.intent, f.leg, f.qty) for f in fills] == [("L", "ENTRY", 3.0), ("L", "EXIT", 1.0)]
    assert signed_qty(fills) == 2.0                     # +3 - 1: the advance notice is NOT filled a second time
    # a different target bar is a different order, never superseded
    later = ActionRequest("MARKET_AT_OPEN", "L", T.Side.BUY, 5.0, None, False, "MARKET", "next bar", 9)
    assert len(h.supersede([advance, requote, later])) == 2


# --- ruling 2 / M2: probe_not_settled is tick-ordered -------------------------

def _pr(fills, retracted):
    """A `ProbeResult` stand-in: `fold_probe` reads `.fills`/`.retracted`
    only, so one tick's outcome can be stated directly instead of driving
    the engine to a state that happens to produce it."""
    return SimpleNamespace(fills=list(fills), retracted=list(retracted))

def _pf(intent="L", leg="ENTRY", is_long=True, path_variant=False):
    from pineforge_live.core.probe import ProbeFill
    return ProbeFill(intent, leg, is_long, 1.0, 100.0, 5, 5, path_variant=path_variant)

def test_probe_not_settled_reads_the_last_state_of_each_sig_not_a_bar_wide_union():
    """M2 / ruling 2: R2 says "retracted by a LATER evaluate". `Probe.evaluate`
    reports a retraction as `prev_map - cur_map`, so fill -> retract ->
    fill is a legal tick sequence; the fill STANDS at the close of the bar
    and must be settled. A bar-wide `retract_sigs` union excused it, which
    is a false negative on the single assertion the harness exists for."""
    h = _module()
    x = _pf()
    live = h.fold_probe([_pr([x], []), _pr([], [x]), _pr([x], [])])
    assert live.owed(settled=set()) == {x.sig}                 # fill -> retract -> fill: still owed
    assert live.owed(settled={x.sig}) == set()                 # unless the settlement produced it
    gone = h.fold_probe([_pr([x], []), _pr([], [x])])
    assert gone.owed(settled=set()) == set()                   # fill -> retract: nothing owed
    # the union tallies stay unions: both bars saw one fill and (for the
    # retracted one) one retract, and that is what the summary counts.
    assert (live.fills, live.retracts) == ({x.sig}, {x.sig})
    assert (gone.fills, gone.retracts) == ({x.sig}, {x.sig})

def test_path_variant_probe_fills_are_reported_separately_not_as_probe_not_settled():
    """m2 / spec §4 evaluate 2: a probe fill is "confirmed on both paths";
    a `path_variant` fill is the deliberate exception -- kept because it
    CLOSES a cycle, and at settlement it is a `PATH_DIVERGENT`
    classification (the same cycle closed via a different leg), not an L1
    failure. Counting it as `probe_not_settled` is a false positive on
    every bracket-heavy probe."""
    h = _module()
    v = _pf(intent="XL", leg="EXIT", is_long=True, path_variant=True)
    t = h.fold_probe([_pr([v], [])])
    assert t.path_variants == {v.sig} and t.fills == {v.sig}
    assert t.owed(settled=set()) == set()


def _revised_feed(src: Path, dst: Path, n_bars: int, revise_index: int) -> Path:
    """`src`'s first `n_bars` bars with bar `revise_index`'s close (and, so
    the bar stays well-formed, its high) moved -- a *revised settled bar*,
    which is what spec §4.1 calls a feed problem."""
    rows = src.read_text().splitlines()
    head, body = rows[0], rows[1:1 + n_bars]
    f = body[revise_index].split(",")
    c = float(f[4]) + 1.0
    f[4] = repr(c); f[2] = repr(max(float(f[2]), c))
    body[revise_index] = ",".join(f)
    dst.write_text("\n".join([head, *body]) + "\n")
    return dst

def test_a_stop_is_reported_json_serialisable_and_exits_1(test_so, test_feed, tmp_path):
    """M3 + m1: a STOP anywhere in the window must reach the operator.

    `row["stop"]` holds `types.StopLevel`/`StopDisposition` members, which
    are plain `enum.Enum` (no `str` mixin), so `json.dumps` raised
    `TypeError` *before* the summary line printed -- the operator got
    neither the JSON nor the `l1:` line on precisely the runs whose report
    matters. And the brief's exit contract (g1 + probe only) returned 0
    while STOPped. Both are pinned here.

    Forcing the STOP: settle bar 2000 into a journal, then re-run the same
    window over a feed whose bar 2000 has been revised. The seed replays
    bars[:2000] unchanged (so it still agrees with its own journaled row),
    and the revised bar 2000 then conflicts with the `bars` row already
    written for that `ts_open` -> `BarsDivergence` -> `STOP(FLAT_ONLY,
    NONE, "bars_divergence")`. `STOP(HARD, HOLD)` is not reachable from
    outside the harness: every journal edit that would fail `settle()`'s
    G1 check against bar n-1 is the same row `seed()` pre-checks, so it
    fails at the seed instead, before any bar is settled."""
    jdir = tmp_path / "j"
    first = _run("--so", test_so, "--feed", test_feed, "--start", 2000, "--bars", 1, "--journal", jdir)
    assert first.returncode == 0, first.stdout + first.stderr

    revised = _revised_feed(Path(test_feed), tmp_path / "revised.csv", 2001, 2000)
    out = tmp_path / "stopped.json"
    p = _run("--so", test_so, "--feed", revised, "--start", 2000, "--bars", 1, "--journal", jdir, "--out", out)
    assert p.returncode == 1, p.stdout + p.stderr
    doc = json.loads(out.read_text())                       # M3: this is the assertion -- it used to raise TypeError
    assert doc["bars"][0]["stop"] == ["FLAT_ONLY", "NONE", "bars_divergence"]
    assert doc["bars"][0]["incidents"] == ["bars_divergence"]
    assert doc["summary"]["stops"] == 1
    assert set(doc["summary"]) == SUMMARY_KEYS
    assert "l1: bars 1" in p.stdout                          # the summary line still printed
    assert "STOP" in p.stdout and "bars_divergence" in p.stdout


def test_l1_harness_on_the_pooc_probe_has_no_missed_fill(test_so_pooc, test_feed, tmp_path):
    """M5, spec §4 settle 6: "`process_orders_on_close` fills -> MARKET
    now" had no code at all. `probe_suppress_tail_logic` means no probe
    ever sees a POOC fill, so the settlement emulated it with an order
    that never rested in the book and the RECONCILER repaired it as
    MISSED: 4 of 200 bars on this fixture -- a 2% steady state, straight
    through the spec's own 1% orphan+missed breaker -- each one gated
    behind the `[r4]` age/distance/budget bound the spec does not put on a
    POOC fill, and a same-bar POOC reversal would have read
    `unreconcilable_sides` on the first cross.

    The fixture is `order-deferred-flip-pooc-cross-bar-01`
    (`process_orders_on_close=true`: the `strategy.close` fires at the
    cross bar's own close while the stop entry waits for the next open).
    Same 15m ETH-USDT feed and `"TAPE"` syminfo as the other two probes --
    its directory carries no `inputs.json` at all. 200 bars from 2000,
    the same window as the release-criterion runs."""
    out = tmp_path / "pooc.json"
    p = _run("--so", test_so_pooc, "--feed", test_feed, "--start", 2000, "--bars", 200,
             "--journal", tmp_path / "j", "--out", out)
    assert p.returncode == 0, p.stdout + p.stderr
    doc = json.loads(out.read_text())
    s = doc["summary"]
    assert s["bars"] == 200 and s["g1_failures"] == 0 and s["probe_not_settled"] == 0 and s["stops"] == 0
    assert s["incidents"] == 0
    classes = [cls for b in doc["bars"] for cls in b["non_confirmed"]]
    assert classes and set(classes) == {"SETTLE_ONLY"}, classes      # zero MISSED over the window
    pooc_bars = [b for b in doc["bars"] if b["non_confirmed"]]
    assert len(pooc_bars) == 4                                       # the four POOC closes in this window
    for b in pooc_bars:
        assert [(a["kind"], a["reduce_only"], a["target_bar_index"]) for a in b["actions"]] == \
               [("MARKET_NOW", True, b["bar_index"])]
