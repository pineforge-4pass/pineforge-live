# Isolated Cloud Run verification

This image runs the live repository's campaign worker with immutable source and
probe inputs. It has Python 3.12, CMake, GCC, Ninja, Git and Eigen. Engine and
codegen source arrive through SHA-pinned Git bundles at runtime; the worker must
check out their declared commits, verify trees, and compile them on Cloud Run.
Codegen currently has no third-party runtime dependencies. No arbitrary remote
Git branch or unpinned runtime installation belongs in this path.

All real-probe backtests, both streaming modes, webhook verification and grading
run inside Cloud Run. Local packaging, pure unit tests and compilation are not
parity measurements. This runner does not update the parity campaign's jobs,
images, Postgres rows, populations, baseline, or grading rubric.

## Prepare reviewed artifacts

`package_review.py` packages explicitly named full Git commits, retaining actual
commit and tree identities. It uses temporary bare clones to create a bundle ref;
the original source checkouts and refs are unchanged. Untracked files are never
included. It rejects common credential-shaped tracked paths across reachable
history. Review the exact commits before packaging, as with any source release.

```sh
python cloudrun/package_review.py \
  --source live=/path/to/pineforge-live --commit live=FULL_COMMIT_SHA \
  --source engine=/path/to/pineforge-engine --commit engine=FULL_COMMIT_SHA \
  --source codegen=/path/to/pineforge-codegen-oss --commit codegen=FULL_COMMIT_SHA \
  --source lab=/path/to/pineforge-lab --commit lab=FULL_COMMIT_SHA \
  --evidence-refs /path/to/exported-probe-evidence-refs.json \
  --output /new/path/to/source-packet \
  --live-context /new/path/to/live-image-context
```

The output `sources.json` describes each `.bundle` by SHA-256, byte length,
commit, tree and gitlinks. `sources.sha256` pins its exact JSON bytes. Gitlinks
are references only: provide a separate corpus source when the worker needs it.
Evidence refs should retain the registry's exact strategy, TV trades, metrics,
meta, inputs and feed digests. Never reconstruct the scientific feed CSV from
Postgres `feed_bars`; use the content-addressed original bytes.

The optional image context contains only the live commit's `pineforge_live/`,
`cloudrun/`, `LICENSE` and `pyproject.toml`. It deliberately excludes `.git`,
credentials, untracked local files, journals, build artifacts and test tapes.
The adjacent context identity document binds the context to the reviewed live
commit and archive digest. Upload packets as immutable `sha256/<sha>` objects
with generation-zero conditions through the authorized orchestrator. The helper
does not upload or invoke GCP.

`build.yaml` builds that exact context into a separately named review image.
Pass `_PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:...` using a resolved digest;
the default is intentionally empty. Pass `_IMAGE` as a unique review tag, then
resolve and record the resulting image digest before creating the job. Image
construction checks Python/SQLite/SSL/ctypes imports; it never runs probes.
Pass `_LIVE_COMMIT` as the context's reviewed full commit. The image records it
in `PINEFORGE_LIVE_BUILD_COMMIT` and its OCI revision label, and the worker
refuses a run manifest naming any other live commit. `job_spec.py` also provides
`PINEFORGE_LIVE_IMAGE_DIGEST` from its exact image reference for the result receipt.

## Job and authentication

`job_spec.py` prints a Cloud Run Job document without submitting it. It refuses
shared production job names, floating image tags, more than eight tasks, and
timeouts beyond four hours. Defaults are one task, eight CPU, 32 GiB, two hours,
zero retries. Give every independent attempt a fresh run ID and isolated job name.

```sh
python cloudrun/job_spec.py \
  --name pineforge-live-review-UNIQUEID \
  --image REGION-docker.pkg.dev/PROJECT/REPOSITORY/pineforge-live-review@sha256:DIGEST \
  --service-account RUNTIME@PROJECT.iam.gserviceaccount.com \
  --bucket EVIDENCE_BUCKET \
  --manifest-sha256 MANIFEST_SHA > /path/to/review-job.json
```

Review the concrete JSON and image identity before authorized job creation.
There is no production-editing or deployment command in these scripts.
The worker reads `PINEFORGE_LIVE_MANIFEST_SHA256` and
`PINEFORGE_EVIDENCE_BUCKET`; Cloud Run supplies `CLOUD_RUN_TASK_INDEX/COUNT`.
Its entrypoint is `python -m pineforge_live.verification.campaign_worker`.

`GcsStore` obtains OAuth only from the Cloud Run metadata server. It does not
read `.env`, invoke gcloud, or carry a credential in the image/manifest. The
runtime identity needs evidence bucket object viewer and object creator. An
exported immutable probe packet needs no SQL socket or Secret Manager access.
The deployer separately needs Artifact Registry write, Cloud Run create/execute
and service-account actAs permissions.

Input downloads stream into a temporary file, verify length/SHA, then publish to
a new local pathname without replacing an existing file. Tar extraction admits
only regular files and directories into a new root, rejects all links, traversal,
duplicates and special files, and limits expanded bytes/member count. Output
uploads are confined to `live-verification/<run_id>/<task>/<artifact_name>` and
use `ifGenerationMatch=0`. Every upload, including HTTP 412 for an existing object,
requires a complete SHA/length readback before reporting success.

