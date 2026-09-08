import csv, json
from pathlib import Path
from pineforge_live import types as T
from pineforge_live.engine import EngineHandle
from pineforge_live.epoch import CodeIdentity, EpochSpec, apply_epoch
from pineforge_live.journal import Journal, StopMarker

def load_bars(feed: Path, limit: int | None = None) -> list[T.NormalizedBar]:
    out = []
    with feed.open() as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            if limit is not None and i >= limit:
                break
            out.append(T.NormalizedBar(int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]),
                                       float(r["close"]), float(r["volume"]), 0))
    return out

def corpus_syminfo() -> T.EngineSyminfo:
    return T.EngineSyminfo("ETHUSDT.P", "TAPE:ETHUSDT.P", "TAPE", "ETHUSDT", "crypto", "USDT", "ETH", 0.01, 100, 1.0, 1,
                           "24x7", "UTC", "base", "corpus probe")

def corpus_spec(script_tf: str = "15", horizon_bars: int = 1_000_000) -> EpochSpec:
    return EpochSpec(venue="TAPE", instrument=T.InstrumentId("TAPE", T.MarketType.PERP, "ETHUSDT"), script_tf=script_tf,
                     history_start_ms=1_577_836_800_000, horizon_bars=horizon_bars,
                     code_identity=CodeIdentity("e" * 64, "c" * 64, "s" * 64, {"codegen_sha": "c", "source_sha": "s", "compiler_id": "clang", "so_sha256": "0"}),
                     syminfo=corpus_syminfo(), reference_tape_sha256="t" * 64)

def make_handle(test_so: Path, spec: EpochSpec) -> EngineHandle:
    h = EngineHandle(test_so); apply_epoch(h, spec); return h

def open_journal(tmp_path: Path) -> tuple[Journal, StopMarker]:
    m = StopMarker(tmp_path / "j.sqlite3.stop"); m.prepare()
    return Journal.open(tmp_path / "j.sqlite3", stop_marker=m), m
