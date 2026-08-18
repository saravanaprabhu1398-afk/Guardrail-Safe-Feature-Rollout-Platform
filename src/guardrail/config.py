from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
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

    guardrail_event_transport: Literal["snowpipe", "redpanda"] = "snowpipe"
    redpanda_bootstrap_servers: str = "localhost:19092"
    redpanda_topic: str = "guardrail.product-events.v1"
    redpanda_client_id: str = "guardrail-api"
    redpanda_consumer_group: str = "guardrail-snowpipe-sink-v1"
    redpanda_worker_batch_size: int = Field(default=500, ge=1, le=5_000)
    redpanda_poll_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