## Compiler and runtime contract

For the campaign's default build profile, compile inside the worker:

```sh
cmake -S ENGINE -B ENGINE/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DPINEFORGE_BUILD_TESTS=OFF -DPINEFORGE_BUILD_TUTORIAL=OFF \
  -DPINEFORGE_BUILD_CORPUS_STRATEGIES=OFF -DCMAKE_CXX_FLAGS=
cmake --build ENGINE/build --target pineforge --parallel 8
g++ -std=c++17 -O1 -fPIC -I ENGINE/include -I ENGINE/build/include \
  -c PROBE/generated.cpp -o PROBE/generated.o
g++ -shared -o PROBE/strategy.so PROBE/generated.o \
  -Wl,--whole-archive ENGINE/build/lib/libpineforge.a -Wl,--no-whole-archive
```

Generate C++ using the pinned codegen's `pineforge_codegen.transpile(source)`.
Load codegen directly from that checked-out source through the worker's Python
path; there are no runtime dependencies to install for the current package.
Use the lane's declared build profile if it differs from the default; record
compiler/toolchain, actual flags, source/generated/library hashes in evidence.

`ENGINE/scripts/run_strategy.py:inputs_run_kwargs(params, strategy_dir,
default_ohlcv, default_chart_tz)` is the existing configuration resolver. It
returns the feed path and `Strategy.run()` kwargs. Pass `params` itself into
`Strategy.run(params=...)` too, so Pine inputs reach the native setters. The
pinned lab's `verify_routing.pine_input_overrides_from_document()` preserves both
the `input_overrides` block and eligible top-level Pine input scalars.

Forward the exact strategy overrides, syminfo numeric/string metadata,
timezone/session, input/script timeframe, chart timezone, magnifier settings,
chart start, trading window and declared auxiliary/native security feeds. Retain
the original `inputs.json`; if paths must be rebased, record the derived runtime
document separately and pin both hashes. Do not infer account FX or add metadata
that the selected probe/lane does not declare.

Use the pinned canonical `verify_corpus.analyze_strategy()` on a complete probe
directory, including exact `inputs.json` and `strategy.pine`; those affect grading.
Run the pinned source-trade provenance validator over metrics/meta/source/trades
before grading. Keep live-vs-backtest equivalence, TV parity grade, and webhook
delivery accounting as separate assertions. Synthetic OHLCV-derived ticks prove
the declared path model, not the unavailable historical exchange tick sequence.

## Two-input measurement scope

`python -m cloudrun.prepare_run` freezes the probe IDs before measurement,
retains the full population identity, and packages only those probes' original
evidence bytes. The default is two probes per lane/group using seed 20260909;
explicit additional IDs and `--all-probes` are supported.

The worker uses the same public `run_signals` runtime for bars and both mock
tick paths. It compares each settlement's broker-state hash, closed-trade
digest, projected order actions, real HTTP delivery, and restart deduplication
against a full C++ batch over identical script bars. Script bars are derived
from the original 1m CSV. Native-chart differences are reported separately.
The replay calendar uses native opening timestamps, declared session hours and
IANA timezones, never prices or actions. Closing boundaries do not move when
input minutes are missing. Windows whose closing schedule is unknown remain
available for native warmup but are excluded from live replay.

This is live/batch equivalence verification, not a new campaign grade or gate.
The optional canonical-grader diagnostic uses fixed live flags; it does not
rerun the campaign's warmup/origin ladder, native higher-timeframe feed
selection, or TV report-window/range-end projection. Regular `request.security`
probes use the original 1m auxiliary history plus observed input minutes. If
that history starts later than the native chart, its origin is the first
complete shared chart opening determined from timestamps and session hours. Each result records the
actual configuration and unavailable feed coverage. Unsupported or failed
probes remain in the result; the selection is never replaced after observation.
Synthetic ticks represent explicit high-first and low-first models, not the
unavailable historical exchange tick sequence.

The replay window is the earliest eligible trade window under the declared
input coverage policy. It retains every warmup bar from the effective history origin. The
worker also executes the native chart from the effective shared origin once;
when reconstructed bars match native bars, their broker-state prefix must
match that run. This
keeps long-history reference evidence while bounding repeated live replay.
A partial run is restarted after two input events before the full replay, and
a final restart must deliver no duplicate actions.

Sparse archived feeds must explicitly select `--input-gap-policy observed`
when preparing a run. Results record the policy and unsupplied minute-slot
count; no row is padded or invented. Strict mode remains the default for
production feeds promising complete minute coverage. Native special-session
labels are retained, but a window without an independent closing schedule
cannot establish closing coverage and is excluded from live replay.
Both stream modes use the same common chart/minute history origin, including
strategies with a one-time entry before a later-starting minute archive. A
restart at that shared origin verifies a new initial state; it does not
reproduce a position opened before the available minute history.
