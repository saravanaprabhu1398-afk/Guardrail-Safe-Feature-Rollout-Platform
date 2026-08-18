#!/bin/sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 ROLE [SUBJECT]" >&2
    exit 2
fi

role="$1"
subject="${2:-manual-${role}}"

case "${role}" in
    guardrail_viewer | \
        guardrail_experiment_operator | \
        guardrail_rollout_approver | \
        guardrail_assignment_client | \
        guardrail_telemetry_producer)
        ;;
    *)
        echo "Unknown Guardrail role: ${role}" >&2
        exit 2
        ;;
esac

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

docker compose \
    --project-name guardrail-prodtest \
    --project-directory "${repository_root}" \
    --file "${repository_root}/docker-compose.production-test.yml" \
    exec -T oidc-prodtest \
    python -c '
import sys

import requests

response = requests.post(
    "https://oidc-prodtest:9443/token",
    json={"subject": sys.argv[1], "roles": [sys.argv[2]]},
    verify="/certs/ca.crt",
    timeout=5,
)
response.raise_for_status()
print(response.json()["access_token"])
' "${subject}" "${role}"
