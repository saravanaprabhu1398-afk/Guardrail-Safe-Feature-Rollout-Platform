from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from typing import Any
from uuid import uuid4

from guardrail.config import SnowflakeSettings
from guardrail.domain.events import ProductEvent
from guardrail.domain.ingestion import EventPublishingError, IngestionReceipt
from guardrail.infrastructure.snowflake import connect

ClientFactory = Callable[..., Any]


class SnowpipeEventPublisher:
    """Publish immutable product events with offset-backed delivery semantics."""

    def __init__(
        self,
        settings: SnowflakeSettings,
        *,
        client_factory: ClientFactory | None = None,
        commit_timeout_seconds: int = 30,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._commit_timeout_seconds = commit_timeout_seconds
        self._client: Any | None = None
        self._channel: Any | None = None
        self._next_offset: int | None = None
        self._lock = Lock()

    def _ensure_channel(self) -> Any:
        if self._channel is not None:
            return self._channel

        properties = {
            "authorization_type": "PAT",
            "personal_access_token": self._settings.read_pat(),
            "account": self._settings.snowflake_account,
            "url": self._settings.snowflake_url,
            "role": self._settings.snowflake_role,
            "application": "guardrail-platform",
        }
        client_factory = self._client_factory
        if client_factory is None:
            os.environ.setdefault("SS_LOG_LEVEL", "warn")
            from snowflake.ingest.streaming import StreamingIngestClient

            client_factory = StreamingIngestClient

        self._client = client_factory(
            client_name="guardrail-api",
            db_name=self._settings.snowflake_database,
            schema_name=self._settings.snowflake_schema,
            pipe_name=self._settings.snowflake_streaming_pipe,
            properties=properties,
        )
        self._channel, _ = self._client.open_channel(self._settings.snowflake_streaming_channel)
        committed_offset = self._channel.get_latest_committed_offset_token()
        self._next_offset = int(committed_offset) + 1 if committed_offset else 0
        return self._channel

    @staticmethod
    def _payload(event: ProductEvent | dict[str, Any]) -> dict[str, Any]:
        if isinstance(event, ProductEvent):
            return event.model_dump(mode="json")
        return event

    @staticmethod
    def _string(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) else None

    @staticmethod
    def _integer(payload: dict[str, Any], key: str) -> int | None:
        value = payload.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _row(
        self,
        event: ProductEvent | dict[str, Any],
        *,
        batch_id: str,
        offset: int,
    ) -> dict[str, Any]:
        payload = self._payload(event)
        canonical_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return {
            "batch_id": batch_id,
            "channel_id": self._settings.snowflake_streaming_channel,
            "stream_offset": offset,
            "event_id": self._string(payload, "event_id"),
            "event_name": self._string(payload, "event_name"),
            "event_version": self._integer(payload, "event_version"),
            "event_timestamp": self._string(payload, "event_timestamp"),
            "received_timestamp": self._string(payload, "received_timestamp"),
            "source": self._string(payload, "source"),
            "payload": payload,
            "payload_hash": sha256(canonical_payload.encode("utf-8")).hexdigest(),
        }

    def publish(self, events: list[ProductEvent | dict[str, Any]]) -> IngestionReceipt:
        if not events:
            raise ValueError("At least one event is required for ingestion.")

        try:
            with self._lock:
                channel = self._ensure_channel()
                assert self._next_offset is not None

                batch_id = str(uuid4())
                first_offset = self._next_offset
                last_offset = first_offset + len(events) - 1
                rows = [
                    self._row(event, batch_id=batch_id, offset=first_offset + index)
                    for index, event in enumerate(events)
                ]

                channel.append_rows(
                    rows,
                    start_offset_token=str(first_offset),
                    end_offset_token=str(last_offset),
                )
                channel.wait_for_commit(
                    lambda token: token is not None and int(token) >= last_offset,
                    timeout_seconds=self._commit_timeout_seconds,
                )
                self._next_offset = last_offset + 1

                reconciliation = self._process_batch(
                    batch_id=batch_id,
                    first_offset=first_offset,
                    last_offset=last_offset,
                    records_submitted=len(events),
                )

            return IngestionReceipt(
                batch_id=batch_id,
                channel_id=self._settings.snowflake_streaming_channel,
                records_submitted=len(events),
                first_offset=first_offset,
                last_offset=last_offset,
                raw_count=reconciliation["raw_count"],
                validated_count=reconciliation["validated_count"],
                rejected_count=reconciliation["rejected_count"],
                duplicate_count=reconciliation["duplicate_count"],
                is_balanced=reconciliation["is_balanced"],
                committed_at=datetime.now(UTC),
            )
        except EventPublishingError:
            raise
        except Exception as exc:
            raise EventPublishingError("Snowflake did not complete the event batch.") from exc

    def _process_batch(
        self,
        *,
        batch_id: str,
        first_offset: int,
        last_offset: int,
        records_submitted: int,
    ) -> dict[str, Any]:
        with connect(self._settings) as connection:
            row = (
                connection.cursor()
                .execute(
                    "CALL CONTROL.PROCESS_INGESTION_BATCH(%s, %s, %s, %s, %s)",
                    (
                        batch_id,
                        self._settings.snowflake_streaming_channel,
                        first_offset,
                        last_offset,
                        records_submitted,
                    ),
                )
                .fetchone()
            )

        if row is None:
            raise RuntimeError("Snowflake batch processor returned no reconciliation result.")

        result = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if not isinstance(result, dict):
            raise RuntimeError("Snowflake batch processor returned an invalid result.")
        return result

    def close(self) -> None:
        with self._lock:
            if self._channel is not None:
                self._channel.close()
                self._channel = None
            if self._client is not None:
                self._client.close()
                self._client = None
            self._next_offset = None
