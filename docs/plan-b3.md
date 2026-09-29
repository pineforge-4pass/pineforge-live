# B3 — historical venue-execution plan

> Superseded on 2026-09-09 by the user's explicit OSS contract: every
> strategy order action emits a simple webhook event, with no broker tied to
> the runner. Broker adapters, account admission, mirror/dead-man execution
> and production exchange evidence below are not requirements of the public
> signal runtime. The current contract is [webhooks.md](webhooks.md).
> Modules and tests named below that are absent from the tree were never
> implemented.

Status: implementation plan, 2026-09-09. This continues the approved live
design and B2 at `1a3a6fb`; it is not evidence that an exchange integration
has passed admission. Local implementation and fault simulation can proceed.
Do not push or enable a production account as part of this continuation.

Authority: sibling `pineforge-workflow-live/docs/superpowers/specs/2026-09-07-pineforge-live-design.md`
(a maintainer design document that is not published),
especially §§2–2a, 4–7; its B2 ledger's post-archive ruling and TODO 8–17;
and this repository's [core design](core.md). The post-archive FLATTEN
exemption supersedes the older spec sentence that budgets that flatten.
Binance USDT-M, Python asyncio, one strategy/instrument/subaccount, one
host/journal, and both stream and check modes were already selected.

## Scope and delivery order

Deliver a working local execution stack first: persistent action/fill state,
deterministic mock adapter, core recovery, coordinator, protection, drivers,
and CLI. Then implement Binance transport and recorded-response conformance.
An adapter with only authored fixtures is locally tested, not testnet-proven.
B4 extends the execution fault matrix and measured population; B3 must still
prove the failure boundaries of each module it lands.

Keep `core/` and shared types venue-neutral. The engine remains the only
implementation of strategy fill rules. A mock simulates venue execution of
actual `OrderAction`s; it must never consult future engine settlements to
decide which requested orders to fill.

Parallelizable starting modules are identity/value types, MockExecutor,
schema/store after the schema owner freezes its contract, and pure rate /
protection calculations. Core recovery and coordinator integration depend on
the store. Drivers depend on coordinator and protection.

## Decisions at the core/executor boundary

### Action identity, retries, and fills

Keep `ActionRequest` as the core's venue-neutral intent. Wrap it at the driver
boundary instead of putting exchange ids into the core. Define frozen types
in `execution/types.py`:

```python
DecisionContext(epoch_hash, runtime_config_hash, decision_id, run_token,
                source, origin_bar_index, bar_ts_open, cycle_seq,
                tick_seq_from, tick_seq_to)
# source = seed | settle | evaluate | recover | protection
ActionIdentity(epoch_hash, intent_key, role, level_version, action_seq,
               run_token, client_id)
ExecutionRequest(identity, request, decision_id, origin_bar_index,
                 expected_ledger_bar_index, slot_key, parent_client_id,
                 receipt_mode, observed_open)
# receipt_mode = LEDGER_FILL | ACTION_RECEIPT
ExecutionReceipt(client_id, venue_order_id, origin_bar_index,
                 observed_bar_index, status, requested_qty, submitted_qty,
                 filled_qty, avg_price, fees, trade_ids, residual_qty)
ExecutionSnapshot(ledger_fills, receipts, in_flight, mirrored,
                  our_signed_fills, real_position, account,
                  fill_watermark, account_observed_ms)
```

`intent_key` is the captured `IntentKey.s` when the action belongs to a
settled intent. Binding uses the frozen previous/current book and leg/side;
it must be unique. Synthetic and correction actions use a separate canonical
execution identity containing `(raw Pine id, cycle, origin bar, role)`;
never pretend that an internal action identity is an engine pending order.
An ambiguous engine ENTRY intent `?` remains refused. Preserve the raw Pine
id separately for `VenueFill.intent`.

