from __future__ import annotations

import argparse
import json
import sys

from guardrail.config import SnowflakeSettings
from guardrail.domain.analysis import (
    AnalysisEvidenceError,
    AnalysisSnapshotError,
    ExperimentAnalysisEvidence,
)
from guardrail.domain.integrity import IntegrityEvidenceError
from guardrail.infrastructure.integrity import SnowflakeIntegrityRepository
from guardrail.infrastructure.snapshots import SnowflakeAnalysisSnapshotRepository
from guardrail.infrastructure.snowflake import connect
from guardrail.services.analysis import ExperimentAnalysisService
from guardrail.services.integrity import IntegrityService

ANALYSIS_EVIDENCE_QUERY = """
WITH query_scope AS (
    SELECT %s::VARCHAR AS experiment_id, %s::NUMBER AS allocation_version
),
user_metrics AS (
    SELECT metrics.*
    FROM GUARDRAIL.METRICS.FCT_USER_EXPERIMENT_METRICS AS metrics
    INNER JOIN query_scope
        ON metrics.experiment_id = query_scope.experiment_id
        AND metrics.allocation_version = query_scope.allocation_version
)
SELECT
    COALESCE(COUNT_IF(variant_id = 'control'), 0),
    COALESCE(COUNT_IF(variant_id = 'control' AND did_convert), 0),
    COALESCE(COUNT_IF(variant_id = 'treatment'), 0),
    COALESCE(COUNT_IF(variant_id = 'treatment' AND did_convert), 0),
    COALESCE(COUNT_IF(
        variant_id = 'control' AND average_delivery_minutes IS NOT NULL
    ), 0),
    AVG(IFF(variant_id = 'control', average_delivery_minutes, NULL)),
    VAR_SAMP(IFF(variant_id = 'control', average_delivery_minutes, NULL)),
    COALESCE(COUNT_IF(
        variant_id = 'treatment' AND average_delivery_minutes IS NOT NULL
    ), 0),
    AVG(IFF(variant_id = 'treatment', average_delivery_minutes, NULL)),
    VAR_SAMP(IFF(variant_id = 'treatment', average_delivery_minutes, NULL))
FROM user_metrics
"""


class SnowflakeAnalysisRepository:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "METRICS",
            }
        )

    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentAnalysisEvidence:
        try:
            with connect(self._settings) as connection:
                row = (
                    connection.cursor()
                    .execute(
                        ANALYSIS_EVIDENCE_QUERY,
                        (experiment_id, allocation_version),
                    )
                    .fetchone()
                )
        except Exception as exc:
            raise AnalysisEvidenceError(
                "Experiment analysis evidence is temporarily unavailable."
            ) from exc

        if row is None:
            raise AnalysisEvidenceError("Experiment analysis evidence query returned no row.")

        try:
            return ExperimentAnalysisEvidence(
                experiment_id=experiment_id,
                allocation_version=allocation_version,
                control_exposed_users=row[0],
                control_converted_users=row[1],
                treatment_exposed_users=row[2],
                treatment_converted_users=row[3],
                control_delivery_observations=row[4],
                control_delivery_mean=row[5],
                control_delivery_variance=row[6],
                treatment_delivery_observations=row[7],
                treatment_delivery_mean=row[8],
                treatment_delivery_variance=row[9],
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise AnalysisEvidenceError(
                "Experiment analysis evidence returned an invalid row."
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


def run_phase4_analysis() -> None:
    parser = argparse.ArgumentParser(
        description="Run integrity-gated Phase 4 statistical analysis from Snowflake facts."
    )
    parser.add_argument("experiment_id")
    parser.add_argument("--allocation-version", type=_positive_integer, default=1)
    parser.add_argument("--treatment-percentage", type=_treatment_percentage, default=5)
    arguments = parser.parse_args()

    settings = SnowflakeSettings()  # type: ignore[call-arg]
    service = ExperimentAnalysisService(
        IntegrityService(SnowflakeIntegrityRepository(settings)),
        SnowflakeAnalysisRepository(settings),
        snapshot_repository=SnowflakeAnalysisSnapshotRepository(settings),
    )
    try:
        report = service.get_results(
            arguments.experiment_id,
            arguments.allocation_version,
            arguments.treatment_percentage,
        )
    except (IntegrityEvidenceError, AnalysisEvidenceError, AnalysisSnapshotError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    print(json.dumps(report.model_dump(mode="json"), indent=2))
    if report.analysis_blocked:
        raise SystemExit(2)
