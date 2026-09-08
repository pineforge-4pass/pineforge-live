# pineforge-live work ledger

## Current public contract — broker-neutral webhooks (2026-09-09)

The user clarified that this repository is OSS and each strategy order action
should trigger a simple webhook event, independent of any broker. This
**supersedes the earlier B3 broker/exchange integration plan**. The C++ engine
continues to compute every strategy decision; Python owns data input, durable
state and HTTP delivery. No broker credentials or account admission are
required by the public `run`/`check` path.

Implemented: settled and optional provisional intrabar alerts, neutral
stdin/JSONL/HTTP/WebSocket feeds, dedicated C++ worker, atomic ledger/outbox
checkpoints, ordered at-least-once webhooks with stable IDs and HMAC, bounded
retries and explicit queue recovery, restart-safe tick provenance,
configuration loading, CLI commands, runnable corpus demo, durable example
receiver, and Apache-2.0 package metadata. The README and docs/webhooks.md
are the supported public workflow. Broker-specific work below is historical,
not unfinished work for this contract.

Validation in progress: 813 tests passed with the ABI-v4 engine; 666 passed
and 147 skipped without it. Generic HTTP and WebSocket transport, compiled
strategy to signed HTTP delivery, queue/receiver crash recovery, and no
duplicate actions after restart are tested. Independent review found and
fixed lease-before-POST timing, long-recompute SQLite contention, artifact-
independent queue recovery, startup refusal reporting, tick timestamp
regression and restored forming-bar provenance. C++ computation now stages
writes without holding SQLite's writer lock, and rechecks authority in the
short atomic commit. A real subprocess sender/receiver demo delivered 37
signed unique events across SMA, bracket and orders-on-close strategies;
restarting each through `check` delivered zero duplicates. Final exact-
candidate Grok review follows.

No push or public repository creation is performed; the earlier no-push
instruction remains in force.

## Historical work before the webhook clarification

## Continuation of Claude session 08f941f2-ec0e-4df3-aada-eaef4778b971

Recovered baseline: B2 completed at `1255ef4`; OSS README committed as
`1a3a6fb`. The earlier session's final TODO list is in the sibling
`pineforge-workflow-live` B2 ledger. The latest user instruction there was
**do not push**. This continuation remains local; no repository, release,
exchange order or deployment has been created.

## Completed locally on 2026-09-09

- B3 preliminary flatten ruling: both reconciler FLATTEN and HARD_FLAT bypass
  correction/notional caps while retaining STOP permission checks. Ordinary
  correction gates remain enforced.
- Journal schema v2: bar-time daily counter restoration with exact admitted
  correction counts. Old journals are refused before mutation.
- Composable journal transactions and atomic `DurableCore` decisions,
  checkpoints, trigger/probe state and G1-checked restoration.
- Durable execution outbox, physical-order identity, next-open notice
  confirmation, timeout adoption, fill deduplication and action receipts.
- Deterministic mock executor with rejection, partial, duplicate-event and
  timeout faults; documented fidelity limits.
- Pure venue/account risk evaluator and startup checks; protective-order
  proposals and emergency rate reserves. These components are not yet wired
  into a live account driver.
- Offline `execution-replay` CLI with stream/check schedules, runtime
  recreation, structured failure reports and real mock Executor calls.
- Fenced, audited `stop-clear` CLI and drained-lease release.
- Intrabar probe `level_resolved` output; mandatory explicit engine build path.
- Current architecture/usage documentation and the B3 implementation plan.

## Review and verification

Baseline: 315 engine-backed tests passed before edits, using engine checkout
`41c9c741d2b4f20eb793655bef4e9c56357a97fe` with built ABI-v4 corpus libraries.
Current local suite: **654 passed with engine**; **530 passed, 124 skipped
without engine**. Scoped pyflakes and `git diff --check` pass.

Independent Codex review reproduced four atomicity defects during development:
external-transaction success before commit, continuation after corrupted
checkpoint refusal, split core/outbox journals, and stranded refused seeds.
All four were fixed and regression-pinned. Driver review also fixed current
STOP submission fencing and failure reports that reread a broken journal.
All three 200-bar L1 lanes passed with G1 failures 0. Six 200-bar execution
lanes (SMA/bracket/POOC × stream/check) each processed 1,000 decisions and
one runtime recreation, with unresolved orders 0. Orders/receipts were 16/16,
13/13, and 8/8 respectively; POOC recorded four separate action receipts.
Full receipts are in `build/b3-review/`. Independent Grok review is required
on the final candidate before this continuation is reported complete.

## Remaining work and gates

Historical status before the webhook clarification: venue-execution B3 was
not complete. That broker-coupled scope is now superseded above.
Remaining implementation is specified in [docs/plan-b3.md](docs/plan-b3.md):
real instrument/exchange adapters; full observed-order adoption/absence
proof; late/partial/chunk reconciliation; live stream/cron scheduling;
mirror/dead-man/DISASTER sequencing and deadlines; account risk integration;
and admission/feature contracts.

Remaining evidence: real recorded adapter conformance, L2/L3 calibration,
full-feed and partition-3 fault lanes, registry/lint delivery, L4 paper/canary.
The offline replay explicitly does not fabricate those observations.

The previously recorded publication decisions remain untouched: LICENSE and
package metadata, public repository/origin, public design spec, engine release
pin, and security contact. No push is authorized by this continuation.

## Independent Grok review and corrections

Grok reviewed `e85b9fec640202abccb00bf0bf1153fceb069651` independently and
returned CHANGES_REQUIRED (P0=0, P1=3, P2=2). The five findings were reproduced
and fixed: notice withdrawals are scoped by leg; pending closes remain
parents across ingestion boundaries; aborted core operations do not consume
decision ids; order-state progression is monotonic; terminal rejections stay
explicit residuals without being mislabeled unresolved submissions. Ten
regression cases cover these paths, including an ACKED market close queued
after its entry. Duplicate fills now also verify their original timestamp
without advancing a conflicting history cursor. A new exact-candidate Grok
review follows these corrections.