`slot_key` includes epoch, cycle, raw Pine id, leg, and target bar, and is the
supersession domain for an unsubmitted market notice. An action's role
distinguishes market execution, stop leg, target leg, dead-man, correction,
flatten, and DISASTER side. Mirrored bracket siblings must have distinct
identities despite sharing a Pine exit id.

Allocate a monotonically increasing `action_seq` per epoch in the same
transaction that writes the new physical-order outbox record. Derive the
client id as a validated adapter prefix plus a bounded hexadecimal digest of
canonical `[epoch, intent_key, role, level_version, action_seq, run_token]`.
Use at least 128 digest bits, respect the adapter's maximum id length, and
check collisions against the full stored identity. Never truncate a Pine id
and treat the truncated value as identity. A new physical order gets a new
sequence; a pure retry retains the entire original identity and run token,
even after a new process acquires a higher fencing token. The current lease
authorizes the retry; the old token identifies the existing order.

After an ambiguous timeout or duplicate-client-id response, query `lookup`,
then `orders_since` and `fills_since` over the durable write-ahead window.
Adopt only if the journal identity and normalized order fields agree. The
current `OrderState` lacks instrument/side/kind details needed for this
verification: add an `ObservedOrder` envelope containing `OrderState`,
instrument, side, kind, qty, prices, reduce_only, close_position,
position_side, and trigger_basis. Version the adapter API when query methods
begin returning it. Client-id equality alone is insufficient.

NOT_FOUND before grace means wait. After grace, retry the same physical
order at most once only if exhaustive order history contains no matching
order and all fills in the window have resolved order-id attribution.
An incomplete history page, retention gap, unmapped fill, or uncertain state
is not absence proof: retain UNKNOWN, prohibit exposure increases, and
escalate. A terminal rejection is never requeued. A partial remainder can
become one child order with a fresh sequence only after the original order
is terminal; reconcile any subsequent residual. Never place the child while
the original can still fill.

Fill identity is `(instrument, venue_trade_id)` within the one-account
journal, not client id or event timestamp. Duplicate WS and REST delivery
must not increase filled quantity, fees, signed basis, or metrics twice.
Conflicting payloads for one identity are a journal divergence. Attribute by
client-id mapping, then venue-order-id mapping, then explicit liquidation /
ADL evidence; an unresolved fill remains external/unattributed and reaches
the core's existing STOP rules. In particular, do not relabel all fills from
a known account as OURS.

### MARKET_AT_OPEN is a durable notice

`settle(n)` creates a NOTICE for bar `n+1`, without allocating or submitting
a physical venue order. Persist its slot, frozen intent/cycle, and proxy qty.
The first **successful, non-aborted** `evaluate(n+1)` is a batch boundary:

1. Persist the observed open, evaluation result, and all changes to notices
   atomically. `open_requote` replaces qty; `withdraw`/zero qty withdraws the
   slot; no update confirms the advance at its existing qty only when this
   evaluation actually completed for the target bar.
2. Apply STOP/driver freshness checks, then convert each confirmed notice to
   exactly one outbox order. A crash before this commit leaves a NOTICE;
   after it, adoption resumes the same outbox order.
3. Close the opposite position before allowing the reversal's ENTRY child.
   Await the close's terminal state and a post-fill position snapshot; a
   partial close cannot authorize the full new-side entry. Use the engine's
   confirmed opening qty, never an independently recalculated percentage.

An aborted evaluation, empty/dropped scheduling record, a quote from the
wrong bucket, or open-wait timeout cannot release a notice. REST forming
history may supply the actual open after `open_wait_ms`, but bar-close or
last-price fallback may not. A repeated completed batch returns its stored
outcome. A later request cannot silently mutate an already submitted MARKET:
adopt/drain it first and journal a contradiction if the new request differs.

### Settlement actions get execution receipts, not invented engine fills

