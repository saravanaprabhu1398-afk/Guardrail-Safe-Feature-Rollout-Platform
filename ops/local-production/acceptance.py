from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import requests
from confluent_kafka import OFFSET_BEGINNING, Consumer, TopicPartition

BASE_URL = os.getenv("ACCEPTANCE_BASE_URL", "https://gateway-prodtest:8443")
DIRECT_API_URL = os.getenv("ACCEPTANCE_DIRECT_API_URL", "http://guardrail-api-prodtest:8000")
OIDC_URL = os.getenv("ACCEPTANCE_OIDC_URL", "https://oidc-prodtest:9443")
CA_FILE = os.getenv("ACCEPTANCE_CA_FILE", "/certs/ca.crt")
EVIDENCE_FILE = Path(os.getenv("ACCEPTANCE_EVIDENCE_FILE", "/evidence/state.json"))
DATABASE_URL = os.environ["ACCEPTANCE_DATABASE_URL"]
INSECURE_DATABASE_DSN = os.environ["ACCEPTANCE_INSECURE_DATABASE_DSN"]
REDPANDA_TOPIC = os.environ["REDPANDA_TOPIC"]

SESSION = requests.Session()
SESSION.verify = CA_FILE


def _expect(response: requests.Response, status: int) -> requests.Response:
    if response.status_code != status:
        safe_body = response.text[:500]
        raise AssertionError(
            f"{response.request.method} {response.request.url} returned "
            f"{response.status_code}, expected {status}: {safe_body}"
        )
    return response


def _wait_for_status(path: str, status: int, *, timeout: float = 60) -> requests.Response:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = SESSION.get(f"{BASE_URL}{path}", timeout=5)
            if response.status_code == status:
                return response
            last_error = AssertionError(f"received {response.status_code}")
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(1)
    raise AssertionError(f"{path} did not reach HTTP {status}: {last_error}")


