#!/usr/bin/env bash
# Build the engine checkout at $PINEFORGE_ENGINE_ROOT.
# with corpus strategies so tests have a compiled strategy library and the derived feed.
set -euo pipefail
[[ -n "${PINEFORGE_ENGINE_ROOT:-}" ]] || { echo "set PINEFORGE_ENGINE_ROOT to an engine checkout with ABI v4" >&2; exit 2; }
ROOT="$PINEFORGE_ENGINE_ROOT"
[[ -f "$ROOT/CMakeLists.txt" ]] || { echo "no engine checkout at $ROOT" >&2; exit 2; }
cd "$ROOT"
[[ -f corpus/CMakeLists.txt ]] || git submodule update --init corpus
SKIP_RUN=1 SKIP_VERIFY=1 JOBS="${JOBS:-8}" scripts/run_corpus.sh
git -C "$ROOT/corpus" checkout -- validation_report.md
ls corpus/validation/ta-sma-152-close-cross-01/strategy.* >/dev/null
echo "engine ready: $ROOT ($(git -C "$ROOT" rev-parse --short HEAD))"