TRIGGER, confirmed MARKET_AT_OPEN, and ordinary mirror fills use
`receipt_mode=LEDGER_FILL`: preserve the actual engine match bar in
`VenueFill.target_bar_index`. Aggregate partial venue trades by the exact
action/leg/bar into weighted-price fills before the current one-to-one
classifier sees them; retain all raw trades in the inbox. Do not coalesce
different actions merely because they share a raw Pine id.

CORRECTION, FLATTEN/HARD_FLAT, SYNTHETIC_CLOSE, and MARKET_NOW emitted by
`settle(n)` use `receipt_mode=ACTION_RECEIPT`. They retain
`origin_bar_index=n`; `observed_bar_index` records the bar where their venue
fill was observed, normally `n+1` but possibly later. Match them to the
durable physical action through client/order ids, side, submitted qty, and
trade ids. This is proof of execution of that action, not a new ledger fill.

At `settle(n+1)`, include these fills exactly once in the explicit cumulative
`our_signed_fills` and the post-fill account check. Publish their receipts
separately from `CoreOutput.classified`; exclude them from the new bar's
engine-fill classifier. Never create an `EmulatedFill` to obtain CONFIRMED,
rewrite their origin bar to `n+1`, or rely on the unmatched-OURS dead-band
fallback as evidence. For MARKET_NOW/SYNTHETIC_CLOSE, the receipt may link to
the already stored bar-n SETTLE_ONLY/SYNTHETIC record; that record stays at n.

An ACTION_RECEIPT settles only the submitted amount. Missing, rejected,
overfilled, wrong-side, or late amounts remain explicit durable obligations;
overfill/wrong side invokes STOP, partial quantities retain a residual and
the bounded remainder rule. While pending, their intents participate in
quiescence. On terminal residual, invoke the existing reconciler with a
stored unresolved origin classification when one exists (e.g. MISSED), or a
quantity-only execution residual input. Extend the reconciler's pure input
for that residual; do not invent a MISSED/engine fill for a corrective action.
It must still issue at most one correction from the aggregate position gap,
respect age/distance/STOP limits, and avoid correcting the same receipt twice.

Delayed LEDGER_FILL receipts also keep their original matching bar: match
against a durable unresolved origin obligation, publish the late resolution
at the current decision, and do not inject them as unmatched n+1 fills.
If their origin was already resolved, an extra fill is a real divergence.

## Durable boundaries and recovery

The existing journal's `actions`, `order_states`, `order_ids`, and `fills`
are scaffolding, not a complete outbox/inbox. Current `append_action` is not
idempotent, `append_order_id` silently ignores conflicting mappings, fill
identity lacks instrument, and query cursors are not durable. Fix these
contracts before permitting coordinator submission.

Root's schema-v2/bar-day work is a prerequisite. Use its final field name
for `reconciles.bar_ts_open`; sum counters by `bar_ts_open // DAY_MS`, not
write time. B3 execution tables are a separately versioned addition (bump
the schema again if v2 has already landed). Do not relabel existing v1 rows
as if their bar timestamps were known. Retain the repository's explicit
old-schema refusal until a separately tested migration exists.

Minimum additional durable records:

| record | identity and fields |
|---|---|
| execution decisions | `(epoch_hash, decision_id)`; source, logical event key, bar index/ts, tick range, input hash/JSON, output JSON, previous/next checkpoint hashes, run token, checksum |
| runtime checkpoints | `(epoch_hash, decision_id)`; versioned state JSON, settled bar/hash/trade digest, inbox watermark, checksum |
| notices | `(epoch_hash, slot_key)`; request JSON, origin and target bar, cycle, status NOTICE/CONFIRMED/WITHDRAWN/RELEASED, release decision/client id |
| action outbox metadata | client id FK to actions; role, decision, origin/match bar, receipt mode, parent, slot, request hash, state PREPARED/SUBMITTING/RESOLVED, retry count, attempt times |
| inbox | `(instrument_key, venue_trade_id)` for fills; immutable normalized payload, raw-payload digest, observed time; order events deduped by venue sequence or normalized identity |
| receipt/obligation events | action id plus monotonically increasing event ordinal; trade links, quantities, status, origin classification reference, resolution decision; append-only |
| adapter cursors | `(epoch_hash, stream_kind)`; opaque orders/fills cursor, coverage window, last event sequence, updated decision |
| basis anchor | epoch/cycle, signed adopted quantity, last reset trade watermark, recorded operator-adoption reference when needed |

