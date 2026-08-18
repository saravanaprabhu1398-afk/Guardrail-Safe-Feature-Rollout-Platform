from __future__ import annotations

import json
from datetime import UTC
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.domain.ingestion import IngestionReceipt
from guardrail.domain.replay import (
    RejectedEvent,
    RejectionNotFoundError,
    ReplayConflictError,
    ReplayRecord,
    ReplayRepository,
    ReplayStatus,
)
from guardrail.infrastructure.snowflake import connect


def _variant(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, dict) else None


class SnowflakeReplayRepository(ReplayRepository):
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings

    def get_rejection(self, rejection_id: str) -> RejectedEvent:
        with connect(self._settings) as connection:
            row = (
                connection.cursor()
                .execute(
                    """
                    SELECT REJECTION_ID, INGESTION_ID, BATCH_ID, EVENT_ID, PAYLOAD,
                           REASON_CODE, REASON_DETAILS, REJECTED_AT, REPLAY_STATUS
                    FROM RAW.REJECTED_EVENTS
                    WHERE REJECTION_ID = %s
                    """,
                    (rejection_id,),
                )
                .fetchone()
            )

        if row is None:
            raise RejectionNotFoundError(rejection_id)
        payload = _variant(row[4])
        if payload is None:
            raise RuntimeError("Rejected event payload is not a JSON object.")
        return RejectedEvent(
            rejection_id=row[0],
            ingestion_id=row[1],
            batch_id=row[2],
            event_id=row[3],
            payload=payload,
            reason_code=row[5],
            reason_details=_variant(row[6]),
            rejected_at=row[7],
            replay_status=row[8],
        )

    def event_contract_exists(self, event_name: str, event_version: int) -> bool:
        with connect(self._settings) as connection:
            row = (
                connection.cursor()
                .execute(
                    """
                    SELECT COUNT(*)
                    FROM CONFIG.EVENT_CONTRACTS
                    WHERE EVENT_NAME = %s
                      AND EVENT_VERSION = %s
                      AND CONTRACT_STATUS = 'ACTIVE'
                    """,
                    (event_name, event_version),
                )
                .fetchone()
            )
        return row is not None and row[0] == 1

    def enqueue(
        self,
        *,
        replay_id: str,
        rejection: RejectedEvent,
        corrected_payload: dict[str, Any],
        corrected_payload_hash: str,
        requested_by: str,
    ) -> ReplayRecord:
        with connect(self._settings) as connection:
            existing = self._find_for_rejection(connection, rejection.rejection_id)
            if existing is not None:
                if existing.corrected_payload_hash != corrected_payload_hash:
                    raise ReplayConflictError(
                        "This rejection already has a different replay correction."
                    )
                return existing

            connection.autocommit(False)
            try:
                connection.cursor().execute(
                    """
                    INSERT INTO RAW.REPLAY_QUEUE (
                      REPLAY_ID, REJECTION_ID, CORRECTED_PAYLOAD,
                      CORRECTED_PAYLOAD_HASH, STATUS, ATTEMPT_COUNT,
                      REQUESTED_BY, REQUESTED_AT
                    )
                    SELECT %s, %s, PARSE_JSON(%s), %s, 'PENDING', 0, %s,
                           CURRENT_TIMESTAMP()
                    """,
                    (
                        replay_id,
                        rejection.rejection_id,
                        json.dumps(corrected_payload, separators=(",", ":")),
                        corrected_payload_hash,
                        requested_by,
                    ),
                )
                connection.cursor().execute(
                    """
                    UPDATE RAW.REJECTED_EVENTS
                    SET REPLAY_STATUS = 'QUEUED'
                    WHERE REJECTION_ID = %s
                    """,
                    (rejection.rejection_id,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

            record = self._find_for_rejection(connection, rejection.rejection_id)

        if record is None:
            raise RuntimeError("Replay queue insert returned no record.")
        return record

    def mark_processing(self, replay_id: str) -> ReplayRecord:
        with connect(self._settings) as connection:
            connection.cursor().execute(
                """
                UPDATE RAW.REPLAY_QUEUE
                SET STATUS = 'PROCESSING',
                    ATTEMPT_COUNT = ATTEMPT_COUNT + 1,
                    LAST_ATTEMPT_AT = CURRENT_TIMESTAMP(),
                    ERROR_MESSAGE = NULL
                WHERE REPLAY_ID = %s
                  AND STATUS <> 'COMPLETED'
                """,
                (replay_id,),
            )
            record = self._get_record(connection, replay_id)
        return record

    def mark_completed(self, replay_id: str, batch_id: str) -> ReplayRecord:
        with connect(self._settings) as connection:
            record = self._get_record(connection, replay_id)
            connection.autocommit(False)
            try:
                connection.cursor().execute(
                    """
                    UPDATE RAW.REPLAY_QUEUE
                    SET STATUS = 'COMPLETED',
                        REPLAY_BATCH_ID = %s,
                        COMPLETED_AT = CURRENT_TIMESTAMP(),
                        ERROR_MESSAGE = NULL
                    WHERE REPLAY_ID = %s
                    """,
                    (batch_id, replay_id),
                )
                connection.cursor().execute(
                    """
                    UPDATE RAW.REJECTED_EVENTS
                    SET REPLAY_STATUS = 'REPLAYED'
                    WHERE REJECTION_ID = %s
                    """,
                    (record.rejection_id,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            completed = self._get_record(connection, replay_id)
        return completed

    def mark_failed(self, replay_id: str, error_message: str) -> ReplayRecord:
        with connect(self._settings) as connection:
            record = self._get_record(connection, replay_id)
            connection.autocommit(False)
            try:
                connection.cursor().execute(
                    """
                    UPDATE RAW.REPLAY_QUEUE
                    SET STATUS = 'FAILED',
                        ERROR_MESSAGE = %s
                    WHERE REPLAY_ID = %s
                    """,
                    (error_message[:1_000], replay_id),
                )
                connection.cursor().execute(
                    """
                    UPDATE RAW.REJECTED_EVENTS
                    SET REPLAY_STATUS = 'FAILED'
                    WHERE REJECTION_ID = %s
                    """,
                    (record.rejection_id,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            failed = self._get_record(connection, replay_id)
        return failed

    def get_ingestion_receipt(self, batch_id: str) -> IngestionReceipt:
        with connect(self._settings) as connection:
            row = (
                connection.cursor()
                .execute(
                    """
                    SELECT audit.BATCH_ID, audit.CHANNEL_ID,
                           audit.RECORDS_SUBMITTED, audit.FIRST_OFFSET,
                           audit.LAST_OFFSET, reconciliation.RAW_COUNT,
                           reconciliation.VALIDATED_COUNT,
                           reconciliation.REJECTED_COUNT,
                           reconciliation.DUPLICATE_COUNT,
                           reconciliation.IS_BALANCED,
                           audit.COMPLETED_AT
                    FROM RAW.INGESTION_AUDIT AS audit
                    JOIN OBSERVABILITY.RECONCILIATION_RESULTS AS reconciliation
                      ON audit.BATCH_ID = reconciliation.BATCH_ID
                    WHERE audit.BATCH_ID = %s
                      AND audit.STATUS = 'COMMITTED'
                    """,
                    (batch_id,),
                )
                .fetchone()
            )

        if row is None:
            raise RuntimeError("Completed replay ingestion evidence was not found.")
        return IngestionReceipt(
            batch_id=row[0],
            channel_id=row[1],
            records_submitted=row[2],
            first_offset=row[3],
            last_offset=row[4],
            raw_count=row[5],
            validated_count=row[6],
            rejected_count=row[7],
            duplicate_count=row[8],
            is_balanced=row[9],
            committed_at=row[10].replace(tzinfo=UTC) if row[10].tzinfo is None else row[10],
        )

    def _find_for_rejection(self, connection: Any, rejection_id: str) -> ReplayRecord | None:
        row = (
            connection.cursor()
            .execute(
                """
                SELECT REPLAY_ID, REJECTION_ID, CORRECTED_PAYLOAD_HASH,
                       REPLAY_BATCH_ID, STATUS, ATTEMPT_COUNT, REQUESTED_BY,
                       REQUESTED_AT, LAST_ATTEMPT_AT, COMPLETED_AT, ERROR_MESSAGE
                FROM RAW.REPLAY_QUEUE
                WHERE REJECTION_ID = %s
                ORDER BY REQUESTED_AT DESC
                LIMIT 1
                """,
                (rejection_id,),
            )
            .fetchone()
        )
        return self._record(row) if row is not None else None

    def _get_record(self, connection: Any, replay_id: str) -> ReplayRecord:
        row = (
            connection.cursor()
            .execute(
                """
                SELECT REPLAY_ID, REJECTION_ID, CORRECTED_PAYLOAD_HASH,
                       REPLAY_BATCH_ID, STATUS, ATTEMPT_COUNT, REQUESTED_BY,
                       REQUESTED_AT, LAST_ATTEMPT_AT, COMPLETED_AT, ERROR_MESSAGE
                FROM RAW.REPLAY_QUEUE
                WHERE REPLAY_ID = %s
                """,
                (replay_id,),
            )
            .fetchone()
        )
        if row is None:
            raise RuntimeError("Replay queue record was not found.")
        return self._record(row)

    @staticmethod
    def _record(row: tuple[Any, ...]) -> ReplayRecord:
        return ReplayRecord(
            replay_id=row[0],
            rejection_id=row[1],
            corrected_payload_hash=row[2],
            replay_batch_id=row[3],
            status=ReplayStatus(row[4]),
            attempt_count=row[5],
            requested_by=row[6],
            requested_at=row[7],
            last_attempt_at=row[8],
            completed_at=row[9],
            error_message=row[10],
        )
