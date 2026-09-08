"""`scripts/l1_harness.py` end to end (spec §10.2 L1, in miniature): the
script is run as a subprocess over a short window and its report read
back, so the harness the operator actually types is the thing under test
-- exit code, the summary line on stdout, and the JSON contract."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "l1_harness.py"
SUMMARY_KEYS = {"bars", "settle_fills", "probe_fills", "retracts", "probe_not_settled", "g1_failures",
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
    assert "probe_not_settled 0" in p.stdout and "g1_failures 0" in p.stdout

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
    import importlib.util
    spec = importlib.util.spec_from_file_location("l1_harness", HARNESS)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
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
    fills = h.echo(actions, bar, 7)
    assert [(f.intent, f.leg, f.side, f.qty, f.price, f.target_bar_index, f.cause, f.executed_trigger) for f in fills] == [
        ("L", "ENTRY", T.Side.BUY, 2.0, bar.o, 7, T.FillCause.OURS, False),
        ("S", "EXIT", T.Side.SELL, 1.0, 11.5, 7, T.FillCause.OURS, True)]
    assert h.signed(fills) == 1.0                       # +2 bought, -1 sold: the account moves by exactly this
