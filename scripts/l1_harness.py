#!/usr/bin/env python3
"""L1 harness (spec §10.2): the full settle/evaluate cadence over a tape.

Runs the real `LiveCore` over a recorded feed exactly as a driver would --
`seed(history)` once, then for every bar `evaluate(forming)` at the tape's
k = 4 probe points (1/5/10/14 minutes of a 15m bar) and `settle(bar)` at
the close -- against a PERFECT VENUE, and reports what the two L1
assertions saw:

* **G1 (prefix stability, spec §1).** Every settlement re-checks the
  previous bar's hash/trades/bars digests against the journal (the
  ledger's own check, `STOP(HARD, HOLD)` on failure). At the end the FINAL
  run stands in for spec §10.2's reference run `R`: `R.hash[m]` and `R`'s
  trade prefix at `m` are compared against what every `L_m` journaled at
  the time. That end-of-run pass is the INDEPENDENT check -- the per-bar
  journal read-back this harness also does is not, since `Ledger.settle`
  compares the same row before journaling and raises otherwise, so it can
  only ever agree.
* **probe ≡ recompute (spec §10.2).** Every probe fill must be either a
  fill of that bar's own settlement (same `(intent, leg, is_long)`) or
  retracted by a LATER `evaluate()` on the same bar. Anything else is
  counted as `probe_not_settled`. "Later" is read in tick order: fill ->
  retract -> fill leaves the fill STANDING at the close and it must be
  settled (see `fold_probe`). Path-variant fills are excluded -- a fill
  only `P_auto` confirmed is by definition not both-path-confirmed and
  settles as `PATH_DIVERGENT`, a classification rather than an L1 failure.

The exit code is the verdict: 0 only when `g1_failures`,
`probe_not_settled` and `stops` are all 0. A STOP mid-window refuses
actions and silently changes the very stream the two assertions are
about, so it can never be a passing run.

**The perfect venue.** It echoes every fill-producing action back as a
fill on the bar it was requested on -- a `TRIGGER` at the probe's own
price, a settled `MARKET_AT_OPEN` leg at the bar's open -- and reports the
account position event-time AFTER those fills (`prev + Σ signed qty`,
never the pre-settle ledger position, which is what spec §5.4 means by
"position snapshot event-time after the last fill"). It honours
`ActionRequest`'s SUPERSEDE contract first (`supersede`): a later request
for the same `(intent, leg, target_bar_index)` replaces the earlier one,
so the open re-quote `evaluate()` emits for a MARKET leg the preceding
settlement asked for in advance is filled ONCE, not twice.
`our_signed_fills` is left for `LiveCore` to derive, so the harness
exercises that derivation rather than feeding it the answer. A cancel is
not a fill and is never echoed. The venue is named `"TAPE"` throughout: no
real exchange name appears here.

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
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runnable from a checkout, installed or not

from pineforge_live import types as T                                              # noqa: E402
from pineforge_live.adapters.tape import TapeTickSource, load_feed_csv             # noqa: E402
from pineforge_live.bars import FormingBarBuilder                                  # noqa: E402
from pineforge_live.bars.policy import tf_ms                                       # noqa: E402
from pineforge_live.core.classify import FillClass, VenueFill                      # noqa: E402
from pineforge_live.core.ids import keys_sha256                                    # noqa: E402
from pineforge_live.core.ledger import BarsDivergence, LedgerDivergence            # noqa: E402
from pineforge_live.core.live import LiveCore, signed_qty                          # noqa: E402
from pineforge_live.core.reconcile import DeadBand                                 # noqa: E402
from pineforge_live.core.riskguard import Breaker, RiskLimits, n_min_for           # noqa: E402
from pineforge_live.epoch import RuntimeConfig                                     # noqa: E402
from pineforge_live.harness import make_handle, open_journal, tape_spec            # noqa: E402

#: Deliberately wide: an L1 run measures G1 and probe-equivalence, not risk
#: policy, and a budget refusal would silently change the action stream the
#: two assertions are about. `max_fill_actions_per_bar` is the one that
#: stays meaningful (spec §4: "≤ P's fill count") -- named here so that
#: claim is checkable rather than positional.
LIMITS = RiskLimits(max_abs_position=1e6, max_notional=1e12, max_order_notional=1e9,
                    max_fill_actions_per_bar=8, max_book_ops_per_bar=32,
                    max_daily_realized_loss=1e9, max_daily_reconciles=50,
                    stale_feed_ms=60_000, stale_eval_ms=60_000, bar_mismatch_streak=3,
                    disagree_twice=2, unexplained_divergence_pct=2.0,
                    liquidation_distance_pct_min=1.0, recompute_ms_p99_max=5_000)
DEAD_BAND = DeadBand(0.001, 0.001, 5.0)
#: m2: a breaker's name IS the reconciler counter it watches, and
#: `BreakerTable.self_test` now refuses one that names anything outside
#: `reconcile.COUNTER_NAMES`. `missed` is the spec's orphan+missed
#: numerator (theta = 1%); on a clean tape it never fires, which is the
#: point -- a run where it does is not a clean cadence.
BREAKERS = [Breaker("missed", 0.01, 500, n_min_for(0.01), 5)]
RUNTIME_CONFIG = RuntimeConfig(poll_interval_ms=30_000, drain_bound_ms=5_000, grace_ms=3_000,
                              open_wait_ms=2_000, risk_limits={})
#: A cancel carries no qty and produces no fill; every other action kind does.
NON_FILL_ACTIONS = frozenset({"CANCEL_STALE_CYCLE"})
#: The divergences that leave the ledger un-advanced, so every later bar is
#: a `LedgerGap` by construction and there is nothing left to measure.
FATAL_INCIDENTS = frozenset({"ledger_divergence", "bars_divergence"})


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (no interpolation): the smallest sample at
    or above `q` of the way through the sorted values. Reported as-is in
    the summary because these numbers feed spec §2's `grace ≥
    recompute_p99 + submit_p99` -- rounding a latency budget DOWN by
    interpolating between two samples is the wrong direction to be wrong
    in. Milliseconds are FLOATS (`Ledger._run`/`Probe.evaluate` measure to
    1 us): a corpus recompute runs in single-digit ms and a probe run can
    be sub-ms, so an integer read floors a real p99 to 0."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def ticks_for(runner: asyncio.Runner, bar: T.NormalizedBar, tf: str, policy: str, seed: int,
              instrument) -> list[T.NormalizedTick]:
    """The tape's ticks for one bar under `policy` (`path4` prints exactly
    at the k = 4 probe points spec §10.2 names). One event loop is shared
    by the whole run (`runner`) rather than built and torn down per bar."""
    src = TapeTickSource([bar], tf, policy=policy, seed=seed)

    async def go():
        return [e.tick async for e in src.subscribe(instrument, 0) if isinstance(e, T.Tick)]

    return runner.run(go())


def supersede(actions: list) -> list:
    """The requests a venue should actually hold, per `ActionRequest`'s
    SUPERSEDE contract: a later request for the same `(intent, leg,
    target_bar_index)` REPLACES the earlier one, so only the last per key
    survives -- in the order the key was first seen.

    The producer in this cadence is spec §4 settle 6's MARKET leg:
    `settle(n)` requests it in advance priced off the only price it has
    (bar n's close) and the first `evaluate()` of bar n+1 re-quotes it with
    the engine's qty at the OPEN (`reason="open_requote"`). They are ONE
    order. A venue that filled both would double every entry and reversal
    -- and the resulting position, being neither side's, reconciles as
    `unreconcilable_sides` -> `STOP(FLAT_ONLY)`.

    The key's `intent` component is the Pine order id, which is `None` for
    a `FLATTEN` (it has no single originating fill) and `"?"` for a
    correction whose delta could not be attributed. Two such requests on
    the SAME bar, same leg, same target bar would collapse onto one key
    here -- which is the right reading for this harness (`reconcile` emits
    at most one FLATTEN per decision, and a perfect venue never produces
    the `"?"` case), and is stated rather than assumed because a real
    executor keying orders this way needs to know it must fall back to its
    own client id for those two."""
    by_key: dict[tuple, object] = {}
    for a in actions:
        by_key[(a.intent, "EXIT" if a.reduce_only else "ENTRY", a.target_bar_index)] = a
    return list(by_key.values())


def echo(actions: list, bar: T.NormalizedBar, bar_index: int) -> list[VenueFill]:
    """The perfect venue's fills for one bar's `supersede`d actions.

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


@dataclass
class ProbeTally:
    """One bar's probe outcomes folded in TICK ORDER.

    `state` is the LAST thing each `(intent, leg, is_long)` was seen as --
    `"fill"` or `"retracted"` -- which is what ruling 2's "retracted by a
    LATER evaluate" means: fill -> retract -> fill leaves the fill standing
    at the close of the bar and it must be settled, while a bar-wide
    `retracted` union would excuse it. `fills`/`retracts` stay unions:
    they are the run's tallies of what the probe reported, not the
    assertion. `path_variants` are sigs some tick reported as
    `path_variant` (P_auto only, kept because they close a cycle) -- not
    both-path-confirmed, so outside the assertion (spec §4 evaluate 2)."""
    state: dict[tuple, str] = field(default_factory=dict)
    fills: set = field(default_factory=set)
    retracts: set = field(default_factory=set)
    path_variants: set = field(default_factory=set)

    def owed(self, settled: set) -> set:
        """The sigs the bar's settlement still owes: standing at the close,
        not produced by the settlement, not a path variant."""
        return {s for s, st in self.state.items() if st == "fill"} - settled - self.path_variants


def fold_probe(results) -> ProbeTally:
    """Fold one bar's `ProbeResult`s (in tick order) into a `ProbeTally`.
    Within one result `fills` and `retracted` are disjoint, so applying
    fills then retracted preserves the order the probe reported them in."""
    t = ProbeTally()
    for r in results:
        for f in r.fills:
            t.state[f.sig] = "fill"
            t.fills.add(f.sig)
            if f.path_variant:
                t.path_variants.add(f.sig)
        for f in r.retracted:
            t.state[f.sig] = "retracted"
            t.retracts.add(f.sig)
    return t


def sig(x) -> list:
    """A fill's identity as JSON: `(intent, leg, is_long)` -- the tuple
    `ProbeFill.sig` uses and `EmulatedFill` carries field by field, which
    is what "the probe's fill IS this settlement's fill" means."""
    return [x.intent, x.leg, x.is_long]


def action_json(a) -> dict:
    return {"kind": a.kind, "intent": a.intent, "qty": a.qty, "reduce_only": a.reduce_only,
            "target_bar_index": a.target_bar_index, "reason": a.reason}


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
    try:
        return _cadence(args, spec, handle, journal, marker, feed)
    finally:
        journal.close()


def _report(args, spec, bars_out, settle_ms, probe_ms_all, g1_failed, reference, aborted_at, seed_row) -> tuple[dict, int]:
    """The report and the exit code, from whatever the run got to.

    One builder for BOTH exits (the cadence's and a refused seed's), so
    `summary`'s key set is the same document either way -- an operator (and
    a test) reads the same fields off a run that never settled a bar as off
    a clean 200-bar window."""
    summary = {
        "bars": len(bars_out),
        "settle_fills": sum(len(b["settle_fills"]) for b in bars_out),
        "probe_fills": sum(len(b["probe_fills"]) for b in bars_out),
        "retracts": sum(len(b["retracts"]) for b in bars_out),
        "probe_not_settled": sum(len(b["probe_not_settled"]) for b in bars_out),
        "path_variant": sum(len(b["path_variant"]) for b in bars_out),
        "superseded": sum(b["superseded"] for b in bars_out),
        "stops": sum(1 for b in bars_out if b["stop"] is not None) + (1 if seed_row and seed_row["stop"] else 0),
        "incidents": sum(len(b["incidents"]) for b in bars_out) + len((seed_row or {}).get("incidents", [])),
        "non_confirmed": sum(len(b["non_confirmed"]) for b in bars_out),
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
                       "p99": percentile(settle_ms, 0.99), "max": max(settle_ms, default=0.0)},
            "probe": {"n": len(probe_ms_all), "p50": percentile(probe_ms_all, 0.50),
                      "p99": percentile(probe_ms_all, 0.99), "max": max(probe_ms_all, default=0.0)},
        },
        "reference_run": reference,
        "aborted_at": aborted_at,
        "seed": seed_row,
        "bars": bars_out,
    }
    # A STOP mid-window refuses actions and changes the very stream the two
    # assertions are about, so it can never be a passing run (m1).
    failed = (summary["g1_failures"] or summary["probe_not_settled"] or summary["stops"]
              or aborted_at is not None)
    return report, (1 if failed else 0)


