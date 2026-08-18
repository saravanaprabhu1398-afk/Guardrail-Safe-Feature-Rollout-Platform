from __future__ import annotations

import argparse
import json
import sys

from guardrail.config import SnowflakeSettings
from guardrail.domain.integrity import (
    ExperimentIntegrityEvidence,
    IntegrityEvidenceError,
)
from guardrail.infrastructure.snowflake import connect
from guardrail.services.integrity import IntegrityService

INTEGRITY_EVIDENCE_QUERY = """
WITH query_scope AS (
    SELECT %s::VARCHAR AS experiment_id, %s::NUMBER AS allocation_version
),
assignment_events AS (
    SELECT assignments.*
    FROM GUARDRAIL.CORE.FCT_EXPERIMENT_ASSIGNMENTS AS assignments
    INNER JOIN query_scope
        ON assignments.experiment_id = query_scope.experiment_id
        AND assignments.allocation_version = query_scope.allocation_version
),
first_assignments AS (
    SELECT user_id, variant_id
    FROM assignment_events
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY user_id
        ORDER BY assignment_timestamp, assignment_event_id
    ) = 1
),
assignment_variant_flicker AS (
    SELECT user_id
    FROM assignment_events
    GROUP BY user_id
    HAVING COUNT(DISTINCT variant_id) > 1
),
version_bounds AS (
    SELECT
        MIN(IFF(
            assignments.allocation_version = query_scope.allocation_version,
            assignments.assignment_timestamp,
            NULL
        )) AS version_start,
        MIN(IFF(
            assignments.allocation_version > query_scope.allocation_version,
            assignments.assignment_timestamp,
            NULL
        )) AS next_version_start
    FROM GUARDRAIL.CORE.FCT_EXPERIMENT_ASSIGNMENTS AS assignments
    CROSS JOIN query_scope
    WHERE assignments.experiment_id = query_scope.experiment_id
),
exposures_in_scope AS (
    SELECT exposures.*
    FROM GUARDRAIL.CORE.FCT_EXPERIMENT_EXPOSURES AS exposures
    CROSS JOIN query_scope
    CROSS JOIN version_bounds
    WHERE exposures.experiment_id = query_scope.experiment_id
      AND (
          exposures.allocation_version = query_scope.allocation_version
          OR (
              exposures.assignment_event_id IS NULL
              AND exposures.exposure_timestamp >= version_bounds.version_start
              AND (
                  version_bounds.next_version_start IS NULL
                  OR exposures.exposure_timestamp < version_bounds.next_version_start
              )
          )
      )
),
eligible_exposed_users AS (
    SELECT metrics.user_id, metrics.variant_id, metrics.has_variant_flicker
    FROM GUARDRAIL.METRICS.FCT_USER_EXPERIMENT_METRICS AS metrics
    INNER JOIN query_scope
        ON metrics.experiment_id = query_scope.experiment_id
        AND metrics.allocation_version = query_scope.allocation_version
)
SELECT
    (SELECT COALESCE(COUNT_IF(variant_id = 'control'), 0) FROM first_assignments),
    (SELECT COALESCE(COUNT_IF(variant_id = 'treatment'), 0) FROM first_assignments),
    (SELECT COALESCE(COUNT_IF(variant_id = 'control'), 0) FROM eligible_exposed_users),
    (SELECT COALESCE(COUNT_IF(variant_id = 'treatment'), 0) FROM eligible_exposed_users),
    (SELECT COUNT(*) FROM exposures_in_scope),
    (SELECT COALESCE(COUNT_IF(has_variant_flicker), 0) FROM eligible_exposed_users),
    (SELECT COUNT(*) FROM assignment_variant_flicker),
    (SELECT COALESCE(COUNT_IF(assignment_event_id IS NULL), 0) FROM exposures_in_scope),
    (SELECT COALESCE(COUNT_IF(
        assignment_event_id IS NOT NULL AND NOT is_eligible_exposure
    ), 0) FROM exposures_in_scope),
    (SELECT COUNT(*)
       FROM first_assignments
      WHERE NOT EXISTS (
          SELECT 1
          FROM eligible_exposed_users
          WHERE eligible_exposed_users.user_id = first_assignments.user_id
      ))
"""


class SnowflakeIntegrityRepository:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "CONTROL",
            }
        )

    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentIntegrityEvidence:
        try:
            with connect(self._settings) as connection:
                row = (
                    connection.cursor()
                    .execute(
                        INTEGRITY_EVIDENCE_QUERY,
                        (experiment_id, allocation_version),
                    )
                    .fetchone()
                )
        except Exception as exc:
            raise IntegrityEvidenceError(
                "Experiment integrity evidence is temporarily unavailable."
            ) from exc

        if row is None:
            raise IntegrityEvidenceError("Experiment integrity evidence query returned no row.")

        try:
            return ExperimentIntegrityEvidence(
                experiment_id=experiment_id,
                allocation_version=allocation_version,
                control_assignments=row[0],
                treatment_assignments=row[1],
                control_exposed_users=row[2],
                treatment_exposed_users=row[3],
                total_exposures=row[4],
                variant_flicker_users=row[5],
                assignment_variant_flicker_users=row[6],
                orphan_exposures=row[7],
                variant_mismatch_exposures=row[8],
                assignments_without_exposure=row[9],
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise IntegrityEvidenceError(
                "Experiment integrity evidence returned an invalid row."
            ) from exc


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _treatment_percentage(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 99:
        raise argparse.ArgumentTypeError("value must be between 1 and 99")
    return parsed


def run_phase4_integrity_check() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate blocking Phase 4 integrity checks from live Snowflake facts."
    )
    parser.add_argument("experiment_id")
    parser.add_argument("--allocation-version", type=_positive_integer, default=1)
    parser.add_argument("--treatment-percentage", type=_treatment_percentage, default=5)
    arguments = parser.parse_args()

    settings = SnowflakeSettings()  # type: ignore[call-arg]
    try:
        report = IntegrityService(SnowflakeIntegrityRepository(settings)).get_health(
            arguments.experiment_id,
            arguments.allocation_version,
            arguments.treatment_percentage,
        )
    except IntegrityEvidenceError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    if report.analysis_blocked:
        raise SystemExit(2)
