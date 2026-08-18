# Local production acceptance

This harness runs Guardrail with production settings on Docker while keeping it isolated from
the ordinary development stack. It uses PostgreSQL with TLS-only host connections, Redpanda with
TLS and SCRAM authentication, a TLS mock OIDC issuer, the production API image, and an HTTPS Nginx
gateway. Only `https://localhost:8443` is published to the host.

The mock issuer and fixed credentials are test fixtures. They must never be copied into a deployed
environment.

## Run the complete acceptance

Start Docker Desktop, then run from the repository root:

```bash
./scripts/run-local-production-acceptance.sh
```

The command builds the application image once, creates an isolated `guardrail-prodtest` Compose
project, runs every phase below, and leaves the passing stack online for manual testing. It does
not modify the normal Compose project's containers or volumes.

| Gate | Automated proof |
| --- | --- |
| Production boot | Production configuration, Alembic migration, PostgreSQL readiness, OIDC readiness, and broker security bootstrap all succeed. |
| TLS boundaries | The API verifies the OIDC and PostgreSQL CA; PostgreSQL rejects plaintext; Redpanda uses TLS plus SCRAM. |
| Authentication | Anonymous, malformed, expired, and wrong-audience tokens return `401`. |
| Authorization | Viewer writes return `403`; operator writes succeed; unknown external roles are ignored; approval-subject impersonation is rejected. |
| HTTP hardening | HSTS, CSP, `nosniff`, frame denial, request IDs, CORS, trusted hosts, hidden production docs, and the request-size limit are checked. |
| State and concurrency | An experiment is created and launched; 348 parallel/repeated assignment requests stay identical; PostgreSQL contains exactly one assignment identity. |
| Local load | Assignment latency must remain below 1 second at p95 for the 300-request local sample. This is a regression gate, not a capacity benchmark. |
| Telemetry | The API receives a broker acknowledgement over TLS/SCRAM and an independent consumer finds the exact event in the three-partition topic. |
| Persistence | The API is restarted and both the experiment and stable assignment remain unchanged. A second Alembic run is idempotent. |
| Recovery | `pg_dump` is restored into a second database and row counts for all five operational tables match. PostgreSQL and Redpanda are each stopped, safe failure behavior is checked, and recovery is verified. |
| Observability and leakage | Stable Prometheus route labels are present and container logs are scanned for the local database and broker credentials. |

Passing evidence is written to ignored local files:

```text
.local-production/ca.crt
.local-production/evidence.json
```

## Manual API validation

The stack must already be running from the complete acceptance command.

First confirm that the host sees the trusted HTTPS gateway and that production API documentation
is disabled:

```bash
curl --cacert .local-production/ca.crt https://localhost:8443/health/ready
curl --cacert .local-production/ca.crt \
  --output /dev/null --write-out '%{http_code}\n' \
  https://localhost:8443/docs
```

Expected results are `{"status":"ready","store":"postgres"}` and HTTP `404`.

Obtain short-lived tokens from the local mock issuer. The helper accepts only the five Guardrail
roles and writes the token to standard output:

```bash
OPERATOR_TOKEN=$(./scripts/get-local-production-token.sh \
  guardrail_experiment_operator manual-operator)
ASSIGNMENT_TOKEN=$(./scripts/get-local-production-token.sh \
  guardrail_assignment_client manual-assignment-client)
VIEWER_TOKEN=$(./scripts/get-local-production-token.sh \
  guardrail_viewer manual-viewer)
```

Create, launch, assign, and read an experiment through the HTTPS gateway:

```bash
EXPERIMENT_ID="manual_$(date -u +%Y%m%d%H%M%S)"

curl --fail-with-body --cacert .local-production/ca.crt \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"experiment_id\":\"${EXPERIMENT_ID}\",\"name\":\"Manual production check\",\"hypothesis\":\"State remains durable\",\"primary_metric\":\"order_conversion_rate\"}" \
  https://localhost:8443/experiments

curl --fail-with-body --cacert .local-production/ca.crt \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" \
  -X POST \
  "https://localhost:8443/experiments/${EXPERIMENT_ID}/launch"

curl --fail-with-body --cacert .local-production/ca.crt \
  -H "Authorization: Bearer ${ASSIGNMENT_TOKEN}" \
  "https://localhost:8443/assignments/${EXPERIMENT_ID}/manual-subject"

curl --fail-with-body --cacert .local-production/ca.crt \
  -H "Authorization: Bearer ${VIEWER_TOKEN}" \
  "https://localhost:8443/experiments/${EXPERIMENT_ID}"
```

Repeat the assignment request. `variant`, `bucket`, and `allocation_version` must be identical.
Restart the API and repeat both reads to prove the result is not process-local:

```bash
docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml \
  restart guardrail-api-prodtest
```

## Verify where the data landed

Operational state is authoritative in the isolated PostgreSQL volume:

```bash
docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml \
  exec -T postgres-prodtest psql -U guardrail -d guardrail \
  -c "SELECT experiment_id, state, treatment_percentage, allocation_version FROM experiments WHERE experiment_id = '${EXPERIMENT_ID}';"

docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml \
  exec -T postgres-prodtest psql -U guardrail -d guardrail \
  -c "SELECT experiment_id, subject_id, variant, bucket, allocation_version FROM assignments WHERE experiment_id = '${EXPERIMENT_ID}';"
```

| Flow data | Local production landing point | Deployed production landing point |
| --- | --- | --- |
| Experiment configuration/current state | PostgreSQL `experiments` | Managed PostgreSQL `experiments` |
| Stable assignment | PostgreSQL `assignments` | Managed PostgreSQL `assignments` |
| Approved decision | PostgreSQL `rollout_decisions` | Managed PostgreSQL `rollout_decisions` |
| Applied state transition | PostgreSQL `state_transitions` | Managed PostgreSQL `state_transitions` |
| Pending analytics replication | PostgreSQL `outbox_events` | Managed PostgreSQL `outbox_events`, then Snowflake control tables |
| Product telemetry | TLS/SCRAM Redpanda topic `guardrail.product-events.prodtest.v1` | Managed Redpanda, then Snowflake raw/clean/fact models through the sink |
| Acceptance evidence | `.local-production/evidence.json` plus the isolated evidence volume | Release evidence store selected by the operator |

The local harness deliberately does not connect to Snowflake. Its telemetry assertion ends only
after an independent consumer reads the exact acknowledged event from Redpanda.

## Additional host checks

Confirm that only the gateway has a published port and that the API runs as the unprivileged
service account:

```bash
docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml ps

docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml \
  exec -T guardrail-api-prodtest id
```

The gateway should show `0.0.0.0:8443->8443/tcp`; backend services should show container ports
only. The API identity must be UID/GID `10001`.

## Stop or reset

Stop containers while preserving the isolated database, broker, certificates, and evidence:

```bash
docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml down
```

Delete only this test project's volumes for a clean rerun:

```bash
docker compose --project-name guardrail-prodtest \
  --file docker-compose.production-test.yml down --volumes
```

## What still requires a target environment

Passing this suite proves the production code paths locally; it is not customer-traffic sign-off.
A real environment must still prove multi-replica peak load and soak, managed PostgreSQL RPO/RTO,
replicated broker failure, real OIDC mappings, Kubernetes network policy, secret rotation, alert
routing, Snowflake sink/outbox reconciliation, and canary rollback.
