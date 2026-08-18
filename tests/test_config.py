from pathlib import Path

import pytest
from pydantic import ValidationError

from guardrail.config import RuntimeSettings, SnowflakeSettings, TransportSettings
from guardrail.infrastructure.snowflake import connection_parameters


def settings(pat_file: Path) -> SnowflakeSettings:
    return SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="USER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )


def test_pat_is_read_from_file_and_trimmed(tmp_path: Path) -> None:
    pat_file = tmp_path / "pat"
    pat_file.write_text("secret-token\n", encoding="utf-8")

    assert settings(pat_file).read_pat() == "secret-token"


def test_missing_pat_file_fails_without_exposing_a_secret(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError, match="PAT file does not exist"):
        settings(missing).read_pat()


def test_connection_context_can_be_deferred_until_bootstrap(tmp_path: Path) -> None:
    pat_file = tmp_path / "pat"
    pat_file.write_text("secret-token", encoding="utf-8")

    parameters = connection_parameters(settings(pat_file), include_data_context=False)

    assert parameters["role"] == "GUARDRAIL_ENGINEER"
    assert "warehouse" not in parameters
    assert "database" not in parameters
    assert "schema" not in parameters


def test_production_runtime_requires_postgres_and_oidc() -> None:
    with pytest.raises(ValidationError, match="postgres"):
        RuntimeSettings(guardrail_env="production", _env_file=None)

    configured = RuntimeSettings(
        guardrail_env="production",
        guardrail_store_backend="postgres",
        database_url=("postgresql+psycopg://guardrail:secret@postgres/guardrail?sslmode=require"),
        auth_enabled=True,
        oidc_issuer="https://identity.example.com/",
        oidc_audience="guardrail-api",
        oidc_jwks_url="https://identity.example.com/.well-known/jwks.json",
        cors_origins="https://guardrail.example.com",
        trusted_hosts="guardrail.example.com",
        _env_file=None,
    )

    assert configured.database_url_value is not None
    assert "secret" not in repr(configured)


def test_production_transport_requires_tls_and_sasl_credentials_are_secret() -> None:
    with pytest.raises(ValidationError, match="requires TLS"):
        TransportSettings(guardrail_env="production", _env_file=None)

    settings = TransportSettings(
        guardrail_env="production",
        guardrail_event_transport="redpanda",
        redpanda_security_protocol="SASL_SSL",
        redpanda_sasl_username="guardrail-service",
        redpanda_sasl_password="broker-secret",
        _env_file=None,
    )

    assert settings.redpanda_sasl_password_value == "broker-secret"
    assert "broker-secret" not in repr(settings)
