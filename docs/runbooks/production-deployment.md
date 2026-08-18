# Production deployment and acceptance

This runbook deploys the cloud-neutral Guardrail runtime to an existing Kubernetes
cluster. The application chart does **not** create databases, brokers, identity providers,
DNS, TLS certificates, or secrets. Those services must be managed and recoverable before
Guardrail is installed.

## Production topology

1. Clients authenticate with the organization's OIDC provider and call the API through TLS.
2. API replicas authorize the token's Guardrail role and persist operational state in managed
   PostgreSQL.
3. Assignment, experiment, decision, transition, and outbox writes are transactional. A rollout
   decision, approval record, state mutation, and outbox record either all commit or all roll back.
4. Product events are acknowledged by managed Redpanda and the event-sink worker writes them to
   Snowpipe Streaming. Its source offset advances only after balanced Snowflake reconciliation.
5. The outbox worker replicates PostgreSQL rollout outcomes into Snowflake. Snowflake is therefore
   eventually consistent for decision history; PostgreSQL remains authoritative.
6. Snowflake computes integrity evidence, metrics, analysis snapshots, and rollout evidence.

## Where data lands

| Data | Authoritative landing point | Secondary/analytical copy |
| --- | --- | --- |
| Experiment configuration and current rollout state | PostgreSQL `experiments` | None |
| Stable subject assignment per allocation version | PostgreSQL `assignments` | Snowflake facts after exposure/event processing |
| Approved rollout recommendation | PostgreSQL `rollout_decisions` | Snowflake `CONTROL.ROLLOUT_DECISIONS` through outbox |
| Human approval and applied state transition | PostgreSQL `state_transitions` | Snowflake `CONTROL.STATE_TRANSITIONS` through outbox |
| Pending/retrying analytics replication | PostgreSQL `outbox_events` | Cleared from pending only after Snowflake succeeds |
| Raw product telemetry | Snowflake raw event table via Redpanda and Snowpipe Streaming | Clean, fact, and metric models in Snowflake |
| Invalid telemetry and replay lineage | Snowflake rejection and replay control tables | Corrected event returns through the ingestion boundary |
| Health/statistical evidence | Snowflake `CONTROL` immutable snapshots | IDs are referenced by PostgreSQL decisions |

## Prerequisites

- A supported Kubernetes cluster with metrics-server, an ingress controller, DNS, and TLS.
- Managed PostgreSQL with TLS required, automated backups, point-in-time recovery, connection
  limits sized for the API and worker pools, and a tested restore procedure.
- Managed Redpanda with replication, TLS/SASL, the `guardrail.product-events.v1` topic, retention,
  quotas, and broker monitoring.
- Snowflake objects bootstrapped by the existing setup commands and a least-privilege service PAT.
- An OIDC application/audience for `guardrail-api`. Tokens must expose a string-list `roles` claim.
- An OIDC-aware ingress/proxy for the optional Streamlit UI. It must overwrite (not append)
  `X-Forwarded-Access-Token` with the authenticated user's API access token and prevent direct
  access to the UI service. The API still validates that token independently.
- An external secret manager or sealed-secret controller that materializes:
  - `guardrail-database` / `database-url`: a `postgresql+psycopg://` URL with
    `sslmode=require`, `verify-ca`, or `verify-full`.
  - `guardrail-snowflake` / `pat`: the role-restricted Snowflake PAT.
  - `guardrail-redpanda` / `sasl-password`: the managed Redpanda service credential.
- An immutable application image tag. Do not deploy `latest`.

Application roles are:

| Role | Access |
| --- | --- |
| `guardrail_viewer` | Read experiments, health, results, decisions, and `/me` |
| `guardrail_experiment_operator` | Create/launch experiments, replay, and simulate |
| `guardrail_rollout_approver` | Apply a transition; `approved_by` must equal token `sub` |
| `guardrail_assignment_client` | Obtain stable assignments |
| `guardrail_telemetry_producer` | Submit product-event batches |

## Publish the release image

Every successful push to `main` publishes the application image to GHCR only after the complete
test, migration, dependency-audit, container, and vulnerability gates pass. The release workflow
publishes `sha-<full-git-sha>` and deliberately does not publish `latest`. A `v*` Git tag also
publishes its semantic version tag. The image includes a BuildKit SBOM and provenance, plus a
GitHub-signed provenance attestation tied to the registry digest.

Use the image and digest printed in the `Publish immutable application image` job summary. Verify
the attestation before promoting the image:

```bash
gh attestation verify \
  oci://ghcr.io/OWNER/REPOSITORY:sha-FULL_GIT_SHA \
  --repo OWNER/REPOSITORY
```

If the GHCR package is private, configure the cluster's registry credential and set the Helm
`imagePullSecrets` value. Production promotion must use the SHA tag from the successful workflow;
do not manually rebuild the same tag.

## Deploy

