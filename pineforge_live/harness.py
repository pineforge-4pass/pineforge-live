"""Tape-backed epoch/handle/journal wiring shared by the L1 harness
(`scripts/l1_harness.py`) and the test suite (`tests/helpers.py`).

One definition of "a `LiveCore` standing over a recorded feed": the venue
is always the neutral placeholder `"TAPE"` (spec's no-venue-names rule),
the instrument is a perp on that venue, and the code identity is a
PLACEHOLDER -- an L1 run measures G1 and probe-equivalence over a tape, it
is not an admission gate (spec §1 admission (a)-(c) needs the campaign
verdict and the graded tape, neither of which a harness invents). The one
real digest available locally, the deployed `.so`'s own sha256, IS filled
in when the caller passes `so=`, so a journal written by a harness run
records which library actually produced it.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from pineforge_live import types as T
from pineforge_live.engine import EngineHandle
from pineforge_live.epoch import CodeIdentity, EpochSpec, apply_epoch
from pineforge_live.journal import Journal, StopMarker

TAPE_VENUE = "TAPE"
#: 2020-01-01T00:00:00Z -- the corpus feeds' own start; `history_start_ms`
#: only has to be at or before the first bar handed to the ledger.
HISTORY_START_MS = 1_577_836_800_000

def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def tape_syminfo(ticker: str = "ETHUSDT.P", base: str = "ETH", quote: str = "USDT") -> T.EngineSyminfo:
    """`EngineSyminfo` for a perp on the neutral `"TAPE"` venue (the corpus
    probe `ta-sma-152-close-cross-01`'s ETHUSDT.P defaults)."""
    return T.EngineSyminfo(ticker, f"{TAPE_VENUE}:{ticker}", TAPE_VENUE, base + quote, "crypto", quote, base,
                           0.01, 100, 1.0, 1, "24x7", "UTC", "base", "corpus probe")

def tape_spec(script_tf: str = "15", horizon_bars: int = 1_000_000, so: str | Path | None = None,
              syminfo: T.EngineSyminfo | None = None) -> EpochSpec:
    """The `EpochSpec` for a tape-backed run of the corpus probe on `tf`.

    `so` (optional) is the strategy library actually loaded: its sha256
    goes into the build receipt's `so_sha256`, so two runs of the harness
    against different libraries are different epochs (and the journal says
    which). Omitted -- the test fixtures' path -- the receipt keeps its
    placeholder digest, and the epoch hash is the stable one the suite's
    journal fixtures have always used."""
    receipt = {"codegen_sha": "c", "source_sha": "s", "compiler_id": "clang",
               "so_sha256": sha256_file(so) if so is not None else "0"}
    return EpochSpec(venue=TAPE_VENUE, instrument=T.InstrumentId(TAPE_VENUE, T.MarketType.PERP, "ETHUSDT"),
                     script_tf=script_tf, history_start_ms=HISTORY_START_MS, horizon_bars=horizon_bars,
                     code_identity=CodeIdentity("e" * 64, "c" * 64, "s" * 64, receipt),
                     syminfo=syminfo or tape_syminfo(), reference_tape_sha256="t" * 64)

def make_handle(so: str | Path, spec: EpochSpec) -> EngineHandle:
    """Load `so` and replay `spec`'s setter sequence onto a fresh `EngineHandle`."""
    h = EngineHandle(Path(so)); apply_epoch(h, spec); return h

def open_journal(directory: str | Path, name: str = "j.sqlite3") -> tuple[Journal, StopMarker]:
    """Open a fresh journal under `directory`, with its stop marker prepared."""
    d = Path(directory)
    m = StopMarker(d / f"{name}.stop"); m.prepare()
    return Journal.open(d / name, stop_marker=m), m
