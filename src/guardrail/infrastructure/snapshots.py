from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.domain.analysis import (
    AnalysisSnapshotError,
    AnalysisSnapshotRecord,
    ExperimentAnalysisSnapshotPayload,
)
from guardrail.infrastructure.snowflake import connect

HEALTH_SNAPSHOT_MERGE = """
MERGE INTO CONTROL.EXPERIMENT_HEALTH_SNAPSHOTS AS target
USING (
    SELECT
        %s::STRING AS health_snapshot_id,
        %s::STRING AS experiment_id,
        %s::NUMBER AS allocation_version,
        %s::NUMBER AS expected_treatment_percentage,
        %s::STRING AS integrity_policy_version,
        PARSE_JSON(%s) AS integrity_policy,
        PARSE_JSON(%s) AS integrity_evidence,
        PARSE_JSON(%s) AS health_report,
        %s::STRING AS content_hash
) AS source
ON target.HEALTH_SNAPSHOT_ID = source.health_snapshot_id
WHEN NOT MATCHED THEN INSERT (
    HEALTH_SNAPSHOT_ID,
    EXPERIMENT_ID,
    ALLOCATION_VERSION,
    EXPECTED_TREATMENT_PERCENTAGE,
    INTEGRITY_POLICY_VERSION,
    INTEGRITY_POLICY,
    INTEGRITY_EVIDENCE,
    HEALTH_REPORT,
    CONTENT_HASH
) VALUES (
    source.health_snapshot_id,
    source.experiment_id,
    source.allocation_version,
    source.expected_treatment_percentage,
    source.integrity_policy_version,
    source.integrity_policy,
    source.integrity_evidence,
    source.health_report,
    source.content_hash
)
"""

ANALYSIS_SNAPSHOT_MERGE = """
MERGE INTO CONTROL.EXPERIMENT_ANALYSIS_SNAPSHOTS AS target
USING (
    SELECT
        %s::STRING AS analysis_snapshot_id,
        %s::STRING AS health_snapshot_id,
        %s::STRING AS experiment_id,
        %s::NUMBER AS allocation_version,
        %s::STRING AS analysis_policy_version,
        PARSE_JSON(%s) AS analysis_policy,
        PARSE_JSON(%s) AS analysis_evidence,
        PARSE_JSON(%s) AS analysis_report,
        %s::STRING AS content_hash
) AS source
ON target.ANALYSIS_SNAPSHOT_ID = source.analysis_snapshot_id
WHEN NOT MATCHED THEN INSERT (
    ANALYSIS_SNAPSHOT_ID,
    HEALTH_SNAPSHOT_ID,
    EXPERIMENT_ID,
    ALLOCATION_VERSION,
    ANALYSIS_POLICY_VERSION,
    ANALYSIS_POLICY,
    ANALYSIS_EVIDENCE,
    ANALYSIS_REPORT,
    CONTENT_HASH
) VALUES (
    source.analysis_snapshot_id,
    source.health_snapshot_id,
    source.experiment_id,
    source.allocation_version,
    source.analysis_policy_version,
    source.analysis_policy,
    source.analysis_evidence,
    source.analysis_report,
    source.content_hash
)
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


class SnowflakeAnalysisSnapshotRepository:
    """Persist immutable, content-addressed Phase 4 analysis snapshots."""

    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "CONTROL",
            }
        )

    def persist(self, payload: ExperimentAnalysisSnapshotPayload) -> AnalysisSnapshotRecord:
        serialized = payload.model_dump(mode="json")
        health_content = {
            "integrity_policy_version": serialized["integrity_policy_version"],
            "integrity_policy": serialized["integrity_policy"],
            "integrity_evidence": serialized["integrity_evidence"],
            "health_report": serialized["health_report"],
        }
        health_hash = _content_hash(health_content)
        health_snapshot_id = f"health_{health_hash}"
        analysis_content = {
            "health_snapshot_id": health_snapshot_id,
            "analysis_policy_version": serialized["analysis_policy_version"],
            "analysis_policy": serialized["analysis_policy"],
            "analysis_evidence": serialized["analysis_evidence"],
            "analysis_report": serialized["analysis_report"],
        }
        analysis_hash = _content_hash(analysis_content)
        analysis_snapshot_id = f"analysis_{analysis_hash}"

        try:
            with connect(self._settings) as connection:
                connection.autocommit(False)
                try:
                    cursor = connection.cursor()
                    cursor.execute(
                        HEALTH_SNAPSHOT_MERGE,
                        (
                            health_snapshot_id,
                            payload.integrity_evidence.experiment_id,
                            payload.integrity_evidence.allocation_version,
                            payload.health_report.expected_treatment_percentage,
                            payload.integrity_policy_version,
                            _canonical_json(serialized["integrity_policy"]),
                            _canonical_json(serialized["integrity_evidence"]),
                            _canonical_json(serialized["health_report"]),
                            health_hash,
                        ),
                    )
                    cursor.execute(
                        ANALYSIS_SNAPSHOT_MERGE,
                        (
                            analysis_snapshot_id,
                            health_snapshot_id,
                            payload.analysis_report.experiment_id,
                            payload.analysis_report.allocation_version,
                            payload.analysis_policy_version,
                            _canonical_json(serialized["analysis_policy"]),
                            _canonical_json(serialized["analysis_evidence"]),
                            _canonical_json(serialized["analysis_report"]),
                            analysis_hash,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

                row = (
                    connection.cursor()
                    .execute(
                        """
                        SELECT CREATED_AT
                        FROM CONTROL.EXPERIMENT_ANALYSIS_SNAPSHOTS
                        WHERE ANALYSIS_SNAPSHOT_ID = %s
                        """,
                        (analysis_snapshot_id,),
                    )
                    .fetchone()
                )
        except Exception as exc:
            raise AnalysisSnapshotError(
                "Experiment analysis snapshot could not be persisted."
            ) from exc

        if row is None:
            raise AnalysisSnapshotError("Experiment analysis snapshot insert returned no row.")
        created_at = row[0]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return AnalysisSnapshotRecord(
            health_snapshot_id=health_snapshot_id,
            analysis_snapshot_id=analysis_snapshot_id,
            created_at=created_at,
        )