Copy `infrastructure/terraform/terraform.tfvars.example` to an ignored environment-specific
file and replace every placeholder. The database URL and PAT are deliberately not Terraform
variables so they do not enter Terraform state.

```bash
terraform -chdir=infrastructure/terraform init
terraform -chdir=infrastructure/terraform apply -target=kubernetes_namespace_v1.guardrail
kubectl -n guardrail wait --for=create secret/guardrail-database --timeout=5m
kubectl -n guardrail wait --for=create secret/guardrail-snowflake --timeout=5m
kubectl -n guardrail wait --for=create secret/guardrail-redpanda --timeout=5m
terraform -chdir=infrastructure/terraform plan -out=guardrail.tfplan
terraform -chdir=infrastructure/terraform apply guardrail.tfplan
```

The one-time targeted namespace apply gives the external secret controller a namespace in which
to materialize credentials before Helm's pre-install migration hook starts. Subsequent releases
use an ordinary full plan/apply; do not use targeted applies for upgrades.

The Helm pre-install/pre-upgrade job runs Alembic before application pods roll. Migrations must
remain backward compatible with the currently running image (expand/migrate/contract). A failed
migration blocks the release.

Verify the rollout:

```bash
kubectl -n guardrail get deploy,pod,job,svc,ingress,pdb,hpa
kubectl -n guardrail rollout status deploy/guardrail-guardrail-api --timeout=5m
kubectl -n guardrail logs job/$(kubectl -n guardrail get job -l app.kubernetes.io/component=migration -o name | tail -1)
```

## Manual acceptance

Export short-lived OIDC tokens into the shell from approved identity tooling. Never paste tokens
into source, tickets, or shared transcripts.

1. **Probe behavior:** `/health/live` returns `200`; `/health/ready` returns `200` only with a
   working PostgreSQL connection; production `/docs` returns `404`.
2. **Authentication:** an anonymous protected call returns `401`; a viewer attempting a write
   returns `403`; a correctly scoped caller succeeds.
3. **Request security:** responses contain `X-Request-ID`, HSTS, CSP, `nosniff`, and frame-deny
   headers. An oversized request returns `413`.
4. **Durability:** create and launch a uniquely named canary experiment, restart all API pods,
   then confirm the experiment and assignment remain unchanged.
5. **Replica consistency:** repeatedly request the same subject assignment through the load
   balancer and confirm variant, bucket, and allocation version are identical.
6. **Telemetry:** send a unique event batch, confirm a broker receipt, run the sink, and verify
   producer/raw/validated/rejected/duplicate counts reconcile in Snowflake.
7. **Decision atomicity:** evaluate a canary transition and verify one row in each of
   `rollout_decisions`, `state_transitions`, and `outbox_events`, plus the matching state in
   `experiments`.
8. **Outbox:** wait for `processed_at` to populate and verify matching Snowflake IDs. Stop the
   outbox worker, create another decision, confirm it stays pending, then restart the worker and
   confirm recovery.
9. **Concurrency:** issue parallel assignment calls for one new subject and confirm PostgreSQL has
   exactly one row for that `(experiment_id, subject_id, allocation_version)` identity.
10. **Observability:** scrape `/metrics`, verify stable route labels, and confirm API/outbox/event
    worker logs are collected without tokens, PATs, or database credentials.

Useful operational queries:

```sql
SELECT COUNT(*) AS pending,
       MAX(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - occurred_at))) AS oldest_seconds
FROM outbox_events
WHERE processed_at IS NULL AND dead_lettered_at IS NULL;

SELECT event_id, aggregate_id, attempt_count, last_error, dead_lettered_at
FROM outbox_events
WHERE dead_lettered_at IS NOT NULL
ORDER BY dead_lettered_at DESC;
```

## Release and rollback rules

- First deploy to a production-like environment and pass the full acceptance list.
- Use immutable image tags and retain at least ten Helm release revisions.
- Roll back application code with `helm rollback` or the previous Terraform image tag only when
  the database migration is backward compatible.
- Do not automatically run Alembic downgrades in production. Restore a database backup only under
  an incident plan that accounts for writes made after the backup.
- Pause experiment ramps if PostgreSQL, Snowflake evidence, outbox replication, or telemetry
  reconciliation is unhealthy. Assignment reads may continue only while PostgreSQL is healthy.

## Production sign-off gates

The platform is not ready for customer traffic until all of these have evidence attached to the
release:

- Restore test meets the agreed RPO/RTO.
- Peak-load and soak tests meet API latency/error SLOs with at least two API replicas.
- OIDC role mapping and approval impersonation tests pass with the real identity provider.
- Network policy, TLS, database TLS, Redpanda SASL/TLS, secret rotation, and image scanning pass.
- Alert routing is tested for API errors/latency, PostgreSQL saturation, broker lag, Snowflake
  ingestion failures, outbox age/retries/dead letters, and failed migrations.
- A canary deployment and rollback rehearsal both complete successfully.
