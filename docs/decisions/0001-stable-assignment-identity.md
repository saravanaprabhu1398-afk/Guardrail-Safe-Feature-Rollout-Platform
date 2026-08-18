# ADR 0001: Stable assignment identity and allocation versioning

- **Status:** Accepted
- **Date:** 2026-08-06
- **Owners:** Experimentation platform

## Context

An experiment result is invalid if the same subject is assigned inconsistently or if
an allocation change rewrites historical assignment evidence. Random assignment on
every request is also operationally fragile because retries and independent service
instances can disagree.

Guardrail must support progressive allocation changes while preserving which rule was
used for every assignment and exposure.

## Decision

1. The assignment identity is `(experiment_id, subject_id)`. The current simulator
   uses its stable user identifier as `subject_id`; anonymous-to-known identity mapping
   will be modeled separately and will not rewrite historical events.
2. The service computes one deterministic bucket in `[0, 9999]` from the first eight
   bytes of `SHA-256("<experiment_id>:<subject_id>")`.
3. Allocation configuration maps bucket ranges to variants. The hash algorithm and
   namespace are part of the platform contract and cannot change silently.
4. Every allocation change creates a new immutable `allocation_version`. Assignment
   records are unique for `(experiment_id, subject_id, allocation_version)`.
5. Repeated requests for the same identity and version return the stored assignment.
   A new version may intentionally change a subject's variant as rollout expands, but
   the old assignment remains available and all exposure analysis retains its version.
6. Metrics use recorded exposure rather than assignment alone. Cross-version movement
   is visible to integrity checks and cannot be mistaken for unexplained variant
   flicker.

## Consequences

- Retries and horizontally scaled assignment services make the same decision without
  coordination.
- Allocation expansion is reproducible and auditable.
- Changing hash input, normalization, or algorithm requires a new ADR and migration.
- Phase 3 identity models must preserve both anonymous and known identifiers and must
  not retroactively alter assignment facts.

## Alternatives considered

- **Random choice per request:** rejected because it is not stable across retries.
- **Persist first assignment without deterministic hashing:** stable after the first
  write, but harder to reproduce during recovery and requires coordination before any
  response.
- **Include allocation version in the hash:** rejected because it reshuffles the entire
  population on every ramp rather than preserving a stable ordering.
