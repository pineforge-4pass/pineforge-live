# Offline execution and recovery

The local B3 path turns core `ActionRequest`s into durable mock-venue orders.
It exercises real submit, lookup, order history and fill history calls through
the `Executor` protocol. The mock fills from prints supplied by the replay;
it never reads a future engine settlement to decide which orders to fill.

This is an offline execution lane. There is no Binance adapter, live account
connection, or production command in this repository. The risk and protection
modules are tested components; the replay does not submit protective orders
or claim an account P&L model.

## Run it

Use the same ABI-v4 engine and derived feed as the README's L1 quickstart.
Each run requires a fresh journal directory; reports preserve their input
library/feed digests and epoch identity.

```sh
python3 -m pineforge_live.cli execution-replay \
  "$PINEFORGE_ENGINE_ROOT/corpus/validation/ta-sma-152-close-cross-01/strategy.dylib" \
  "$PINEFORGE_ENGINE_ROOT/corpus/data/derived/ohlcv_ETH-USDT-USDT_15m.csv" \
  --journal-dir build/execution-sma-stream \
  --start 2000 --bars 200 --mode stream --restart-after 7
```

On Linux use `strategy.so`. Repeat with `--mode check` and a different output
directory. Both modes consume the same four recorded forming-bar observations;
stream renews ownership and check uses bounded leases. This comparison does
not simulate concurrent sockets, missed cron runs, or deployment latency.

`--restart-after N` closes and recreates the runtime after a committed decision
while the mock venue retains its orders. Separate transaction and subprocess
tests inject crashes around write-ahead/submit boundaries.

`--fault after_accept` injects a timeout after the mock accepted the first
order. Lookup/history adoption resolves it without a second physical order.
`--fault before_accept` produces UNKNOWN and a failing report; the coordinator
will not interpret incomplete absence evidence as permission to retry.

The command prints a compact JSON summary and writes `execution-report.json`.
An unresolved order, terminal residual, late ledger receipt, core STOP or
failed evidence collection makes the command return nonzero. Reports retain
the original failure even when the execution journal cannot be reread.

## Decision and submission boundary

`DurableCore` owns the transaction for each seed, settle or evaluate operation.
It commits ledger/classification records, execution requests, trigger/probe
state, pending notices, daily caps and breaker samples together. No network
call occurs inside that transaction. The coordinator then submits committed
requests after checking the current lease and caller-supplied STOP permission.

A write failure invalidates that runtime instance. A new instance must seed
from the exact checkpoint history, pass G1, restore the checkpoint, and adopt
its existing physical orders. It must use the same journal, epoch, instrument
and runtime/risk configuration. A refused cold start retains STOP evidence
without leaving an orphan settlement behind.

Schema v2 includes the settled bar timestamp in reconcile records. Daily caps
follow that UTC day, including delayed closes and replayed history. Refused
proposals and the two flatten classes do not count as admitted corrections.
Schema-v1 journals are refused before DDL/WAL mutation and remain available
for audit; this pre-alpha change does not offer an in-place migration.

## Identities, notices and receipts

A physical order has an immutable durable identity and client id. Repeated
submissions of the same intended order reuse that identity. A different
physical order allocates another sequence. Reversal entries wait for the
close leg's FILLED state and a venue-flat snapshot, including when the two
Pine ids differ or the close was reserved in an earlier decision. An
ACKED market close remains an active close; an ACKED conditional is resting.

`MARKET_AT_OPEN` initially records a notice. The first successful evaluation
for its target bar folds the requote or withdrawal into that notice before
one physical order is reserved. Withdrawing an entry notice leaves its
reduce-only sibling intact. An aborted evaluation does not release notices
or consume its decision id; the same id can retry a recompute abort.
Current STOP permission is checked again immediately before submission.

Venue trades deduplicate by instrument and venue trade id. Conflicting payloads
fail as journal divergence. Client id and venue order id mappings preserve
attribution when fills omit the client id. Unmapped fills remain external.

`TRIGGER`/next-open fills can match their target engine bar. Settle-emitted
`CORRECTION`, `FLATTEN`, `MARKET_NOW` and `SYNTHETIC_CLOSE` instead receive
explicit action receipts tied to their origin. Their signed quantity enters
the account basis once; no replacement engine fill is invented for bar n+1.
Late ledger receipts, terminal partial residuals and uncertain absent orders
remain explicit unresolved facts. Automatic remainder/chunk submission and
exhaustive NOT_FOUND recovery require further adapter contracts.

## STOP recovery

Inspect the journal before an explicit operator clear:

```sh
pineforge-live journal-inspect build/execution-sma-stream/j.sqlite3
pineforge-live stop-clear build/execution-sma-stream/j.sqlite3 \
  --cause "verified venue position and pending orders"
```

An active journal lease prevents clearing even if another lock-file path is
passed. `--marker` selects a separately mounted STOP sidecar; `--lock` selects
a custom runtime lock. The journal clear and reason commit before the marker
is removed. A failure before that final removal leaves startup stopped.
A corrupt journal must still pass journal validation; clearing a marker does
not repair its bytes.

## Remaining B3 integration

- Instrument resolution and the first real exchange adapter, including
  recorded REST/WS conformance and full observed-order identity.
- Live stream/check orchestration, close watchdogs, healing and stale-event
  fencing; the replay schedule is deliberately finite.
- Durable mirror replacement, entry-ACK dead-man arming and deadlines,
  emergency fallback adoption, HOLD expiry and DISASTER sequencing.
- Authoritative account/PnL/funding inputs for the risk evaluator, persistent
  margin quiet periods and host-wide rate budgets.
- Calibrated L2/L3 evidence, feature lint, exact-epoch admission and L4
  paper/canary evidence. No local test result stands in for these gates.

The [active ledger](../ledger.md) records the continuation's verified state;
[plan-b3.md](plan-b3.md) retains the implementation contract for the remaining
integration.