def stop_json(stop) -> list | None:
    """A `CoreOutput.stop` as JSON: `types.StopLevel`/`StopDisposition` are
    plain `enum.Enum` (no `str` mixin), so the members themselves raise
    `TypeError` in `json.dumps` -- on precisely the runs whose report
    matters most."""
    return [stop[0].value, stop[1].value, stop[2]] if stop else None


def _cadence(args, spec, handle, journal, marker, feed) -> tuple[dict, int]:
    core = LiveCore(handle, spec, journal, marker, RUNTIME_CONFIG, LIMITS, DEAD_BAND, BREAKERS)

    empty_reference = {"bars_checked": 0, "hash_mismatches": [], "prefix_mismatches": []}
    seeded = core.seed(feed[:args.start])
    seed_row = {"stop": stop_json(seeded.stop), "incidents": [x["kind"] for x in seeded.incidents]}
    if seeded.settle is None:
        # A refused seed is a RESULT, not a usage error: `seed()` returns
        # the STOP it raised (a divergent recompute against the journal,
        # spec §4.1) and an operator needs that written down. Raising
        # SystemExit here printed a string and wrote no `--out` at all --
        # no summary line, no JSON, on the one run whose report is the
        # whole point.
        return _report(args, spec, [], [], [], set(), empty_reference, None, seed_row)
    # The venue starts holding what the seed adopted -- an always-in-market
    # corpus script is never flat, and an account that disagreed with the
    # ledger at bar 0 would be an `account_mismatch` STOP on bar 1 that has
    # nothing to do with G1.
    real_position = seeded.settle.position_size
    owed: list = []

    bars_out, settle_ms, probe_ms_all, g1_failed = [], [], [], set()
    settled_indices: list[int] = []
    aborted_at = None

    with asyncio.Runner() as runner:
        for i in range(args.start, args.start + args.bars):
            bar = feed[i]
            builder = FormingBarBuilder(spec.script_tf)
            results, bar_probe_ms = [], []
            pending = list(owed)
            # NEW-1: `evaluate()` raises its own STOPs (a `risk_violation`
            # from the per-bar fill budget) and journals its own incidents
            # (`action_refused_by_stop`, `risk_refused`,
            # `ambiguous_trigger_intent`). Reading only `settle()`'s
            # `CoreOutput` made every one of them invisible to `stops`,
            # `incidents` AND the exit code -- while the module docstring,
            # the README and docs/core.md all say a STOP anywhere in the
            # window exits non-zero. `probe_retract` is excluded: it has
            # its own `retracts` tally and is explicitly never a STOP.
            ev_stop, ev_incidents = None, []
            for tick in ticks_for(runner, bar, spec.script_tf, args.policy, args.seed, spec.instrument):
                builder.push(tick)
                ev = core.evaluate(builder.forming(), now_ms=tick.ts)
                bar_probe_ms.append(ev.probe.recompute_ms)
                results.append(ev.probe)
                pending += ev.actions
                ev_stop = ev_stop or ev.stop
                ev_incidents += [x["kind"] for x in ev.incidents if x["kind"] != "probe_retract"]
            probe = fold_probe(results)

            # M1: one order per supersede key reaches the venue.
            held = supersede(pending)
            venue = echo(held, bar, i)
            # The account moves by the CORE's own definition of "our signed
            # fills" (`signed_qty`, OURS-filtered): the harness echoes only
            # OURS fills, so the filter is a no-op here, but two spellings
            # of that quantity is exactly the drift `real_position` exists
            # to detect.
            real_position += signed_qty(venue)
            row = {"bar_index": i, "ts_open": bar.ts_open, "g1": "ok", "settle_recompute_ms": None,
                   "probe_recompute_ms": bar_probe_ms, "probe_fills": sorted(probe.fills),
                   "retracts": sorted(probe.retracts), "path_variant": sorted(probe.path_variants),
                   "settle_fills": [], "probe_not_settled": [], "actions": [],
                   "venue_echo": [action_json(a) for a in held], "superseded": len(pending) - len(held),
                   "stop": None, "incidents": [], "non_confirmed": [],
                   "venue_fills": len(venue), "real_position": real_position}
            probe_ms_all += bar_probe_ms
            # A `LedgerGap` is NOT caught: it means the harness handed the
            # core the wrong bar (spec §2's carry-forward is the bar
            # layer's job), i.e. a harness bug, and it must be loud.
            row["incidents"] = list(ev_incidents)
            row["stop"] = stop_json(ev_stop)
            try:
                out = _settle_with_one_retry(core, bar, venue, real_position, spec)
            except (LedgerDivergence, BarsDivergence) as e:
                row["g1"] = f"raised:{type(e).__name__}:{e}"
                g1_failed.add(i)
                bars_out.append(row)
                aborted_at = i
                break

            row["incidents"] = ev_incidents + [x["kind"] for x in out.incidents]
            row["stop"] = stop_json(ev_stop or out.stop)
            row["non_confirmed"] = sorted(c.cls.value for c in out.classified if c.cls is not FillClass.CONFIRMED)
            if out.settle is None:
                # LiveCore swallows a divergence into a STOP + incident
                # rather than raising; same conclusion as the except branch.
                row["g1"] = "diverged:" + ",".join(row["incidents"])
                if FATAL_INCIDENTS & set(row["incidents"]):
                    g1_failed.add(i)
                bars_out.append(row)
                # Either way the ledger did NOT advance, so every later bar
                # is a `LedgerGap` by construction. A divergence is a G1
                # failure; a `recompute_aborted` that survived its retry is
                # not -- but both END the run, with a written report and a
                # non-zero exit, rather than raising `LedgerGap` out of the
                # loop on the next bar and printing a traceback instead of
                # a summary.
                aborted_at = i
                break

            s = out.settle
            settled_indices.append(s.bar_index)
            settle_ms.append(s.recompute_ms)
            row["settle_recompute_ms"] = s.recompute_ms
            # A read-back of the journal's own record for bar n-1. NOT an
            # independent check (n3): `Ledger.settle` compares `s.hashes[m]`
            # against this same row before journaling and raises otherwise,
            # so this branch cannot fire unless the `except` above already
            # did. Kept as a cheap assertion that what LANDED is what the
            # run computed; the INDEPENDENT check is the reference pass.
            prev_row = journal.settlement(spec.epoch_hash(), s.bar_index - 1)
            if prev_row is None or int(prev_row["broker_state_hash"]) != s.hashes[s.bar_index - 1]:
                row["g1"] = "journal_hash_mismatch"
                g1_failed.add(i)

            settled_sigs = {(c.emulated.intent, c.emulated.leg, c.emulated.is_long)
                            for c in out.classified if c.emulated is not None}
            row["settle_fills"] = sorted(list(x) for x in settled_sigs)
            row["actions"] = [action_json(a) for a in out.actions]
            # Ruling 2 / spec §10.2: probe fills ⊆ settlement fills ∪ PROBE_RETRACT.
            row["probe_not_settled"] = sorted(list(p) for p in probe.owed(settled_sigs))
            bars_out.append(row)

            owed = [a for a in out.actions if a.kind not in NON_FILL_ACTIONS]

    # Spec §10.2's reference run: the LAST recompute covers every settled
    # bar, so its own hash vector and trade prefixes are checked against
    # what each L_m journaled at the time -- one pass, no extra recompute.
    # This is the harness's INDEPENDENT G1 check (see the module docstring).
    reference = {"bars_checked": 0, "hash_mismatches": [], "prefix_mismatches": []}
    final = core.ledger.last
    if final is not None:
        for m in settled_indices:
            reference["bars_checked"] += 1
            jr = journal.settlement(spec.epoch_hash(), m)
            if jr is None or int(jr["broker_state_hash"]) != final.hashes[m]:
                reference["hash_mismatches"].append(m)
                g1_failed.add(m)
                continue
            if keys_sha256([k for k in final.keys if k.exit_bar <= m]) != jr["trades_sha256"]:
                reference["prefix_mismatches"].append(m)
                g1_failed.add(m)

    return _report(args, spec, bars_out, settle_ms, probe_ms_all, g1_failed, reference, aborted_at, seed_row)