Use validated structured Journal methods, not interpolated dynamic identifiers
in executor modules. Apply checksums to durable decision/checkpoint/action
content and verify before recovery. Idempotent writes return the existing
record only if the full canonical content agrees; conflicting natural keys
raise `JournalConflict`.

Three boundaries govern crashes:

1. **Inbox commit before cursor movement.** Insert all normalized records,
   update order-id mappings and cursor in one SQLite transaction. Processing
   a committed inbox is replayable. Poll again after an interrupted page.
2. **Core decision plus outbox commit before network.** Add supported Journal
   transaction/savepoint composition; current `_insert_checksummed` starts
   its own transaction and cannot be wrapped. Driver writes decision inputs,
   invokes the synchronous core, and commits settlement/reconcile/intents,
   classification, checkpoint, notices, and outbox together. No await or
   Executor method runs inside this transaction. A database failure discards
   the in-memory core instance and restores the last committed checkpoint.
   A STOP sidecar written before a rolled-back transaction remains raised;
   never clear it to make a replay convenient.
3. **SUBMITTING before network, acknowledgement afterward.** Commit the attempt
   identity before `submit`. Crash before send and crash after accepted send
   look identical locally, so both recover by adoption. New processes may
   not assume a PREPARED/SUBMITTING order was never sent.

Define `LiveCore.export_checkpoint()` and
`LiveCore.restore_checkpoint(history, checkpoint)` as supported APIs. Store
all decision-affecting state: pending markets, current book, current-bar
trigger keys, probe previous/retracted fill history, carried MISSED and its
ages, basis, per-bar committed quantities and risk counters, daily counters
and bar day, G3 window samples, non-quiescent streak/alert flag, and horizon
flag. STOP state is restored from the stronger durable marker/STOP record;
HARD_FLAT outstanding identity belongs to the outbox rather than an
in-memory boolean. Version checkpoint format and reject missing fields.

Recompute the engine prefix to the checkpoint's settled bar and compare G1,
trade digest, position, and captured book; restore only Python runtime state,
never serialize ctypes handles or reuse stale engine accessors. Because the
decision/outbox transaction is atomic, a crash cannot leave a new settlement
without its classification. For an old B2 journal lacking checkpoints, use an
explicit recovery conversion: recompute n-1 then n using a fresh handle,
compare the two existing settlement rows, capture the previous book in its
valid accessor window, derive the real bar-n delta, then classify/reconcile
the persisted venue inbox exactly once with a unique recovery decision id.
If the required previous history, order attribution, or fill coverage is
absent, refuse resumption; `seed(n)` alone is never called recovery.

Restart order: acquire/verify lease → verify epoch/config/code/journal/STOP →
read history and restore checkpoint/G1 → adopt all unresolved actions and
ingest fills → resolve DISASTER → cancel stale cycles and apply STOP cancel
policy → obtain a post-fill account snapshot → process unresolved receipts
and reconcile → admit new work. Discretionary submission remains fenced until
this sequence completes. A present STOP marker blocks ordinary run/check;
recovery inspection or a protection-only path must be explicit and cannot
silently resume entries.

The OURS basis is the durable adopted anchor plus signed deduplicated OURS
fills after its watermark. Reset only at a recorded boundary where ledger
and venue are flat within the dead band. Do not re-anchor each restart to the
latest account position; that would hide foreign fills. The account snapshot
must follow the fill drain in event time; adapters need a timestamp/watermark
envelope when the venue exposes one. Where it does not, drain → poll account
→ re-drain and retry if new fills arrived. On bounded failure report
non-quiescence, not an invented agreeing position.

