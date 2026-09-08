import glob, os
from pathlib import Path
import pytest

def _engine_root() -> Path | None:
    v = os.environ.get("PINEFORGE_ENGINE_ROOT")
    return Path(v).expanduser() if v else None

@pytest.fixture(scope="session")
def engine_root() -> Path:
    root = _engine_root()
    if root is None or not (root / "CMakeLists.txt").exists():
        pytest.skip("PINEFORGE_ENGINE_ROOT not set or not an engine checkout")
    return root

@pytest.fixture(scope="session")
def test_so(engine_root: Path) -> Path:
    hits = glob.glob(str(engine_root / "corpus/validation/ta-sma-152-close-cross-01/strategy.*"))
    hits = [h for h in hits if h.endswith((".dylib", ".so"))]
    if not hits:
        pytest.skip("corpus strategy library not built; run scripts/build_engine.sh")
    return Path(hits[0])

@pytest.fixture(scope="session")
def test_so_bracket(engine_root: Path) -> Path:
    """The corpus bracket probe (`ta-pivot-atr-stop-target-01`): a
    strategy.exit ATR stop/target, so its pending-order mirror exposes real
    ENTRY/EXIT rows (unlike the sole MARKET row `test_so` ever shows)."""
    hits = glob.glob(str(engine_root / "corpus/validation/ta-pivot-atr-stop-target-01/strategy.*"))
    hits = [h for h in hits if h.endswith((".dylib", ".so"))]
    if not hits:
        pytest.skip("corpus strategy library not built; run scripts/build_engine.sh")
    return Path(hits[0])

@pytest.fixture(scope="session")
def test_feed(engine_root: Path) -> Path:
    p = engine_root / "corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv"
    if not p.exists():
        pytest.skip("derived 15m feed missing; run scripts/build_engine.sh")
    return p
