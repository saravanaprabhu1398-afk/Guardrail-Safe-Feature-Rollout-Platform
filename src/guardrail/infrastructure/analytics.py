from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.infrastructure.snowflake import connect

EXPECTED_DYNAMIC_TABLES = {
    ("CORE", "DIM_USERS"),
    ("CORE", "FCT_ATTRIBUTED_OUTCOMES"),
    ("CORE", "FCT_EXPERIMENT_ASSIGNMENTS"),
    ("CORE", "FCT_EXPERIMENT_EXPOSURES"),
    ("CORE", "FCT_PRODUCT_OUTCOMES"),
    ("CORE", "FCT_USER_SESSIONS"),
    ("METRICS", "AGG_EXPERIMENT_RESULTS"),
    ("METRICS", "FCT_USER_EXPERIMENT_METRICS"),
}


@dataclass(frozen=True)
class Phase3Evidence:
    staged_events: int
    assignments: int
    exposures: int
    eligible_exposures: int
    product_outcomes: int
    attributed_outcomes: int
    user_experiment_metrics: int
    experiment_variant_metrics: int
    variant_flicker_users: int
    attribution_window_violations: int
    eligible_exposure_violations: int
    rate_bound_violations: int
    dynamic_tables: frozenset[tuple[str, str]]

    def validate(self) -> None:
        missing = EXPECTED_DYNAMIC_TABLES - self.dynamic_tables
        if missing:
            missing_names = ", ".join(f"{schema}.{name}" for schema, name in sorted(missing))
            raise RuntimeError(f"Missing Phase 3 Dynamic Tables: {missing_names}")

        required_nonempty = {
            "staged_events": self.staged_events,
            "assignments": self.assignments,
            "eligible_exposures": self.eligible_exposures,
            "product_outcomes": self.product_outcomes,
            "attributed_outcomes": self.attributed_outcomes,
            "user_experiment_metrics": self.user_experiment_metrics,
            "experiment_variant_metrics": self.experiment_variant_metrics,
        }
        empty = [name for name, count in required_nonempty.items() if count < 1]
        if empty:
            raise RuntimeError(
                "Phase 3 acceptance needs non-empty analytical data: " + ", ".join(empty)
            )

        violations = {
            "attribution_window": self.attribution_window_violations,
            "eligible_exposure": self.eligible_exposure_violations,
            "rate_bounds": self.rate_bound_violations,
        }
        failed = [f"{name}={count}" for name, count in violations.items() if count]
        if failed:
            raise RuntimeError("Phase 3 analytical invariant failure: " + ", ".join(failed))