## Narrow module interfaces

```python
# execution/store.py — synchronous, one journal connection
store.commit_decision(context, inputs, compute_output) -> StoredDecision
store.reserve_order(execution_request, order_action) -> StoredAction
store.ingest_page(stream, events, next_cursor, coverage) -> IngestResult
store.pending_actions() -> list[StoredAction]
store.receipts_since(watermark) -> list[ExecutionReceipt]

# execution/coordinator.py — sole owner of Executor calls
await coordinator.accept(output, context) -> ExecutionBatch
await coordinator.on_evaluation(output, context, forming) -> ExecutionBatch
await coordinator.recover(now_ms) -> RecoveryReport
await coordinator.drain(deadline_ms) -> ExecutionSnapshot
await coordinator.snapshot(bar_index, now_ms) -> ExecutionSnapshot
# Acceptance reserves/journals before submit. Repeated decision_id is a no-op.

# execution/protection.py — pure desired set, coordinator submits differences
planner.desired(book, probe_levels, position, account, constraints, policy)
    -> ProtectionPlan
planner.after_fill(fill, snapshot) -> ProtectionPlan
planner.on_deadline(now_ms, snapshot) -> ProtectionPlan

# adapters/mock.py — implements versioned Executor protocol
mock.advance_to(now_ms); mock.inject(Fault(...))
# Fault controls venue behavior; it has no engine/ledger reference.

# drivers/runtime.py — same coordinator/core for both operating modes
await runtime.run_stream(stop_event) -> RunReport
await runtime.run_check() -> CheckReport
```

`ExecutionBatch` reports reserved/submitted/adopted/withdrawn/blocked ids and
incidents; it does not return success merely because requests were queued.
`ExecutionSnapshot.in_flight` contains pending immediate actions and partial
orders only; ACKED resting conditionals are in `mirrored`. UNKNOWN recovery
states fence new work and must not be accidentally treated as quiescent.

## Tasks and acceptance tests

### T0 — B2 prelims and schema day identity

Files: `core/live.py`, `core/riskguard.py`, `core/reconcile.py`, journal schema
and relevant tests/docs. This work is already assigned separately.

Exempt both venue-sized reduce-only FLATTEN classes from correction budgets;
correct N6/N9 comments; restore counters by settled bar day and pin a genuine
day rollover. Test exhausted daily/notional budgets still permit flatten
without permitting TOP_UP, and midnight restart cannot reset the old day's
cap. Require migration/refusal behavior for previous schema versions.

### T1 — execution types, ids, normalization

Files: `execution/__init__.py`, `execution/types.py`, `execution/identity.py`,
adapter protocol/type version changes, `tests/test_execution_identity.py`.

Implement the frozen boundary types and exact decimal filter arithmetic
(`Decimal(str(value))` at venue boundary; float conversion only at core ABI).
Floor quantities to lot step without increasing exposure, retain residuals,
respect market_max_qty, and validate prices/trigger bands from constraints.
Do not add Binance strings to shared modules.

Failure tests: pipe/backslash/unicode ids; same identity across restart;
distinct role/sequence/cycle ids; deliberately stubbed digest collision;
partial close retains residual; nonfinite or negative qty rejected; lot
boundary does not round an entry upward; reduce-only min-notional exemption
is capability-dependent. Confirm non-MARKET and MARKET limits differ.

### T2 — transactional store, inbox/outbox, checkpoints

Files: `execution/store.py`, journal schema/API, `tests/test_execution_store.py`.
Implement the durable records and composable transactions above. Existing
action/fill helper behavior must remain compatible or fail explicitly.

