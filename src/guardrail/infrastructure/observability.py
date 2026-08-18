from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.domain.observability import (
    CostPerformanceError,
    CostPerformanceReport,
    QueryPerformanceSlice,
)
from guardrail.infrastructure.snowflake import connect

COST_CAVEAT = (
    "Active compute credits are an X-Small-equivalent estimate from summed query execution "
    "time. They exclude warehouse idle time, concurrency, minimum billing, and services other "
    "than the query-level cloud-services credits reported by Snowflake."
)


def _query(lookback_hours: int, history_limit: int) -> str:
    return f"""
WITH history AS (
  SELECT *
  FROM TABLE(GUARDRAIL.INFORMATION_SCHEMA.QUERY_HISTORY(
    END_TIME_RANGE_START => DATEADD('hour', -{lookback_hours}, CURRENT_TIMESTAMP()),
    RESULT_LIMIT => {history_limit}
  ))
), summarized AS (
  SELECT
    WAREHOUSE_NAME,
    MAX(WAREHOUSE_SIZE) AS WAREHOUSE_SIZE,
    QUERY_TYPE,
    COUNT_IF(EXECUTION_STATUS = 'SUCCESS') AS SUCCESSFUL_QUERIES,
    COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILED_QUERIES,
    SUM(COALESCE(EXECUTION_TIME, 0)) / 1000.0 AS TOTAL_EXECUTION_SECONDS,
    PERCENTILE_CONT(0.95) WITHIN GROUP (
      ORDER BY COALESCE(TOTAL_ELAPSED_TIME, 0)
    ) AS P95_TOTAL_ELAPSED_MS,
    AVG(COALESCE(QUEUED_OVERLOAD_TIME, 0)) AS AVERAGE_QUEUED_OVERLOAD_MS,
    SUM(COALESCE(BYTES_SCANNED, 0)) AS BYTES_SCANNED,
    SUM(COALESCE(ROWS_PRODUCED, 0)) AS ROWS_PRODUCED,
    SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CLOUD_SERVICES_CREDITS,
    SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000.0
      * CASE MAX(WAREHOUSE_SIZE)
          WHEN 'X-Small' THEN 1
          WHEN 'Small' THEN 2
          WHEN 'Medium' THEN 4
          WHEN 'Large' THEN 8
          WHEN 'X-Large' THEN 16
          WHEN '2X-Large' THEN 32
          WHEN '3X-Large' THEN 64
          WHEN '4X-Large' THEN 128
          ELSE 1
        END AS ESTIMATED_ACTIVE_COMPUTE_CREDITS
  FROM history
  WHERE WAREHOUSE_NAME IN (%s, %s)
  GROUP BY WAREHOUSE_NAME, QUERY_TYPE
)
SELECT
  WAREHOUSE_NAME,
  WAREHOUSE_SIZE,
  QUERY_TYPE,
  SUCCESSFUL_QUERIES,
  FAILED_QUERIES,
  ROUND(TOTAL_EXECUTION_SECONDS, 3),
  ROUND(P95_TOTAL_ELAPSED_MS, 3),
  ROUND(AVERAGE_QUEUED_OVERLOAD_MS, 3),
  BYTES_SCANNED,
  ROWS_PRODUCED,
  ROUND(CLOUD_SERVICES_CREDITS, 6),
  ROUND(ESTIMATED_ACTIVE_COMPUTE_CREDITS, 6)
FROM summarized
ORDER BY ESTIMATED_ACTIVE_COMPUTE_CREDITS DESC, WAREHOUSE_NAME, QUERY_TYPE
"""


class SnowflakeCostPerformanceRepository:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._source_warehouses = (
            settings.snowflake_warehouse,
            settings.snowflake_transform_warehouse,
        )
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "OBSERVABILITY",
            }
        )

    def get_report(
        self,
        *,
        lookback_hours: int = 24,
        history_limit: int = 1_000,
    ) -> CostPerformanceReport:
        if not 1 <= lookback_hours <= 167:
            raise ValueError("Lookback hours must be between 1 and 167.")
        if not 1 <= history_limit <= 10_000:
            raise ValueError("History limit must be between 1 and 10,000.")
        try:
            with connect(self._settings) as connection:
                rows = (
                    connection.cursor()
                    .execute(
                        _query(lookback_hours, history_limit),
                        self._source_warehouses,
                    )
                    .fetchall()
                )
            slices = tuple(self._slice(row) for row in rows)
        except Exception as exc:
            raise CostPerformanceError(
                "Snowflake query cost and performance evidence is unavailable."
            ) from exc
        return CostPerformanceReport(
            generated_at=datetime.now(UTC),
            lookback_hours=lookback_hours,
            history_limit=history_limit,
            slices=slices,
            cost_caveat=COST_CAVEAT,
        )

    @staticmethod
    def _slice(row: tuple[Any, ...]) -> QueryPerformanceSlice:
        return QueryPerformanceSlice(
            warehouse_name=row[0],
            warehouse_size=row[1],
            query_type=row[2],
            successful_queries=row[3],
            failed_queries=row[4],
            total_execution_seconds=row[5],
            p95_total_elapsed_ms=row[6],
            average_queued_overload_ms=row[7],
            bytes_scanned=row[8],
            rows_produced=row[9],
            cloud_services_credits=row[10],
            estimated_active_compute_credits=row[11],
        )


def run_cost_report() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize Guardrail Snowflake query cost and performance evidence."
    )
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--history-limit", type=int, default=1_000)
    arguments = parser.parse_args()
    try:
        report = SnowflakeCostPerformanceRepository(
            SnowflakeSettings()  # type: ignore[call-arg]
        ).get_report(
            lookback_hours=arguments.lookback_hours,
            history_limit=arguments.history_limit,
        )
    except (CostPerformanceError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(report.model_dump(mode="json"), indent=2))
