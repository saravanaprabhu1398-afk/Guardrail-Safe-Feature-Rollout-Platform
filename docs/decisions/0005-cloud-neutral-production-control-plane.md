# ADR 0005: Cloud-neutral production control plane

- **Status:** Accepted
- **Date:** 2026-08-18
- **Owners:** Platform engineering

## Context

The portfolio implementation deliberately keeps current experiment state and assignment
records in one process. That is reproducible locally but cannot survive restarts, coordinate
replicas, authenticate operators, or apply rollout changes atomically. Production also needs
portable deployment without coupling the control plane to one cloud vendor.

## Decision

1. Kubernetes is the portable workload boundary; Terraform installs a versioned Helm chart.
2. Managed PostgreSQL is the authoritative operational store for experiment configuration,
   allocation versions, assignments, rollout decisions, transitions, and an outbox.
3. Snowflake remains the analytical system of record for telemetry, metric artifacts,
   immutable evidence snapshots, and replicated decision history.
4. Managed Redpanda remains the durable telemetry transport. Consumer offsets advance only
   after the existing idempotent Snowflake correctness boundary.
5. OIDC bearer tokens authenticate users and workloads. Application roles authorize viewer,
   experiment-operator, and rollout-approver actions.
6. A transactional outbox couples an approved PostgreSQL state change to eventual,
   idempotent Snowflake decision replication. No distributed transaction is attempted.
7. Production configuration fails closed when PostgreSQL or OIDC settings are missing.
   Secrets are supplied through an existing Kubernetes Secret, never Helm values or images.

## Consequences

- API replicas can scale horizontally without assignment or state divergence.
- Rollout state and its approval record commit together in PostgreSQL.
- Snowflake replication is eventually consistent and observable through outbox lag/retries.
- Operators need managed PostgreSQL, OIDC, Redpanda, Snowflake, ingress, and secret-management
  endpoints before installing the chart.
- Local development retains the in-memory backend and disabled authentication by default.

## Alternatives considered

- **Snowflake as the operational state store:** rejected because warehouse latency and OLAP
  concurrency semantics are a poor fit for assignment and optimistic control-plane updates.
- **Cloud-specific serverless services:** rejected because the requested baseline is portable.
- **Distributed PostgreSQL/Snowflake transactions:** rejected because they are unavailable and
  would be more fragile than an idempotent outbox.
- **Unauthenticated private ingress:** rejected because network location is not authorization.
