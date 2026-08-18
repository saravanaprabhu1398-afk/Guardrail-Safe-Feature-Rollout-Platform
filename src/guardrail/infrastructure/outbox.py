from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from guardrail.config import RuntimeSettings, SnowflakeSettings
from guardrail.domain.rollout import RolloutDecisionRecord, StateTransitionRecord
from guardrail.infrastructure.postgres import create_postgres_engine, outbox_events
from guardrail.infrastructure.rollout import SnowflakeDecisionRepository
from guardrail.infrastructure.telemetry import configure_json_logging

LOGGER = logging.getLogger("guardrail.outbox")


@dataclass(frozen=True)
class OutboxEnvelope:
    event_id: str
    event_type: str
    payload: dict[str, Any]
    attempt_count: int
    lock_token: str


@dataclass(frozen=True)
class OutboxRunResult:
    claimed: int
    processed: int
    failed: int
    dead_lettered: int


class OutboxPublisher(Protocol):
    def publish(self, envelope: OutboxEnvelope) -> None: ...


class PostgresOutboxWorker:
    """Lease-based, retrying dispatcher for the transactional outbox."""

    def __init__(
        self,
        engine: Engine,
        publisher: OutboxPublisher,
        settings: RuntimeSettings,
    ) -> None:
        self._engine = engine
        self._publisher = publisher
        self._batch_size = settings.outbox_batch_size
        self._lock_timeout = timedelta(seconds=settings.outbox_lock_timeout_seconds)
        self._max_attempts = settings.outbox_max_attempts
        self._retry_base_seconds = settings.outbox_retry_base_seconds

    def run_once(self) -> OutboxRunResult:
        envelopes = self._claim()
        processed = 0
        failed = 0
        dead_lettered = 0
        for envelope in envelopes:
            try:
                self._publisher.publish(envelope)
            except Exception as exc:
                failed += 1
                is_dead_lettered = self._mark_failed(envelope, exc)
                dead_lettered += int(is_dead_lettered)
                LOGGER.exception(
                    "outbox_publish_failed event_id=%s attempt=%s dead_lettered=%s",
                    envelope.event_id,
                    envelope.attempt_count,
                    is_dead_lettered,
                )
            else:
                self._mark_processed(envelope)
                processed += 1
        return OutboxRunResult(
            claimed=len(envelopes),
            processed=processed,
            failed=failed,
            dead_lettered=dead_lettered,
        )

    def _claim(self) -> tuple[OutboxEnvelope, ...]:
        now = datetime.now(UTC)
        stale_before = now - self._lock_timeout
        lock_token = uuid4().hex
        try:
            with self._engine.begin() as connection:
                rows = (
                    connection.execute(
                        select(outbox_events)
                        .where(
                            outbox_events.c.processed_at.is_(None),
                            outbox_events.c.dead_lettered_at.is_(None),
                            outbox_events.c.available_at <= now,
                            or_(
                                outbox_events.c.locked_at.is_(None),
                                outbox_events.c.locked_at < stale_before,
                            ),
                        )
                        .order_by(outbox_events.c.occurred_at, outbox_events.c.event_id)
                        .limit(self._batch_size)
                        .with_for_update(skip_locked=True)
                    )
                    .mappings()
                    .all()
                )
                envelopes: list[OutboxEnvelope] = []
                for row in rows:
                    attempt_count = row["attempt_count"] + 1
                    connection.execute(
                        update(outbox_events)
                        .where(outbox_events.c.event_id == row["event_id"])
                        .values(
                            locked_at=now,
                            lock_token=lock_token,
                            attempt_count=attempt_count,
                        )
                    )
                    envelopes.append(
                        OutboxEnvelope(
                            event_id=row["event_id"],
                            event_type=row["event_type"],
                            payload=row["payload"],
                            attempt_count=attempt_count,
                            lock_token=lock_token,
                        )
                    )
                return tuple(envelopes)
        except SQLAlchemyError as exc:
            raise RuntimeError("Transactional outbox could not claim pending events.") from exc

    def _mark_processed(self, envelope: OutboxEnvelope) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(outbox_events)
                .where(
                    outbox_events.c.event_id == envelope.event_id,
                    outbox_events.c.lock_token == envelope.lock_token,
                )
                .values(
                    processed_at=datetime.now(UTC),
                    locked_at=None,
                    lock_token=None,
                    last_error=None,
                )
            )

    def _mark_failed(self, envelope: OutboxEnvelope, error: Exception) -> bool:
        now = datetime.now(UTC)
        dead_lettered = envelope.attempt_count >= self._max_attempts
        exponential_delay = self._retry_base_seconds * (2 ** (envelope.attempt_count - 1))
        retry_at = now + timedelta(seconds=min(exponential_delay, 3_600))
        safe_error = f"{type(error).__name__}: rollout analytics replication failed"[:500]
        with self._engine.begin() as connection:
            connection.execute(
                update(outbox_events)
                .where(
                    outbox_events.c.event_id == envelope.event_id,
                    outbox_events.c.lock_token == envelope.lock_token,
                )
                .values(
                    available_at=retry_at,
                    locked_at=None,
                    lock_token=None,
                    dead_lettered_at=now if dead_lettered else None,
                    last_error=safe_error,
                )
            )
        return dead_lettered


class SnowflakeRolloutOutboxPublisher:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._repository = SnowflakeDecisionRepository(settings)

    def publish(self, envelope: OutboxEnvelope) -> None:
        if envelope.event_type != "rollout.outcome-recorded.v1":
            raise ValueError(f"Unsupported outbox event type: {envelope.event_type}")
        decision = RolloutDecisionRecord.model_validate(envelope.payload["decision"])
        transition = StateTransitionRecord.model_validate(envelope.payload["transition"])
        self._repository.persist_outcome(decision, transition)


def run_worker() -> None:
    configure_json_logging()
    runtime = RuntimeSettings()
    engine = create_postgres_engine(runtime)
    worker = PostgresOutboxWorker(
        engine,
        SnowflakeRolloutOutboxPublisher(SnowflakeSettings()),  # type: ignore[call-arg]
        runtime,
    )
    LOGGER.info("outbox_worker_started batch_size=%s", runtime.outbox_batch_size)
    try:
        while True:
            result = worker.run_once()
            if result.claimed:
                LOGGER.info(
                    "outbox_batch_complete claimed=%s processed=%s failed=%s dead_lettered=%s",
                    result.claimed,
                    result.processed,
                    result.failed,
                    result.dead_lettered,
                )
            time.sleep(runtime.outbox_poll_seconds)
    except KeyboardInterrupt:
        LOGGER.info("outbox_worker_stopped")
    finally:
        engine.dispose()