EVIDENCE_QUERY = """
SELECT
  (SELECT COUNT(*) FROM GUARDRAIL.CLEAN.STG_PRODUCT_EVENTS),
  (SELECT COUNT(*) FROM GUARDRAIL.CORE.FCT_EXPERIMENT_ASSIGNMENTS),
  (SELECT COUNT(*) FROM GUARDRAIL.CORE.FCT_EXPERIMENT_EXPOSURES),
  (SELECT COUNT_IF(IS_ELIGIBLE_EXPOSURE)
     FROM GUARDRAIL.CORE.FCT_EXPERIMENT_EXPOSURES),
  (SELECT COUNT(*) FROM GUARDRAIL.CORE.FCT_PRODUCT_OUTCOMES),
  (SELECT COUNT(*) FROM GUARDRAIL.CORE.FCT_ATTRIBUTED_OUTCOMES),
  (SELECT COUNT(*) FROM GUARDRAIL.METRICS.FCT_USER_EXPERIMENT_METRICS),
  (SELECT COUNT(*) FROM GUARDRAIL.METRICS.AGG_EXPERIMENT_RESULTS),
  (SELECT COUNT_IF(HAS_VARIANT_FLICKER)
     FROM GUARDRAIL.METRICS.FCT_USER_EXPERIMENT_METRICS),
  (SELECT COUNT(*)
     FROM GUARDRAIL.CORE.FCT_ATTRIBUTED_OUTCOMES
    WHERE SECONDS_AFTER_EXPOSURE < 0 OR SECONDS_AFTER_EXPOSURE >= 24 * 60 * 60),
  (SELECT COUNT(*)
     FROM GUARDRAIL.CORE.FCT_EXPERIMENT_EXPOSURES
    WHERE IS_ELIGIBLE_EXPOSURE
      AND (ASSIGNMENT_EVENT_ID IS NULL
        OR ASSIGNMENT_TIMESTAMP > EXPOSURE_TIMESTAMP
        OR ASSIGNED_VARIANT_ID != VARIANT_ID)),
  (SELECT COUNT(*)
     FROM GUARDRAIL.METRICS.AGG_EXPERIMENT_RESULTS
    WHERE CONVERSION_RATE NOT BETWEEN 0 AND 1
       OR CANCELLATION_USER_RATE NOT BETWEEN 0 AND 1
       OR PAYMENT_FAILURE_USER_RATE NOT BETWEEN 0 AND 1)
"""


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def run_phase3_smoke() -> None:
    """Validate the live Phase 3 DAG and print concise portfolio evidence."""

    settings = SnowflakeSettings(  # type: ignore[call-arg]
        snowflake_warehouse="GUARDRAIL_TRANSFORM_WH",
        snowflake_schema="METRICS",
    )
    with connect(settings) as connection:
        count_row = connection.cursor().execute(EVIDENCE_QUERY).fetchone()
        if count_row is None:
            raise RuntimeError("Phase 3 evidence query returned no result.")

        dynamic_cursor = connection.cursor().execute("SHOW DYNAMIC TABLES IN DATABASE GUARDRAIL")
        dynamic_columns = {
            description[0]: index
            for index, description in enumerate(dynamic_cursor.description or ())
        }
        dynamic_rows = dynamic_cursor.fetchall()
        dynamic_tables = frozenset(
            (
                row[dynamic_columns["schema_name"]],
                row[dynamic_columns["name"]],
            )
            for row in dynamic_rows
        )

        metric_cursor = connection.cursor().execute(
            """
            SELECT EXPERIMENT_ID, VARIANT_ID, ALLOCATION_VERSION, EXPOSED_USERS,
                   CONVERSION_RATE, ABSOLUTE_CONVERSION_LIFT,
                   RELATIVE_CONVERSION_LIFT, CANCELLATION_USER_RATE,
                   PAYMENT_FAILURE_USER_RATE, COMPLETED_REVENUE
            FROM GUARDRAIL.METRICS.AGG_EXPERIMENT_RESULTS
            ORDER BY EXPERIMENT_ID, ALLOCATION_VERSION, VARIANT_ID
            """
        )
        metric_columns = [description[0].lower() for description in metric_cursor.description]
        metrics = [
            {column: _json_value(value) for column, value in zip(metric_columns, row, strict=True)}
            for row in metric_cursor.fetchall()
        ]

    evidence = Phase3Evidence(
        staged_events=count_row[0],
        assignments=count_row[1],
        exposures=count_row[2],
        eligible_exposures=count_row[3],
        product_outcomes=count_row[4],
        attributed_outcomes=count_row[5],
        user_experiment_metrics=count_row[6],
        experiment_variant_metrics=count_row[7],
        variant_flicker_users=count_row[8],
        attribution_window_violations=count_row[9],
        eligible_exposure_violations=count_row[10],
        rate_bound_violations=count_row[11],
        dynamic_tables=dynamic_tables,
    )
    evidence.validate()

    payload = asdict(evidence)
    payload["dynamic_tables"] = [
        f"{schema}.{name}" for schema, name in sorted(evidence.dynamic_tables)
    ]
    payload["variant_results"] = metrics
    payload["status"] = "passed"
    print(json.dumps(payload, indent=2))
