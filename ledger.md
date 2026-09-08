# pineforge-live work ledger

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
Current local suite: **644 passed with engine**; **522 passed, 122 skipped
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

B3 is **not complete**. The local executable slice is not trading readiness.
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
