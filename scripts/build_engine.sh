#!/usr/bin/env bash
# Build the engine checkout at $PINEFORGE_ENGINE_ROOT (default ~/code/pineforge-engine-wt/main)
# with corpus strategies so tests have a compiled strategy library and the derived feed.
set -euo pipefail
ROOT="${PINEFORGE_ENGINE_ROOT:-$HOME/code/pineforge-engine-wt/main}"
[[ -f "$ROOT/CMakeLists.txt" ]] || { echo "no engine checkout at $ROOT" >&2; exit 2; }
cd "$ROOT"
[[ -f corpus/CMakeLists.txt ]] || git submodule update --init corpus
SKIP_RUN=1 SKIP_VERIFY=1 JOBS="${JOBS:-8}" scripts/run_corpus.sh
ls corpus/validation/ta-sma-152-close-cross-01/strategy.* >/dev/null
echo "engine ready: $ROOT ($(git -C "$ROOT" rev-parse --short HEAD))"