Failure tests: kill subprocess at inbox/cursor, decision/outbox, and
SUBMITTING/ACK boundaries; reopen and prove no lost logical action and no
duplicate fill accounting. Fail a transaction after settlement append and
prove no settlement or reconcile half remains. Conflicting order mapping,
client-id row, receipt, or payload raises; identical replay is idempotent.
Corrupt checkpoint/outbox content refuses recovery. Do not use only in-memory
exceptions as proof of durable crash behavior.

### T3 — deterministic MockExecutor and adapter conformance

Files: `adapters/mock.py`, `adapters/conformance.py`,
`tests/test_mock_executor.py`, `tests/test_adapter_conformance.py`.

Implement all Executor methods, controllable clock, normalized constraints,
immediate MARKET execution, resting LAST/MARK conditionals, cancellation,
pagination/cursors, fees, account/position, and independent account/submit
faults. Inject acceptance followed by timeout, delayed ACK, partial then
terminal, repeated/reordered events, duplicate-id rejection, not-found
grace, retention gaps, rate exhaustion, stale stream, and external reduction.
Record each physical order and each trade in the mock's own venue store.

Failure tests assert actual mock positions/trade counts: timeout followed by
retry/adoption fills once; cancel racing fill preserves the fill; partial
remainder cannot overlap original; both OCO-like legs cannot overclose with
closePosition; event replay leaves fees unchanged. Document the mock's
fidelity and that authored payloads are not golden captures.

### T4 — coordinator and execution receipts

Files: `execution/coordinator.py`, `execution/receipts.py`,
`tests/test_execution_coordinator.py`, `tests/test_execution_receipts.py`.
Depends on T1–T3. Implement state machine, outbox submit, adopt/retry,
partial remainder, fill attribution, post-fill snapshot, and supersession.
Every submit verifies current fence and STOP immediately before transport;
skip submission at `now >= lease_expiry - submit_p99`.

Failure tests: one advance + requote + repeated evaluation yields one
physical order; no requote on a successful evaluation releases original qty;
withdraw and aborted evaluation release none; crash after batch commit adopts
instead of resubmitting with a new id; two-leg reversal closes before entry;
unknown old-token action blocks new-token entry; stale lease never submits.
Correction emitted at n and filled at n+1 gets an identity receipt, changes
basis once, and creates no fabricated or unmatched n+1 engine fill. Wrong-id,
wrong-side, overfill, missing, and partial correction remain visible.

### T5 — supported core restart and residual reconciliation

Files: `core/live.py`, `core/probe.py`, `core/reconcile.py`,
`execution/recovery.py`, `tests/test_execution_recovery.py`.
Depends on T2/T4. Add explicit checkpoint APIs, recovery conversion, and
quantity-only residual reconciliation input as described above. Preserve
the existing reconciler's position-primary, account-secondary rules. Add
resolved probe book metadata so protection sees `level_resolved` from the
actual completed probe accessor window, not a bar later.

Failure tests: interrupted settle/reconcile resumed twice equals uninterrupted
actions/classifications/counters; open-position entry needs n-1 delta and is
reconstructed; an offset bracket becomes mirrorable immediately after its
entry probe; first evaluation after restart cannot emit duplicate TRIGGER;
carried MISSED ages, G3 samples, day caps, pending notices, and stale-cycle
state survive. Late bar-n partial fill resolves only its origin obligation;
a terminal correction residual causes one bounded repair, never an engine
fill or correction loop. Seed-only recovery and missing prehistory refuse.

### T6 — protection and rate lanes

Files: `execution/protection.py`, `execution/rate_limit.py`,
`tests/test_execution_protection.py`, `tests/test_execution_rate_limit.py`.
Keep pure desired-set calculations separate from coordinator mutations.

Mirror resolved eligible exits only: LAST STOP_MARKET/TAKE_PROFIT_MARKET,
full-position closePosition where supported. Follow mode executes probe
triggers. Partial/pyramided brackets refuse mirror admission. Refresh levels
using tick dead band and minimum interval; create-before-cancel or a verified
amend; increment durable level_version only on semantic changes. Cancel old
cycle orders on the flattening fill event before the next cycle's entry.
Desired-set sync must cancel a MISSED exit's leftover venue order.

