from __future__ import annotations

import json
from datetime import UTC, datetime

from guardrail.config import SnowflakeSettings
from guardrail.domain.events import EventName, ProductEvent
from guardrail.domain.experiments import Variant
from guardrail.domain.replay import ReplayRequest
from guardrail.infrastructure.replay import SnowflakeReplayRepository
from guardrail.infrastructure.snowflake import connect
from guardrail.infrastructure.streaming import SnowpipeEventPublisher
from guardrail.services.replay import ReplayService


def run_phase2_smoke() -> None:
    timestamp = datetime.now(UTC)
    valid_event = ProductEvent(
        event_id="phase2_smoke_event_v1",
        event_name=EventName.EXPERIMENT_EXPOSED,
        user_id="phase2_smoke_user",
        anonymous_id="phase2_smoke_anonymous",
        session_id="phase2_smoke_session",
        experiment_id="new_ranking_algorithm",
        variant_id=Variant.TREATMENT,
        event_timestamp=timestamp,
        sent_timestamp=timestamp,
        received_timestamp=timestamp,
        source="phase2_smoke",
        attributes={"feature_version": "ranking-v1", "surface": "restaurant_feed"},
    )
    invalid_event = {
        "event_name": "unsupported_smoke_event",
        "event_version": 1,
        "source": "phase2_smoke",
        "attributes": {},
    }

    publisher = SnowpipeEventPublisher(
        SnowflakeSettings()  # type: ignore[call-arg]
    )
    try:
        receipt = publisher.publish([valid_event, valid_event, invalid_event])
    finally:
        publisher.close()

    print(receipt.model_dump_json(indent=2))


def run_replay_smoke() -> None:
    settings = SnowflakeSettings()  # type: ignore[call-arg]
    with connect(settings) as connection:
        row = (
            connection.cursor()
            .execute(
                """
                SELECT REJECTION_ID
                FROM RAW.REJECTED_EVENTS
                WHERE REPLAY_STATUS = 'NOT_REQUESTED'
                ORDER BY REJECTED_AT DESC
                LIMIT 1
                """
            )
            .fetchone()
        )
    if row is None:
        raise RuntimeError(
            "No unreplayed rejection is available. Run guardrail-phase2-smoke first."
        )

    rejection_id = row[0]
    timestamp = datetime.now(UTC)
    corrected_event = ProductEvent(
        event_id=f"replayed_{rejection_id.replace('-', '')}",
        event_name=EventName.EXPERIMENT_EXPOSED,
        user_id="phase2_replay_user",
        anonymous_id="phase2_replay_anonymous",
        session_id="phase2_replay_session",
        experiment_id="new_ranking_algorithm",
        variant_id=Variant.TREATMENT,
        event_timestamp=timestamp,
        sent_timestamp=timestamp,
        received_timestamp=timestamp,
        source="phase2_replay_smoke",
        attributes={"feature_version": "ranking-v1", "surface": "restaurant_feed"},
    )
    request = ReplayRequest(
        corrected_event=corrected_event.model_dump(mode="json"),
        requested_by="phase2_smoke",
    )
    publisher = SnowpipeEventPublisher(settings)
    service = ReplayService(SnowflakeReplayRepository(settings), publisher)
    try:
        first = service.replay(rejection_id, request)
        second = service.replay(rejection_id, request)
    finally:
        publisher.close()

    if first.replay_id != second.replay_id:
        raise RuntimeError("Replay idempotency failed: replay IDs differ.")
    if first.ingestion.batch_id != second.ingestion.batch_id:
        raise RuntimeError("Replay idempotency failed: ingestion batches differ.")

    with connect(settings) as connection:
        evidence = (
            connection.cursor()
            .execute(
                """
                SELECT queue.STATUS, queue.ATTEMPT_COUNT,
                       COUNT(raw_event.INGESTION_ID) AS RAW_REPLAY_ROWS,
                       MAX(raw_event.PAYLOAD:_guardrail_replay:rejection_id::STRING)
                         AS LINEAGE_REJECTION_ID
                FROM RAW.REPLAY_QUEUE AS queue
                JOIN RAW.PRODUCT_EVENTS AS raw_event
                  ON queue.REPLAY_BATCH_ID = raw_event.BATCH_ID
                WHERE queue.REPLAY_ID = %s
                GROUP BY queue.STATUS, queue.ATTEMPT_COUNT
                """,
                (first.replay_id,),
            )
            .fetchone()
        )
    if evidence is None:
        raise RuntimeError("Replay evidence was not found in Snowflake.")

    print(
        json.dumps(
            {
                "replay_id": first.replay_id,
                "rejection_id": first.rejection_id,
                "batch_id": first.ingestion.batch_id,
                "status": evidence[0],
                "attempt_count_after_two_identical_requests": evidence[1],
                "raw_replay_rows": evidence[2],
                "lineage_rejection_id": evidence[3],
                "is_balanced": first.ingestion.is_balanced,
                "idempotent": first.ingestion.batch_id == second.ingestion.batch_id,
            },
            indent=2,
        )
    )
