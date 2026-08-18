from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SnowflakeSettings(BaseSettings):
    """Non-secret Snowflake settings plus a path to the local PAT file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    snowflake_account: str = Field(min_length=3)
    snowflake_user: str = Field(min_length=1)
    snowflake_role: str = "GUARDRAIL_ENGINEER"
    snowflake_warehouse: str = "GUARDRAIL_INGEST_WH"
    snowflake_transform_warehouse: str = "GUARDRAIL_TRANSFORM_WH"
    snowflake_database: str = "GUARDRAIL"
    snowflake_schema: str = "RAW"
    snowflake_auth_method: Literal["PAT"] = "PAT"
    snowflake_pat_file: Path = Path(".secrets/snowflake_pat")
    snowflake_streaming_pipe: str = "PRODUCT_EVENTS_STREAMING"
    snowflake_streaming_channel: str = "guardrail-local-dev"

    @property
    def snowflake_url(self) -> str:
        return f"https://{self.snowflake_account}.snowflakecomputing.com"

    def read_pat(self) -> str:
        path = self.snowflake_pat_file.expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Snowflake PAT file does not exist: {path}")

        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"Snowflake PAT file is empty: {path}")
        return token


class TransportSettings(BaseSettings):
    """Local event-transport configuration without broker credentials."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    guardrail_env: Literal["local", "test", "production"] = "local"
    guardrail_event_transport: Literal["snowpipe", "redpanda"] = "snowpipe"
    redpanda_bootstrap_servers: str = "localhost:19092"
    redpanda_topic: str = "guardrail.product-events.v1"
    redpanda_client_id: str = "guardrail-api"
    redpanda_consumer_group: str = "guardrail-snowpipe-sink-v1"
    redpanda_worker_batch_size: int = Field(default=500, ge=1, le=5_000)
    redpanda_poll_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    redpanda_security_protocol: Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"] = (
        "PLAINTEXT"
    )
    redpanda_sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = "SCRAM-SHA-256"
    redpanda_sasl_username: str | None = None
    redpanda_sasl_password: SecretStr | None = None
    redpanda_ssl_ca_location: Path | None = None

    @property
    def redpanda_sasl_password_value(self) -> str | None:
        return (
            self.redpanda_sasl_password.get_secret_value()
            if self.redpanda_sasl_password is not None
            else None
        )

    @model_validator(mode="after")
    def validate_transport_security(self) -> TransportSettings:
        uses_sasl = self.redpanda_security_protocol.startswith("SASL_")
        if uses_sasl and (
            self.redpanda_sasl_username is None or self.redpanda_sasl_password is None
        ):
            raise ValueError("SASL transport requires a Redpanda username and password")
        if self.guardrail_env == "production" and self.redpanda_security_protocol not in {
            "SSL",
            "SASL_SSL",
        }:
            raise ValueError("Production Redpanda transport requires TLS")
        return self


class RuntimeSettings(BaseSettings):
    """Operational control-plane settings with production fail-fast validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    guardrail_env: Literal["local", "test", "production"] = "local"
    guardrail_store_backend: Literal["memory", "postgres"] = "memory"
    database_url: SecretStr | None = None
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    outbox_batch_size: int = Field(default=100, ge=1, le=1_000)
    outbox_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    outbox_lock_timeout_seconds: int = Field(default=300, ge=30, le=3_600)
    outbox_max_attempts: int = Field(default=12, ge=1, le=100)
    outbox_retry_base_seconds: int = Field(default=5, ge=1, le=300)
    auth_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_roles_claim: str = "roles"
    cors_origins: str = "http://localhost:8501"
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    max_request_bytes: int = Field(default=5_242_880, ge=1_024, le=104_857_600)

    @property
    def database_url_value(self) -> str | None:
        return self.database_url.get_secret_value() if self.database_url is not None else None

    @property
    def cors_origin_values(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.cors_origins.split(",") if value.strip())

    @property
    def trusted_host_values(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.trusted_hosts.split(",") if value.strip())

    @model_validator(mode="after")
    def validate_production_contract(self) -> RuntimeSettings:
        if self.guardrail_store_backend == "postgres" and self.database_url is None:
            raise ValueError("DATABASE_URL is required for the postgres store backend")
        if self.guardrail_env == "production":
            if self.guardrail_store_backend != "postgres":
                raise ValueError("Production requires GUARDRAIL_STORE_BACKEND=postgres")
            database_url = self.database_url_value or ""
            database_query = parse_qs(urlparse(database_url).query)
            ssl_mode = database_query.get("sslmode", [""])[0]
            if ssl_mode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("Production PostgreSQL connections must require TLS")
            if not self.auth_enabled:
                raise ValueError("Production requires AUTH_ENABLED=true")
            required_oidc = (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
            if any(value is None for value in required_oidc):
                raise ValueError("Production requires OIDC issuer, audience, and JWKS URL")
            if "*" in self.cors_origin_values or "*" in self.trusted_host_values:
                raise ValueError("Production CORS origins and trusted hosts cannot use wildcards")
        return self