def _token(subject: str, *roles: str, **overrides: Any) -> str:
    payload: dict[str, Any] = {"subject": subject, "roles": list(roles)}
    payload.update(overrides)
    response = _expect(
        SESSION.post(f"{OIDC_URL}/token", json=payload, timeout=5),
        200,
    )
    return str(response.json()["access_token"])


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _event(experiment_id: str, variant: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    suffix = uuid4().hex
    return {
        "event_id": f"prodtest_{suffix}",
        "event_name": "experiment_exposed",
        "event_version": 1,
        "user_id": f"prodtest-user-{suffix}",
        "anonymous_id": f"anonymous-{suffix}",
        "session_id": f"session-{suffix}",
        "experiment_id": experiment_id,
        "variant_id": variant,
        "event_timestamp": now,
        "sent_timestamp": now,
        "received_timestamp": now,
        "source": "local-production-acceptance",
        "attributes": {"acceptance": True},
    }


def _publish_event(experiment_id: str, variant: str, token: str) -> dict[str, Any]:
    event = _event(experiment_id, variant)
    response = _expect(
        SESSION.post(
            f"{BASE_URL}/events",
            json={"events": [event]},
            headers=_headers(token),
            timeout=25,
        ),
        202,
    )
    receipt = response.json()
    assert receipt["transport"] == "redpanda"
    assert receipt["records_submitted"] == 1
    assert receipt["topic"] == REDPANDA_TOPIC
    assert len(receipt["offsets"]) == 1
    return event


def _consume_event(event_id: str) -> None:
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["REDPANDA_BOOTSTRAP_SERVERS"],
            "group.id": f"guardrail-prodtest-{uuid4().hex}",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "security.protocol": "SASL_SSL",
            "sasl.mechanism": "SCRAM-SHA-256",
            "sasl.username": os.environ["REDPANDA_ADMIN_USERNAME"],
            "sasl.password": os.environ["REDPANDA_ADMIN_PASSWORD"],
            "ssl.ca.location": CA_FILE,
        }
    )
    try:
        metadata = consumer.list_topics(REDPANDA_TOPIC, timeout=10)
        partitions = metadata.topics[REDPANDA_TOPIC].partitions
        consumer.assign(
            [
                TopicPartition(REDPANDA_TOPIC, partition, OFFSET_BEGINNING)
                for partition in partitions
            ]
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = consumer.poll(1)
            if message is None:
                continue
            if message.error() is not None:
                raise AssertionError(f"Redpanda consumer error: {message.error()}")
            envelope = json.loads(message.value())
            if envelope.get("payload", {}).get("event_id") == event_id:
                return
        raise AssertionError(f"Event {event_id} was not found in acknowledged Redpanda records")
    finally:
        consumer.close()


def _database_evidence(experiment_id: str, subject_id: str) -> dict[str, int]:
    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM experiments WHERE experiment_id = %s",
            (experiment_id,),
        )
        experiments = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM assignments
            WHERE experiment_id = %s AND subject_id = %s AND allocation_version = 1
            """,
            (experiment_id, subject_id),
        )
        assignments = int(cursor.fetchone()[0])
    return {"experiments": experiments, "assignments": assignments}


def _assert_database_rejects_plaintext() -> None:
    try:
        connection = psycopg.connect(INSECURE_DATABASE_DSN, connect_timeout=3)
    except psycopg.OperationalError:
        return
    connection.close()
    raise AssertionError("PostgreSQL unexpectedly accepted a plaintext connection")


def setup() -> None:
    ready = _wait_for_status("/health/ready", 200)
    assert ready.json() == {"status": "ready", "store": "postgres"}

    security = _expect(
        SESSION.get(
            f"{BASE_URL}/health/live",
            headers={"X-Request-ID": "local-production-request"},
            timeout=5,
        ),
        200,
    )
    assert security.headers["X-Request-ID"] == "local-production-request"
    assert security.headers["Strict-Transport-Security"].startswith("max-age=")
    assert security.headers["Content-Security-Policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )
    assert security.headers["X-Content-Type-Options"] == "nosniff"
    assert security.headers["X-Frame-Options"] == "DENY"
    _expect(SESSION.get(f"{BASE_URL}/docs", timeout=5), 404)
    _expect(SESSION.get(f"{BASE_URL}/me", timeout=5), 401)
    _expect(
        SESSION.get(f"{BASE_URL}/me", headers=_headers("not-a-jwt"), timeout=5),
        401,
    )

    wrong_audience = _token("wrong-audience", "guardrail_viewer", audience="wrong-api")
    expired = _token("expired-viewer", "guardrail_viewer", expires_in=-60)
    _expect(SESSION.get(f"{BASE_URL}/me", headers=_headers(wrong_audience), timeout=5), 401)
    _expect(SESSION.get(f"{BASE_URL}/me", headers=_headers(expired), timeout=5), 401)

    viewer = _token("local-viewer", "guardrail_viewer", "ignored_external_role")
    operator = _token("local-operator", "guardrail_experiment_operator")
    approver = _token("local-approver", "guardrail_rollout_approver")
    assignment_client = _token("local-assignment", "guardrail_assignment_client")
    telemetry_producer = _token("local-telemetry", "guardrail_telemetry_producer")

    principal = _expect(
        SESSION.get(f"{BASE_URL}/me", headers=_headers(viewer), timeout=5),
        200,
    ).json()
    assert principal["subject"] == "local-viewer"
    assert principal["roles"] == ["guardrail_viewer"]

    experiment_id = f"prodlocal_{uuid4().hex[:16]}"
    experiment = {
        "experiment_id": experiment_id,
        "name": "Local production acceptance",
        "hypothesis": "Production-mode state remains secure and durable across failures.",
        "primary_metric": "order_conversion_rate",
    }
    _expect(SESSION.post(f"{BASE_URL}/experiments", json=experiment, timeout=5), 401)
    _expect(
        SESSION.post(
            f"{BASE_URL}/experiments",
            json=experiment,
            headers=_headers(viewer),
            timeout=5,
        ),
        403,
    )
    created = _expect(
        SESSION.post(
            f"{BASE_URL}/experiments",
            json=experiment,
            headers=_headers(operator),
            timeout=5,
        ),
        201,
    ).json()
    assert created["state"] == "DRAFT"
    launched = _expect(
        SESSION.post(
            f"{BASE_URL}/experiments/{experiment_id}/launch",
            headers=_headers(operator),
            timeout=5,
        ),
        200,
    ).json()
    assert launched["state"] == "RUNNING_5"

    _expect(
        SESSION.post(
            f"{BASE_URL}/experiments/{experiment_id}/transitions",
            json={"requested_by": "local-test", "approved_by": "impersonated-user"},
            headers=_headers(approver),
            timeout=5,
        ),
        403,
    )

    subject_id = "local-production-subject"
    assignment_url = f"{BASE_URL}/assignments/{experiment_id}/{subject_id}"

    def assign_once(_: int) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        response = requests.get(
            assignment_url,
            headers=_headers(assignment_client),
            verify=CA_FILE,
            timeout=10,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return _expect(response, 200).json(), elapsed_ms

    with ThreadPoolExecutor(max_workers=16) as executor:
        concurrent = tuple(executor.map(assign_once, range(48)))
    assignments = [item[0] for item in concurrent]
    assert all(assignment == assignments[0] for assignment in assignments)

    load_latencies: list[float] = []
    with ThreadPoolExecutor(max_workers=24) as executor:
        for assignment, latency in executor.map(assign_once, range(300)):
            assert assignment == assignments[0]
            load_latencies.append(latency)
    p95_ms = statistics.quantiles(load_latencies, n=100, method="inclusive")[94]
    assert p95_ms < 1_000, f"Local assignment p95 was {p95_ms:.1f} ms"

    oversized = SESSION.post(
        f"{BASE_URL}/experiments",
        data=b"x" * 2049,
        headers=_headers(operator, **{"Content-Type": "application/json"}),
        timeout=5,
    )
    _expect(oversized, 413)

    preflight = _expect(
        SESSION.options(
            f"{BASE_URL}/experiments",
            headers={
                "Origin": "https://gateway-prodtest:8443",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
            timeout=5,
        ),
        200,
    )
    assert preflight.headers["Access-Control-Allow-Origin"] == "https://gateway-prodtest:8443"

    untrusted_host = requests.get(
        f"{DIRECT_API_URL}/health/live",
        headers={"Host": "evil.invalid"},
        timeout=5,
    )
    _expect(untrusted_host, 400)

    _assert_database_rejects_plaintext()
    counts = _database_evidence(experiment_id, subject_id)
    assert counts == {"experiments": 1, "assignments": 1}

    event = _publish_event(experiment_id, assignments[0]["variant"], telemetry_producer)
    _consume_event(event["event_id"])

    metrics = _expect(SESSION.get(f"{BASE_URL}/metrics", timeout=5), 200).text
    assert 'route="/assignments/{experiment_id}/{subject_id}"' in metrics
    assert 'route="/experiments"' in metrics
    assert "guardrail_http_request_duration_seconds" in metrics

    evidence = {
        "experiment": launched,
        "assignment": assignments[0],
        "event_id": event["event_id"],
        "assignment_p95_ms": round(p95_ms, 2),
    }
    EVIDENCE_FILE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "setup", "status": "passed", **evidence}, separators=(",", ":")))


def verify() -> None:
    _wait_for_status("/health/ready", 200)
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding="utf-8"))
    experiment_id = evidence["experiment"]["experiment_id"]
    subject_id = evidence["assignment"]["subject_id"]
    viewer = _token("local-viewer", "guardrail_viewer")
    assignment_client = _token("local-assignment", "guardrail_assignment_client")

    experiment = _expect(
        SESSION.get(
            f"{BASE_URL}/experiments/{experiment_id}",
            headers=_headers(viewer),
            timeout=5,
        ),
        200,
    ).json()
    assignment = _expect(
        SESSION.get(
            f"{BASE_URL}/assignments/{experiment_id}/{subject_id}",
            headers=_headers(assignment_client),
            timeout=5,
        ),
        200,
    ).json()
    assert experiment == evidence["experiment"]
    assert assignment == evidence["assignment"]
    assert _database_evidence(experiment_id, subject_id) == {
        "experiments": 1,
        "assignments": 1,
    }
    print(json.dumps({"phase": "verify", "status": "passed"}))


def degraded() -> None:
    _wait_for_status("/health/live", 200)
    unavailable = _wait_for_status("/health/ready", 503)
    assert unavailable.json() == {"detail": "Operational state store is unavailable."}
    assert "password" not in unavailable.text.lower()
    print(json.dumps({"phase": "database-degraded", "status": "passed"}))


def recovered() -> None:
    _wait_for_status("/health/ready", 200, timeout=90)
    print(json.dumps({"phase": "recovered", "status": "passed"}))


def redpanda_down() -> None:
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding="utf-8"))
    telemetry = _token("local-telemetry", "guardrail_telemetry_producer")
    response = SESSION.post(
        f"{BASE_URL}/events",
        json={
            "events": [
                _event(
                    evidence["experiment"]["experiment_id"],
                    evidence["assignment"]["variant"],
                )
            ]
        },
        headers=_headers(telemetry),
        timeout=30,
    )
    _expect(response, 503)
    assert response.json() == {"detail": "Event transport is temporarily unavailable."}
    print(json.dumps({"phase": "redpanda-degraded", "status": "passed"}))


def redpanda_recovered() -> None:
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding="utf-8"))
    telemetry = _token("local-telemetry", "guardrail_telemetry_producer")
    event = _publish_event(
        evidence["experiment"]["experiment_id"],
        evidence["assignment"]["variant"],
        telemetry,
    )
    _consume_event(event["event_id"])
    print(json.dumps({"phase": "redpanda-recovered", "status": "passed"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=("setup", "verify", "degraded", "recovered", "redpanda-down", "redpanda-recovered"),
    )
    phase = parser.parse_args().phase
    {
        "setup": setup,
        "verify": verify,
        "degraded": degraded,
        "recovered": recovered,
        "redpanda-down": redpanda_down,
        "redpanda-recovered": redpanda_recovered,
    }[phase]()


if __name__ == "__main__":
    main()
