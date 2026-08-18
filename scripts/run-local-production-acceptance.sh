#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

compose() {
    docker compose \
        --project-name guardrail-prodtest \
        --project-directory "${repository_root}" \
        --file "${repository_root}/docker-compose.production-test.yml" \
        "$@"
}

wait_for_postgres() {
    attempts=0
    until compose exec -T postgres-prodtest pg_isready -U guardrail -d guardrail >/dev/null; do
        attempts=$((attempts + 1))
        if [ "${attempts}" -ge 30 ]; then
            echo "PostgreSQL did not recover within 90 seconds" >&2
            return 1
        fi
        sleep 3
    done
}

compose run --rm cert-init
compose build guardrail-api-prodtest
compose up --detach --wait gateway-prodtest

compose run --rm --no-deps acceptance python /test-support/acceptance.py setup

compose restart guardrail-api-prodtest
compose run --rm --no-deps acceptance python /test-support/acceptance.py verify

compose run --rm --no-deps migration-prodtest

compose exec -T postgres-prodtest \
    pg_dump -U guardrail -d guardrail --format=custom --file=/tmp/guardrail-prodtest.dump
compose exec -T postgres-prodtest dropdb -U guardrail --if-exists guardrail_restore
compose exec -T postgres-prodtest createdb -U guardrail guardrail_restore
compose exec -T postgres-prodtest \
    pg_restore -U guardrail -d guardrail_restore /tmp/guardrail-prodtest.dump
for table in \
    experiments \
    assignments \
    rollout_decisions \
    state_transitions \
    outbox_events
do
    source_rows=$(compose exec -T postgres-prodtest \
        psql -U guardrail -d guardrail -Atc "SELECT COUNT(*) FROM ${table}")
    restored_rows=$(compose exec -T postgres-prodtest \
        psql -U guardrail -d guardrail_restore -Atc "SELECT COUNT(*) FROM ${table}")
    test "${source_rows}" = "${restored_rows}"
done

compose stop postgres-prodtest
compose run --rm --no-deps acceptance python /test-support/acceptance.py degraded
compose start postgres-prodtest
wait_for_postgres
compose run --rm --no-deps acceptance python /test-support/acceptance.py recovered

compose stop redpanda-prodtest
compose run --rm --no-deps acceptance python /test-support/acceptance.py redpanda-down
compose start redpanda-prodtest
compose run --rm --no-deps redpanda-init
compose run --rm --no-deps acceptance python /test-support/acceptance.py redpanda-recovered

if compose logs --no-color | rg -q \
    'local-prodtest-db-password|local-prodtest-redpanda-admin-password|local-prodtest-redpanda-service-password'; then
    echo "A local production-test credential appeared in container logs" >&2
    exit 1
fi

mkdir -p "${repository_root}/.local-production"
compose cp cert-init:/certs/public/ca.crt "${repository_root}/.local-production/ca.crt"
compose cp cert-init:/evidence/state.json "${repository_root}/.local-production/evidence.json"
chmod 644 \
    "${repository_root}/.local-production/ca.crt" \
    "${repository_root}/.local-production/evidence.json"

echo "Local production acceptance passed."
echo "Gateway: https://localhost:8443"
echo "Trusted CA: ${repository_root}/.local-production/ca.crt"
echo "Evidence: ${repository_root}/.local-production/evidence.json"
echo "Stop: docker compose --project-name guardrail-prodtest --file docker-compose.production-test.yml down"
echo "Reset: docker compose --project-name guardrail-prodtest --file docker-compose.production-test.yml down --volumes"
