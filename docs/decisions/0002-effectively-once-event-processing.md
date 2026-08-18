# ADR 0002: At-least-once delivery with effectively-once analytics

- **Status:** Accepted
- **Date:** 2026-08-06
- **Owners:** Data platform

## Context

Network timeouts can occur after a broker or warehouse has accepted a record but before
the sender receives confirmation. Claiming exactly-once behavior across an API,
Redpanda, Snowpipe Streaming, and Snowflake would hide these ambiguity windows.

The analytical result must still remain correct when producers, consumers, or the
warehouse retry work.

## Decision

Guardrail explicitly provides at-least-once transport and effectively-once analytical
processing:

1. Producers generate a stable `event_id` for the logical product action.
2. The Redpanda producer enables idempotence, requests acknowledgement from all
   replicas, and keys events by experiment subject where possible.
3. Every acknowledged API response includes the broker topic, partition, and offset.
4. The sink consumer disables automatic commits and retains source topic, partition,
   offset, transport batch, and message identifiers in the raw payload.
5. Snowpipe Streaming channel offsets protect ingestion progress. They do not replace
   the product-level `event_id`.
6. Raw delivery is append-only and may contain duplicates. The clean analytical table
   uses an idempotent merge to retain one canonical row per `event_id`.
7. The sink commits the next Redpanda offset only after Snowflake reports a balanced
   producer-to-raw-to-classified reconciliation. A failed or unbalanced batch remains
   eligible for redelivery.

## Consequences

- Raw counts describe transport attempts; deduplicated counts describe logical events.
- A retry after an ambiguous warehouse result can add another auditable raw row without
  duplicating the analytical event.
- Consumers must remain idempotent and source offsets must be observable.
- Interview and operational documentation must avoid an end-to-end exactly-once claim.

## Alternatives considered

- **End-to-end exactly once:** rejected because the external system boundary makes the
  claim misleading.
- **Commit broker offsets before warehouse reconciliation:** rejected because a
  warehouse failure could cause silent loss.
- **Drop duplicates at ingestion:** rejected because it destroys delivery evidence and
  makes incident analysis harder.
