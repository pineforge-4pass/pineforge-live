# pineforge-live

The live runtime whose ledger *is* the PineForge backtest: at every script-bar
close the ledger is `run_backtest_full(bars[history_start..n])`; every intrabar
evaluation is the same function over the bars plus the forming bar with the
engine's ABI v4 tail flags. Design: `pineforge-workflow-live/docs/superpowers/specs/2026-09-07-pineforge-live-design.md`.

## Layout
- `pineforge_live/engine/` — ctypes binding to the engine's C ABI (v4). Never the streaming lifecycle (G0).
- `pineforge_live/types.py`, `pineforge_live/adapters/base.py` — venue-neutral types and adapter interfaces (`ADAPTER_API_VERSION`).
- `pineforge_live/bars/` — forming-bar builder, bar policy, bar hash.
- `pineforge_live/journal/` — SQLite WAL journal, sidecar STOP marker, fencing lease.
- `pineforge_live/epoch.py` — epoch / code identity / runtime config hashes.
- `pineforge_live/adapters/tape.py` — bar/tick tape adapters and a drivable clock.

## Tests
`PINEFORGE_ENGINE_ROOT=~/code/pineforge-engine-wt/main python3 -m pytest -q`
(`scripts/build_engine.sh` builds that checkout; engine-backed tests skip without the variable.)
