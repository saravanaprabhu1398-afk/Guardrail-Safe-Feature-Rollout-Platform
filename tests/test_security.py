from __future__ import annotations

from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from guardrail.config import RuntimeSettings
from guardrail.domain.auth import (
    ApplicationRole,
    AuthenticationError,
    Principal,
)
from guardrail.main import create_app
from guardrail.services.security import OidcAuthenticator
from guardrail.services.store import InMemoryExperimentStore

EXPERIMENT = {
    "experiment_id": "ranking_security_test",
    "name": "Security test experiment",
    "hypothesis": "The secured flow accepts only explicitly authorized callers.",
    "primary_metric": "order_conversion_rate",
}


class StaticAuthenticator:
    def __init__(self, principals: dict[str, Principal]) -> None:
        self._principals = principals

    def authenticate(self, token: str) -> Principal:
        try:
            return self._principals[token]
        except KeyError as exc:
            raise AuthenticationError("invalid token") from exc


class UnavailableStore(InMemoryExperimentStore):
    def ping(self) -> None:
        raise ConnectionError("database host and password must stay private")


def production_settings(**updates: Any) -> RuntimeSettings:
    values: dict[str, Any] = {
        "guardrail_env": "production",
        "guardrail_store_backend": "postgres",
        "database_url": (
            "postgresql+psycopg://guardrail:secret@postgres/guardrail?sslmode=require"
        ),
        "auth_enabled": True,
        "oidc_issuer": "https://identity.example.com",
        "oidc_audience": "guardrail-api",
        "oidc_jwks_url": "https://identity.example.com/.well-known/jwks.json",
        "cors_origins": "https://guardrail.example.com",
        "trusted_hosts": "testserver,guardrail.example.com",
        "_env_file": None,
    }
    values.update(updates)
    return RuntimeSettings(**values)


def principal(subject: str, *roles: ApplicationRole) -> Principal:
    return Principal(subject=subject, roles=frozenset(roles))


def test_protected_routes_enforce_authentication_and_role_boundaries() -> None:
    authenticator = StaticAuthenticator(
        {
            "viewer": principal("viewer", ApplicationRole.VIEWER),
            "operator": principal("operator", ApplicationRole.EXPERIMENT_OPERATOR),
        }
    )
    app = create_app(
        store=InMemoryExperimentStore(),
        runtime_settings=production_settings(),
        token_authenticator=authenticator,
    )

    with TestClient(app) as client:
        anonymous = client.post("/experiments", json=EXPERIMENT)
        forbidden = client.post(
            "/experiments",
            json=EXPERIMENT,
            headers={"Authorization": "Bearer viewer"},
        )
        created = client.post(
            "/experiments",
            json=EXPERIMENT,
            headers={"Authorization": "Bearer operator"},
        )
        visible = client.get(
            "/experiments/ranking_security_test",
            headers={"Authorization": "Bearer viewer"},
        )

    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert forbidden.status_code == 403
    assert created.status_code == 201
    assert visible.status_code == 200


def test_approver_cannot_impersonate_a_different_approval_subject() -> None:
    authenticator = StaticAuthenticator(
        {"approver": principal("release-owner", ApplicationRole.ROLLOUT_APPROVER)}
    )
    app = create_app(
        store=InMemoryExperimentStore(),
        runtime_settings=production_settings(),
        token_authenticator=authenticator,
    )

    with TestClient(app) as client:
        response = client.post(
            "/experiments/missing/transitions",
            json={"requested_by": "automation", "approved_by": "someone-else"},
            headers={"Authorization": "Bearer approver"},
        )

    assert response.status_code == 403
    assert response.json()["detail"].startswith("approved_by")


def test_public_probes_and_production_security_headers() -> None:
    app = create_app(
        store=InMemoryExperimentStore(),
        runtime_settings=production_settings(),
        token_authenticator=StaticAuthenticator({}),
    )

    with TestClient(app) as client:
        response = client.get("/health/ready", headers={"X-Request-ID": "request-123"})
        docs = client.get("/docs")

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-123"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )
    assert docs.status_code == 404


def test_readiness_masks_database_failure_and_metrics_use_stable_route_labels() -> None:
    with TestClient(create_app(store=UnavailableStore())) as client:
        unavailable = client.get("/health/ready")
        metrics = client.get("/metrics")

    assert unavailable.status_code == 503
    assert "password" not in unavailable.text
    assert metrics.status_code == 200
    assert 'route="/health/ready"' in metrics.text
    assert "guardrail_http_request_duration_seconds" in metrics.text


def test_request_body_limit_is_enforced_before_route_processing() -> None:
    settings = RuntimeSettings(max_request_bytes=1024, _env_file=None)
    with TestClient(create_app(runtime_settings=settings)) as client:
        response = client.post(
            "/experiments",
            content=b"x" * 1025,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413


def test_oidc_authenticator_verifies_contract_and_maps_only_known_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def decode(token: str, key: object, **kwargs: Any) -> dict[str, Any]:
        captured.update({"token": token, "key": key, **kwargs})
        return {
            "sub": "release-owner",
            "email": "owner@example.com",
            "roles": ["guardrail_rollout_approver", "unrelated_role"],
        }

    monkeypatch.setattr(jwt, "decode", decode)
    authenticator = OidcAuthenticator(
        production_settings(),
        signing_key_resolver=lambda _: "public-key",
    )

    authenticated = authenticator.authenticate("signed-token")

    assert authenticated.subject == "release-owner"
    assert authenticated.roles == frozenset({ApplicationRole.ROLLOUT_APPROVER})
    assert captured["audience"] == "guardrail-api"
    assert captured["issuer"] == "https://identity.example.com"
    assert captured["algorithms"] == ["RS256", "ES256"]
    assert captured["options"] == {"require": ["exp", "iat", "sub"]}
