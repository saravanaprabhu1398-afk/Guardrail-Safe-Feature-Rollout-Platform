# ADR 0004: Synchronous classification before source-offset commit

- **Status:** Accepted
- **Date:** 2026-08-06
- **Owners:** Data platform

## Context

The local MVP needs a simple, demonstrable correctness boundary. After Redpanda accepts
an event, the sink must decide when it is safe to advance its consumer offset. Fully
asynchronous Snowflake task graphs add operational state and retry paths before the
core models exist, while performing warehouse work in the API request would couple
product latency to Snowflake.

## Decision

1. The API returns HTTP 202 after Redpanda durably acknowledges the event batch. It does
   not wait for Snowflake classification.
2. The sink worker synchronously waits for Snowpipe Streaming commit,
   `CONTROL.PROCESS_INGESTION_BATCH`, and balanced reconciliation.
3. The worker commits Redpanda offsets synchronously only after that complete boundary.
4. Provider failure, unbalanced reconciliation, or commit failure leaves the source
   work retryable. All warehouse writes remain idempotent.
5. The direct-Snowpipe development path and corrected-event replay remain synchronous
   for now and return their final ingestion evidence.
6. A Snowflake task graph may supersede the classification portion when Phase 3 models
   exist. Migration must retain the same raw batch identity, reconciliation evidence,
   retry semantics, and source-commit safety boundary.

## Consequences

- Product-facing API latency is isolated from Snowflake latency.
- The local worker is easy to reason about and demonstrate.
- One slow warehouse batch occupies a consumer worker until it succeeds or fails.
- Scaling to multiple workers requires distinct Snowpipe channels and monitoring for
  partition lag, task failures, and stuck batches.

## Revisit triggers

Revisit this decision when sustained traffic, warehouse latency, multi-worker scaling,
or independent reprocessing requires durable asynchronous orchestration. The task graph
must be observable before the source offset is allowed to advance.

## Alternatives considered

- **Classify inside the API request:** rejected because warehouse latency would affect
  the product control plane.
- **Commit immediately after raw Snowpipe ingestion:** rejected because downstream
  classification failure could become invisible to the source consumer.
- **Introduce a task graph before the clean/core models exist:** deferred because it
  adds operational surface without improving the current correctness proof.