Dead-man uses MARK, spec §5.1 width interval and current constraints. Preflight
the feasible interval before entry, arm at entry ACK, refresh on fill/size/side,
cancel on flat. Failed/late arm leads FLAT_ONLY plus flatten; fired protection
leads HARD/FLATTEN. `hold_expired` produces the same durable emergency flatten
obligation; respect venue market_max_qty with sequenced chunks/remainder.
DISASTER is deterministic replace-not-add per epoch/side, promoted or cancelled
at next check before ordinary orders.

The discretionary rate lane cannot spend the emergency reserve. Both obey
actual exchange limits; reserve exhaustion records inability and retries
within the protection deadline, never claims a submission. Attempt emergency
write-ahead; on JournalFault write the existing out-of-band log and retain a
deterministic identity derived from last good durable protection state before
submitting. Replay/adoption of that log must be demonstrated before enabling
this fallback. Discretionary write failure always prevents submit.

Failure tests: entry ACK then process failure still leaves protection/adoptable
arm obligation; dead-man arm timeout flattens; empty width interval refuses
entry; create failure retains old stop; old-cycle cancellation completes before
new entry; emergency reserve survives discretionary exhaustion; journal failure
blocks ENTRY while emergency flatten uses recoverable id once; DISASTER rerun
does not add duplicate pairs; HOLD expiry survives restart.

### T7 — stream/check runtime and feed/account risk

Files: `drivers/runtime.py`, `drivers/state.py`, `execution/health.py`,
`tests/test_runtime_stream.py`, `tests/test_runtime_check.py`,
`tests/test_runtime_health.py`.

Use one serialized core worker; never call a handle concurrently. Stream
coalesces H/L/C changes at `max_eval_rate`, journals dropped tick ranges,
gives settle priority via abort and waits for the probe to finish, buffers
ticks during settle, deduplicates sequences, and REST-heals close watchdogs.
Compare forming vs confirmed bars and preserve the existing synthesis policy.
Store source/mismatch evidence before raising configured STOP.

Check acquires a lease, recovers prior actions before counting new
non-quiescence, processes contiguous missed closes, gets an actual forming
open, evaluates, drains immediate actions to terminal and mirrors to ACKED,
then evaluates/protects again after entry fill. Drain timeout arms DISASTER,
journals the result, and reports unsuccessful completion. Persist last-check
time and measure duration; enforce configured polling/admission bounds.

Enforce declared risk inputs: feed/eval staleness from successful event and
evaluation timestamps; bar mismatch streak; recompute p99 from real samples;
liquidation distance from venue mark/liquidation price; unexplained equity
divergence after funding/fee accounting; realized daily loss from normalized
venue realized-PnL/income records. Current `Fill`/`AccountState` cannot deliver
authoritative realized PnL: add an adapter income/account envelope and durable
dedupe before claiming a daily loss cap. Missing required observations block
admission/entries, not silently disable limits. `max_entry_slip_bps` is explicit
epoch/runtime configuration backed by the measured lane, not the 50-bps default.

Failure tests: settle arriving during probe produces no stale action; duplicate
tick/catch-up close cannot execute twice; confirmed-bar revision STOPs; account
poll before final fill cannot be accepted as quiescent; both driver modes on
the same deterministic event schedule reproduce action ids/receipts; missed
cron + lease takeover adopts before entry; false-open timeout creates no
order; stale stream and stale evaluation each block entry; duplicated funding
and realized-PnL income cannot launder the daily cap across midnight/restart.

### T8 — Binance transport and instrument source

