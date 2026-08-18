# ADR 0003: Append-only raw retention and lineage-preserving replay

- **Status:** Accepted
- **Date:** 2026-08-06
- **Owners:** Data platform and governance

## Context

Contract defects, late data, and producer bugs must be diagnosable after clean models
change. Updating malformed raw rows in place would erase what the platform actually
received and make replay results difficult to defend.

## Decision

1. `RAW.PRODUCT_EVENTS` is an immutable delivery ledger. Application workflows do not
   update or delete product-event rows.
2. Each row preserves the original JSON payload, canonical SHA-256 payload hash,
   producer fields, ingestion batch/channel offsets, ingestion timestamp, and any
   source transport lineage.
3. Invalid rows are copied into `RAW.REJECTED_EVENTS` with a structured reason. Their
   original raw row remains unchanged.
4. A correction is a new event delivery. Replay records preserve the rejection ID,
   original batch and ingestion IDs, correction hash, requester, attempts, status, and
   resulting batch.
5. Identical correction requests resolve to the same deterministic replay identity and
   return the completed result rather than creating a second logical replay.
6. Development Snowflake Time Travel is configured for one day to control cost. This
   is separate from current-table raw retention; a production purge and archival policy
   must be approved before non-synthetic data is introduced.

## Consequences

- Every analytical row can be traced back to an immutable delivery attempt.
- Reprocessing logic can improve without destroying historical evidence.
- Storage grows with retries and replays, so retention, clustering, and archival costs
  must be measured before production scale.
- Deletion requirements would need a governed tombstone or purge workflow rather than
  ad hoc updates.

## Alternatives considered

- **Correct raw rows in place:** rejected because it removes evidence.
- **Keep only rejected payloads:** rejected because successful transformations also need
  reproducible source lineage.
- **Retain raw data indefinitely by default:** rejected as an unjustified cost and
  governance commitment for a portfolio environment.
