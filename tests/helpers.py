"""Shared test fixtures for the real-engine suite.

Stand for one corpus probe throughout: `ta-sma-152-close-cross-01`
(ETH-USDT, 15m), built by `scripts/build_engine.sh` under
`PINEFORGE_ENGINE_ROOT`. `"TAPE"` is the venue-neutral placeholder used in
place of any real exchange name (spec's "no venue names in tests" rule).

The construction itself lives in `pineforge_live.harness` -- the same
wiring `scripts/l1_harness.py` stands a `LiveCore` up with, so the suite
and the harness cannot drift into two different ideas of "the corpus
epoch". This module is the suite's names for it.
"""
from pathlib import Path
from pineforge_live import types as T
from pineforge_live.adapters.tape import load_feed_csv
from pineforge_live.epoch import EpochSpec
from pineforge_live.harness import make_handle, open_journal, tape_spec, tape_syminfo

__all__ = ["load_bars", "corpus_syminfo", "corpus_spec", "corpus_spec_bracket", "corpus_spec_pooc",
           "make_handle", "open_journal"]

def load_bars(feed: Path, limit: int | None = None) -> list[T.NormalizedBar]:
    """The first `limit` bars of a `timestamp,open,high,low,close,volume`
    feed CSV as `NormalizedBar`s (`adapters.tape.load_feed_csv`, which the
    tape sources themselves read feeds with).

    Feed `run_full`/`Ledger.seed` with `[b.ohlcv() for b in bars]` (or pass
    the `NormalizedBar`s straight in now that `EngineHandle.run_full`
    accepts anything exposing `.ohlcv()`)."""
    return load_feed_csv(feed, limit)

def corpus_syminfo() -> T.EngineSyminfo:
    """`EngineSyminfo` for `ta-sma-152-close-cross-01` (ETHUSDT.P, venue `"TAPE"`)."""
    return tape_syminfo()

def corpus_spec(script_tf: str = "15", horizon_bars: int = 1_000_000) -> EpochSpec:
    """`EpochSpec` for `ta-sma-152-close-cross-01` on the 15m ETH-USDT feed."""
    return tape_spec(script_tf, horizon_bars)

def corpus_spec_bracket(script_tf: str = "15", horizon_bars: int = 1_000_000) -> EpochSpec:
    """`EpochSpec` for the corpus bracket probe `ta-pivot-atr-stop-target-01`
    (ATR stop/target via `strategy.exit`). Its `inputs.json` carries no
    `ohlcv_csv`/tf override, so -- like `ta-sma-152-close-cross-01` -- it
    runs on the same default 15m ETH-USDT feed (`test_feed`) and the same
    `"TAPE"` syminfo/venue; only the loaded `.so` (`test_so_bracket`)
    differs, which is why this is the same spec rather than another one."""
    return tape_spec(script_tf, horizon_bars)

def corpus_spec_pooc(script_tf: str = "15", horizon_bars: int = 1_000_000) -> EpochSpec:
    """`EpochSpec` for the corpus POOC probe
    (`order-deferred-flip-pooc-cross-bar-01`: `process_orders_on_close=true`,
    a weekly-reset deferred-flip chain). Its probe directory holds no
    `inputs.json` at all, so -- like `ta-sma-152-close-cross-01` and
    `ta-pivot-atr-stop-target-01` -- it runs on the default 15m ETH-USDT
    feed (`test_feed`) and the same `"TAPE"` syminfo; only the loaded `.so`
    (`test_so_pooc`) differs, which is why this is the same spec rather
    than another one."""
    return tape_spec(script_tf, horizon_bars)
