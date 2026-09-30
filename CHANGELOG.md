# Changelog

## Unreleased

- Supports PineForge engine v1.0.0 and codegen 1.0.0; the README builds
  against the engine's `v1.0.0` tag and names codegen 1.0.0 as its pair.
- Engine v1.0.0 reports every `strategy.entry` as ENTRY in the pending-order
  mirror. An entry with neither a limit nor a stop level is still keyed as a
  MARKET intent, so market entries keep their settle-time `MARKET_AT_OPEN`
  legs and intent keys.

## 0.1.0 — 2026-09-09 (pre-alpha)

Initial standalone, pre-alpha runtime for PineForge-compiled strategies.

- One runner accepts script-timeframe feeds or ticks/confirmed 1m OHLCV for
  higher script timeframes; `mock-feed` generates replay inputs.
- Broker-neutral simulated-fill webhooks with stable event IDs, durable
  ordered delivery, retries, optional HMAC and queue recovery commands.
- Confirmed bar-close actions and optional provisional intrabar updates.
- Minute aggregation, explicit session calendars, declared sparse-input
  policy and immutable auxiliary history for supported security queries.
- Restart-safe SQLite state, input checksums and a sample deduplicating
  webhook receiver.
- Apache-2.0 licensing, attribution notices, contributor documentation and
  distribution/CI preparation.

The [recorded two-input verification](docs/two-input-verification.md) passed
35 selected probes in three feed variants with 201 actual HTTP action
deliveries. It establishes bounded live/batch equivalence on identical input;
it is not a full campaign grade or proof of real broker fills. Known scope
and source-data differences remain in that report and the README.