def _settle_with_one_retry(core: LiveCore, bar, venue, real_position, spec):
    """Settle `bar`, retrying it ONCE on `recompute_aborted`.

    A `RecomputeAborted` journals nothing and leaves the ledger untouched
    (`Ledger.settle` commits its staged state last), so the same bar --
    same venue fills, same account snapshot, none of which were consumed
    -- can simply be settled again. It is not a G1 failure and not a
    divergence; only a second abort is recorded as one."""
    for attempt in (1, 2):
        out = core.settle(bar, venue_fills=venue, in_flight=set(), mirrored=set(),
                          real_position=real_position, now_ms=bar.ts_open + tf_ms(spec.script_tf))
        retryable = out.settle is None and not (FATAL_INCIDENTS & {x["kind"] for x in out.incidents})
        if not retryable or attempt == 2:
            return out
    raise AssertionError("unreachable")


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
    seed_row = report.get("seed") or {}
    if seed_row.get("stop"):
        print("l1: seed STOP {0}/{1}: {2}".format(*seed_row["stop"]))
    if not report["bars"] and seed_row.get("incidents"):
        print(f"l1: seed incidents: {', '.join(seed_row['incidents'])}")
    print("l1: recompute_ms settle p50 {p50} p99 {p99} max {max} (n {n})".format(**report["recompute_ms"]["settle"]))
    print("l1: recompute_ms probe  p50 {p50} p99 {p99} max {max} (n {n})".format(**report["recompute_ms"]["probe"]))
    if report["aborted_at"] is not None:
        print(f"l1: ABORTED at bar {report['aborted_at']}: {report['bars'][-1]['g1']}")
    for b in report["bars"]:
        if b["probe_not_settled"]:
            print(f"l1: bar {b['bar_index']} probe fills neither settled nor retracted: {b['probe_not_settled']}")
        if b["g1"] != "ok":
            print(f"l1: bar {b['bar_index']} G1: {b['g1']}")
        if b["stop"] is not None:
            print(f"l1: bar {b['bar_index']} STOP {b['stop'][0]}/{b['stop'][1]}: {b['stop'][2]}")
        if b["incidents"]:
            print(f"l1: bar {b['bar_index']} incidents: {', '.join(b['incidents'])}")
        if b["non_confirmed"]:
            print(f"l1: bar {b['bar_index']} fills not CONFIRMED: {', '.join(b['non_confirmed'])}")
        if b["path_variant"]:
            print(f"l1: bar {b['bar_index']} path-variant probe fills (excluded from probe_not_settled): {b['path_variant']}")
        if b["superseded"]:
            print(f"l1: bar {b['bar_index']} superseded {b['superseded']} request(s) before the venue saw them")
    for m in report["reference_run"]["hash_mismatches"]:
        print(f"l1: reference run disagrees with journaled hash at bar {m}")
    for m in report["reference_run"]["prefix_mismatches"]:
        print(f"l1: reference run disagrees with journaled trade prefix at bar {m}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