Files: `adapters/binance_usdm/` for transport, normalization, instruments,
bars/ticks, executor, recorded fixtures, and adapter-specific tests.
Depends on the conformance contract; keep transport injectable. Implement
signed REST and user-data WS, testnet/prod endpoints as explicit config,
redacted diagnostics, independent account/submit errors, clock skew,
rate headers, listen-key renewal, and pagination retention semantics.
Binance constants and rejection mapping stay here.

Resolve actual EngineSyminfo and constraints; production `exchangeInfo`
supplies filters even for a testnet execution lane, with its snapshot digest
recorded. Assert account currency, multiplier, one-way mode, leverage/margin,
and tick size against epoch metadata. Surface `userTrades` without client ids
through order-id adoption. Unsupported/unknown rejection codes fail closed;
CONVERT requires a matching probe-confirmed gap-through exit.

Failure tests use recorded/authored labelled fixtures for conditional flags,
bands, reduce-only with no position, duplicate client ids, unmapped trades,
token expiry, pagination and signed request serialization. Sanitized golden
captures must retain provenance/time/API version/filter snapshot and be
obtained from real testnet activity; leave their evidence status missing
until collected. Label liquidation/ADL/PERCENT_PRICE authored rows as such.
No tests require or print user credentials. Do not send real or testnet orders
merely to make this task's local tests pass.

### T9 — CLI, admission, evidence, and operator recovery

Files: CLI/config loader, `execution/admission.py`, `docs/execution.md`,
`tests/test_execution_cli.py`, `tests/test_execution_admission.py`.

Add `run` and `check` with mock default, explicit adapter/mode, config and
journal. Include structured output covering actions, receipts, protection,
missing evidence, and STOP. Add fenced `stop-inspect` and `stop-clear --cause`:
clearing is an explicit operator command, with journal clear before sidecar
clear so an interruption never silently resumes an uncleared STOP. Obtain
the exclusive lease and refuse an active writer. Journal faults are not
made recoverable by deleting their marker.

Add an `AdmissionEvidence` record keyed by exact epoch/code/config/adapter,
constraint snapshot, instrument, timeframe, mode, feature verdict and measured
L0–L3 lane reports. Missing features default BLOCKED. Assert input/script TF,
tail/ABI/export identity, bar/reference and syminfo hashes, required overrides,
fee/slippage and margin compatibility, G3 self-test, protection capabilities,
grace >= measured recompute_p99 + submit_p99, and check-duration/poll bounds.
Unknown pricescale/minmove codegen delivery and missing registry/lint verdicts
remain blocked until the separate cross-repo work supplies evidence.

Production order submission requires BOTH an explicit production-order flag
and a passing admission report for that exact deployment. Neither mock
success nor a CLI flag may manufacture a green report. Testnet likewise has
an explicit execution opt-in and separate evidence status. No credentials,
golden captures, measured slippage distributions, key permissions, or canary
results are invented. Actual exchange writes and production rollout are
separate authorized operational steps; no push is included here.

Failure tests: mock can run offline; production adapter without explicit
enablement cannot call transport; flag with missing/wrong-epoch evidence still
cannot submit; every unknown feature is blocked; stop-clear under a live
lease fails; interruption during clear remains stopped. A subprocess CLI
fault writes a parseable failure report and nonzero exit instead of losing
the report behind a traceback.

## Completion evidence

For each task, report code paths, behavior tests, unresolved dependencies,
and exact commit/test mode. Run the existing engine-free and engine-present
suites after integration, then the three existing L1 fixtures to prove B2
ledger/probe behavior remains intact. Add local coordinator replay over the
SMA, bracket and POOC fixtures with the actual mock (not an echo of settled
fills), including the n→n+1 receipt and crash cases above.

B3 local completion means both driver modes can run and recover against the
mock without double physical orders or silently discarded receipts, and
the real adapter parses its declared fixture surface. Exchange conformance,
L2/L3 calibration, registry/lint admission and L4 paper/canary remain separately
reported evidence. The README must state the actual boundary reached; it
must not claim trading readiness from local tests.
