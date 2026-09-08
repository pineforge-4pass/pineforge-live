#!/usr/bin/env python3
"""L1 harness (spec §10.2): the full settle/evaluate cadence over a tape.

Runs the real `LiveCore` over a recorded feed exactly as a driver would --
`seed(history)` once, then for every bar `evaluate(forming)` at the tape's
k = 4 probe points (1/5/10/14 minutes of a 15m bar) and `settle(bar)` at
the close -- against a PERFECT VENUE, and reports what the two L1
assertions saw:

* **G1 (prefix stability, spec §1).** Every settlement re-checks the
  previous bar's hash/trades/bars digests against the journal (the
  ledger's own check, `STOP(HARD, HOLD)` on failure), and this harness
  re-reads the journaled `broker_state_hash` for bar n−1 independently
  after each settle. At the end the FINAL run stands in for spec §10.2's
  reference run `R`: `R.hash[m]` and `R`'s trade prefix at `m` are
  compared against what every `L_m` journaled at the time.
* **probe ≡ recompute (spec §10.2).** Every probe fill must be either a
  fill of that bar's own settlement (same `(intent, leg, is_long)`) or
  retracted by a later `evaluate()` on the same bar. Anything else is
  counted as `probe_not_settled`.

The exit code is the verdict: 0 only when `g1_failures` and
`probe_not_settled` are both 0.

**The perfect venue.** It echoes every fill-producing action back as a
fill on the bar it was requested on -- a `TRIGGER` at the probe's own
price, a settled `MARKET_AT_OPEN` leg at the bar's open -- and reports the
account position event-time AFTER those fills (`prev + Σ signed qty`,
never the pre-settle ledger position, which is what spec §5.4 means by "position
snapshot event-time after the last fill"). `our_signed_fills` is left for
`LiveCore` to derive, so the harness exercises that derivation rather than
feeding it the answer. A cancel is not a fill and is never echoed. The
venue is named `"TAPE"` throughout: no real exchange name appears here.

Usage:

    PINEFORGE_ENGINE_ROOT=~/code/pineforge-engine-wt/main python3 scripts/l1_harness.py \
        --so   $PINEFORGE_ENGINE_ROOT/corpus/validation/ta-sma-152-close-cross-01/strategy.dylib \
        --feed $PINEFORGE_ENGINE_ROOT/corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv \
        --start 2000 --bars 200 --out build/l1.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runnable from a checkout, installed or not

from pineforge_live import types as T                                              # noqa: E402
from pineforge_live.adapters.tape import TapeTickSource, load_feed_csv             # noqa: E402
from pineforge_live.bars import FormingBarBuilder                                  # noqa: E402
from pineforge_live.bars.policy import tf_ms                                       # noqa: E402
from pineforge_live.core.classify import VenueFill, emulated_from_settle           # noqa: E402
from pineforge_live.core.ids import keys_sha256                                    # noqa: E402
from pineforge_live.core.ledger import BarsDivergence, LedgerDivergence, LedgerGap  # noqa: E402
from pineforge_live.core.live import LiveCore                                      # noqa: E402
from pineforge_live.core.reconcile import DeadBand                                 # noqa: E402
from pineforge_live.core.riskguard import Breaker, RiskLimits, n_min_for           # noqa: E402
from pineforge_live.epoch import RuntimeConfig                                     # noqa: E402
from pineforge_live.harness import make_handle, open_journal, tape_spec            # noqa: E402

#: Deliberately wide: an L1 run measures G1 and probe-equivalence, not risk
#: policy, and a budget refusal would silently change the action stream the
#: two assertions are about. `max_fill_actions_per_bar` is the one that
#: stays meaningful (spec §4: "≤ P's fill count").
LIMITS = RiskLimits(1e6, 1e12, 1e9, 8, 32, 1e9, 50, 60_000, 60_000, 3, 2, 2.0, 1.0, 5_000)
DEAD_BAND = DeadBand(0.001, 0.001, 5.0)
BREAKERS = [Breaker("orphan", 0.01, 500, n_min_for(0.01), 5)]
RUNTIME_CONFIG = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000, grace_ms=3_000,
                              open_wait_ms=2_000, risk_limits={})
#: A cancel carries no qty and produces no fill; every other action kind does.
NON_FILL_ACTIONS = frozenset({"CANCEL_STALE_CYCLE"})


def percentile(values: list[int], q: float) -> int:
    """Nearest-rank percentile (no interpolation): the smallest sample at
    or above `q` of the way through the sorted values. Reported as-is in
    the summary because these numbers feed spec §2's `grace ≥
    recompute_p99 + submit_p99` -- rounding a latency budget DOWN by
    interpolating between two samples is the wrong direction to be wrong
    in."""
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def ticks_for(bar: T.NormalizedBar, tf: str, policy: str, seed: int, instrument) -> list[T.NormalizedTick]:
    """The tape's ticks for one bar under `policy` (`path4` prints exactly
    at the k = 4 probe points spec §10.2 names)."""
    src = TapeTickSource([bar], tf, policy=policy, seed=seed)

    async def go():
        return [e.tick async for e in src.subscribe(instrument, 0) if isinstance(e, T.Tick)]

    return asyncio.run(go())


def echo(actions, bar: T.NormalizedBar, bar_index: int) -> list[VenueFill]:
    """The perfect venue's fills for one bar's requested actions.

    Each action's own `target_bar_index` is carried through verbatim
    rather than rewritten to this bar: that field is what
    `classify.classify_bar` identity-matches on, so faking it would hide
    exactly the mismatch this harness exists to find. A `MARKET_AT_OPEN`
    fills at the bar's open (spec §4 settle 6: the open IS the price, no
    fallback); a `TRIGGER` at the probe's own price; anything else at the
    close."""
    fills = []
    for k, a in enumerate(actions):
        if a.kind in NON_FILL_ACTIONS or a.qty <= 0.0:
            continue
        price = bar.o if a.kind == "MARKET_AT_OPEN" else (a.price_hint if a.price_hint is not None else bar.c)
        fills.append(VenueFill(a.intent, "EXIT" if a.reduce_only else "ENTRY", a.side, a.qty, price,
                               a.target_bar_index, T.FillCause.OURS, f"cid-{bar_index}-{k}", a.kind == "TRIGGER"))
    return fills


def signed(fills: list[VenueFill]) -> float:
    return sum((f.qty if f.side is T.Side.BUY else -f.qty) for f in fills)


def sig(x) -> list:
    """A fill's identity as JSON: `(intent, leg, is_long)` -- the tuple
    `ProbeFill.sig` uses and `EmulatedFill` carries field by field, which
    is what "the probe's fill IS this settlement's fill" means."""
    return [x.intent, x.leg, x.is_long]


def run(args) -> tuple[dict, int]:
    """Run the cadence and return `(report, exit_code)`."""
    feed = load_feed_csv(args.feed, args.start + args.bars)
    if len(feed) < args.start + args.bars:
        raise SystemExit(f"feed {args.feed} holds {len(feed)} bars, need {args.start + args.bars} "
                         f"(--start {args.start} + --bars {args.bars})")
    if args.start < 1 or args.bars < 1:
        raise SystemExit("--start must be >= 1 (the ledger needs a history to seed from) and --bars >= 1")

    spec = tape_spec(args.tf, so=args.so)
    handle = make_handle(args.so, spec)
    journal, marker = open_journal(args.journal_dir)
    core = LiveCore(handle, spec, journal, marker, RUNTIME_CONFIG, LIMITS, DEAD_BAND, BREAKERS)

    seeded = core.seed(feed[:args.start])
    if seeded.settle is None:
        raise SystemExit(f"seed failed: {seeded.incidents}")
    # The venue starts holding what the seed adopted -- an always-in-market
    # corpus script is never flat, and an account that disagreed with the
    # ledger at bar 0 would be an `account_mismatch` STOP on bar 1 that has
    # nothing to do with G1.
    real_position = seeded.settle.position_size
    prev_book, owed = seeded.book, []

    bars_out, settle_ms, probe_ms_all, g1_failed = [], [], [], set()
    settled_indices: list[int] = []
    aborted_at = None

    for i in range(args.start, args.start + args.bars):
        bar = feed[i]
        builder = FormingBarBuilder(spec.script_tf)
        probe_sigs, retract_sigs, bar_probe_ms = set(), set(), []
        pending = list(owed)
        for tick in ticks_for(bar, spec.script_tf, args.policy, args.seed, spec.instrument):
            builder.push(tick)
            ev = core.evaluate(builder.forming(), now_ms=tick.ts)
            bar_probe_ms.append(ev.probe.recompute_ms)
            probe_sigs |= {f.sig for f in ev.probe.fills}
            retract_sigs |= {f.sig for f in ev.probe.retracted}
            pending += ev.actions

        venue = echo(pending, bar, i)
        real_position += signed(venue)
        row = {"bar_index": i, "ts_open": bar.ts_open, "g1": "ok", "settle_recompute_ms": None,
               "probe_recompute_ms": bar_probe_ms, "probe_fills": sorted(probe_sigs), "retracts": sorted(retract_sigs),
               "settle_fills": [], "probe_not_settled": [], "actions": [], "stop": None,
               "incidents": [], "venue_fills": len(venue), "real_position": real_position}
        probe_ms_all += bar_probe_ms
        try:
            out = core.settle(bar, venue_fills=venue, in_flight=set(), mirrored=set(),
                              real_position=real_position, now_ms=bar.ts_open + tf_ms(spec.script_tf))
        except (LedgerDivergence, BarsDivergence, LedgerGap) as e:
            # Ruling 3: record it and go to the summary. The ledger did not
            # advance, so every later bar would be a gap -- there is nothing
            # left to measure after this.
            row["g1"] = f"raised:{type(e).__name__}:{e}"
            g1_failed.add(i); bars_out.append(row); aborted_at = i
            break

        row["incidents"] = [x["kind"] for x in out.incidents]
        row["stop"] = list(out.stop[:2]) + [out.stop[2]] if out.stop else None
        if out.settle is None:
            # LiveCore swallows a divergence into a STOP + incident rather
            # than raising; same conclusion as the except branch above.
            row["g1"] = "diverged:" + ",".join(row["incidents"])
            if {"ledger_divergence", "bars_divergence"} & set(row["incidents"]):
                g1_failed.add(i)
            bars_out.append(row); aborted_at = i
            break

        s = out.settle
        settled_indices.append(s.bar_index)
        settle_ms.append(s.recompute_ms)
        row["settle_recompute_ms"] = s.recompute_ms
        # An independent read-back of the journal's own record for bar n-1
        # (the ledger checked the same thing before journaling; this proves
        # what LANDED, not just what the run computed).
        prev_row = journal.settlement(spec.epoch_hash(), s.bar_index - 1)
        if prev_row is None or int(prev_row["broker_state_hash"]) != s.hashes[s.bar_index - 1]:
            row["g1"] = "journal_hash_mismatch"
            g1_failed.add(i)

        settled = emulated_from_settle(s, out.book_diff, prev_book)
        settled_sigs = {(e.intent, e.leg, e.is_long) for e in settled}
        row["settle_fills"] = [sig(e) for e in settled]
        row["actions"] = [{"kind": a.kind, "intent": a.intent, "qty": a.qty, "reduce_only": a.reduce_only,
                           "target_bar_index": a.target_bar_index} for a in out.actions]
        # Ruling 2 / spec §10.2: probe fills ⊆ settlement fills ∪ PROBE_RETRACT.
        row["probe_not_settled"] = sorted(list(p) for p in (probe_sigs - settled_sigs - retract_sigs))
        bars_out.append(row)

        prev_book = out.book
        owed = [a for a in out.actions if a.kind not in NON_FILL_ACTIONS]

    # Spec §10.2's reference run: the LAST recompute covers every settled
    # bar, so its own hash vector and trade prefixes are checked against
    # what each L_m journaled at the time -- one pass, no extra recompute.
    reference = {"bars_checked": 0, "hash_mismatches": [], "prefix_mismatches": []}
    final = core.ledger.last
    if final is not None:
        for m in settled_indices:
            reference["bars_checked"] += 1
            jr = journal.settlement(spec.epoch_hash(), m)
            if jr is None or int(jr["broker_state_hash"]) != final.hashes[m]:
                reference["hash_mismatches"].append(m); g1_failed.add(m)
                continue
            if keys_sha256([k for k in final.keys if k.exit_bar <= m]) != jr["trades_sha256"]:
                reference["prefix_mismatches"].append(m); g1_failed.add(m)
    journal.close()

    summary = {
        "bars": len(bars_out),
        "settle_fills": sum(len(b["settle_fills"]) for b in bars_out),
        "probe_fills": sum(len(b["probe_fills"]) for b in bars_out),
        "retracts": sum(len(b["retracts"]) for b in bars_out),
        "probe_not_settled": sum(len(b["probe_not_settled"]) for b in bars_out),
        "g1_failures": len(g1_failed),
        "recompute_ms_p99_settle": percentile(settle_ms, 0.99),
        "recompute_ms_p99_probe": percentile(probe_ms_all, 0.99),
    }
    report = {
        "config": {"so": str(args.so), "feed": str(args.feed), "start": args.start, "bars": args.bars,
                   "tf": spec.script_tf, "policy": args.policy, "seed": args.seed, "venue": spec.venue,
                   "epoch_hash": spec.epoch_hash()},
        "summary": summary,
        "recompute_ms": {
            "settle": {"n": len(settle_ms), "p50": percentile(settle_ms, 0.50),
                       "p99": percentile(settle_ms, 0.99), "max": max(settle_ms, default=0)},
            "probe": {"n": len(probe_ms_all), "p50": percentile(probe_ms_all, 0.50),
                      "p99": percentile(probe_ms_all, 0.99), "max": max(probe_ms_all, default=0)},
        },
        "reference_run": reference,
        "aborted_at": aborted_at,
        "bars": bars_out,
    }
    failed = summary["g1_failures"] or summary["probe_not_settled"] or aborted_at is not None
    return report, (1 if failed else 0)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="L1 harness: G1 + probe ≡ recompute over a tape (spec §10.2)")
    p.add_argument("--so", required=True, type=Path, help="compiled strategy library (.so/.dylib)")
    p.add_argument("--feed", required=True, type=Path, help="timestamp,open,high,low,close,volume CSV")
    p.add_argument("--start", type=int, default=2000, help="first bar index to run the cadence on; bars[:start] seed the ledger")
    p.add_argument("--bars", type=int, default=200, help="how many bars to run the full cadence over")
    p.add_argument("--tf", default="15", help="script timeframe of the feed (default 15)")
    p.add_argument("--policy", default="path4", help="tape tick policy (default path4: the k=4 probe points)")
    p.add_argument("--seed", type=int, default=1, help="tick-source seed (only the random policies read it)")
    p.add_argument("--journal", dest="journal_dir", type=Path, default=None,
                   help="directory for this run's journal (default: a temporary one, discarded)")
    p.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    tmp = None
    if args.journal_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="l1-harness-")
        args.journal_dir = Path(tmp.name)
    else:
        args.journal_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, code = run(args)
    finally:
        if tmp is not None:
            tmp.cleanup()

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")
    s = report["summary"]
    print("l1: " + " ".join(f"{k} {v}" for k, v in s.items()))
    print("l1: recompute_ms settle p50 {p50} p99 {p99} max {max} (n {n})".format(**report["recompute_ms"]["settle"]))
    print("l1: recompute_ms probe  p50 {p50} p99 {p99} max {max} (n {n})".format(**report["recompute_ms"]["probe"]))
    if report["aborted_at"] is not None:
        print(f"l1: ABORTED at bar {report['aborted_at']}: {report['bars'][-1]['g1']}")
    for b in report["bars"]:
        if b["probe_not_settled"]:
            print(f"l1: bar {b['bar_index']} probe fills neither settled nor retracted: {b['probe_not_settled']}")
        if b["g1"] != "ok":
            print(f"l1: bar {b['bar_index']} G1: {b['g1']}")
    for m in report["reference_run"]["hash_mismatches"]:
        print(f"l1: reference run disagrees with journaled hash at bar {m}")
    for m in report["reference_run"]["prefix_mismatches"]:
        print(f"l1: reference run disagrees with journaled trade prefix at bar {m}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
