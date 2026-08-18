from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.domain.metrics import (
    CompiledMetricArtifact,
    MetricArtifactNotFoundError,
    MetricArtifactPersistenceError,
    MetricDefinition,
    MetricDefinitionError,
    MetricDefinitionStatus,
    MetricVersionConflictError,
)
from guardrail.infrastructure.snowflake import connect
from guardrail.services.metrics import EventContractCatalog, MetricCompiler, MetricDefinitionLoader

PROJECT_ROOT = Path(__file__).resolve().parents[3]
METRIC_SCHEMA = PROJECT_ROOT / "contracts" / "metrics" / "metric-definition.v1.schema.json"
EVENT_CONTRACTS = PROJECT_ROOT / "contracts" / "events"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _variant(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise MetricArtifactPersistenceError("Stored metric definition is not a JSON object.")
    return parsed


class SnowflakeMetricArtifactRepository:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "CONTROL",
            }
        )

    def save(self, artifact: CompiledMetricArtifact) -> CompiledMetricArtifact:
        try:
            with connect(self._settings) as connection:
                existing = self._get(connection, artifact.metric_name, artifact.metric_version)
                if existing is not None:
                    if (
                        existing.definition_hash != artifact.definition_hash
                        or existing.compiled_sql_hash != artifact.compiled_sql_hash
                    ):
                        raise MetricVersionConflictError(
                            f"Metric '{artifact.metric_name}' version {artifact.metric_version} "
                            "already exists with different content."
                        )
                    return existing
                connection.cursor().execute(
                    """
                    INSERT INTO CONTROL.METRIC_DEFINITIONS (
                      ARTIFACT_ID, METRIC_NAME, METRIC_VERSION, DEFINITION_HASH,
                      DEFINITION, COMPILED_SQL, COMPILED_SQL_HASH, STATUS, COMPILED_AT
                    )
                    SELECT %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s, %s
                    """,
                    (
                        artifact.artifact_id,
                        artifact.metric_name,
                        artifact.metric_version,
                        artifact.definition_hash,
                        _canonical_json(artifact.definition.model_dump(mode="json")),
                        artifact.compiled_sql,
                        artifact.compiled_sql_hash,
                        artifact.status.value,
                        artifact.compiled_at,
                    ),
                )
                stored = self._get(connection, artifact.metric_name, artifact.metric_version)
        except MetricVersionConflictError:
            raise
        except Exception as exc:
            raise MetricArtifactPersistenceError("Metric artifact could not be persisted.") from exc
        if stored is None:
            raise MetricArtifactPersistenceError("Metric artifact insert returned no row.")
        return stored

    def activate(self, metric_name: str, metric_version: int) -> CompiledMetricArtifact:
        try:
            with connect(self._settings) as connection:
                existing = self._get(connection, metric_name, metric_version)
                if existing is None:
                    raise MetricArtifactNotFoundError((metric_name, metric_version))
                connection.autocommit(False)
                try:
                    connection.cursor().execute(
                        """
                        UPDATE CONTROL.METRIC_DEFINITIONS
                        SET STATUS = 'VALIDATED', UPDATED_AT = CURRENT_TIMESTAMP()
                        WHERE METRIC_NAME = %s AND STATUS = 'ACTIVE'
                        """,
                        (metric_name,),
                    )
                    connection.cursor().execute(
                        """
                        UPDATE CONTROL.METRIC_DEFINITIONS
                        SET STATUS = 'ACTIVE', UPDATED_AT = CURRENT_TIMESTAMP()
                        WHERE METRIC_NAME = %s AND METRIC_VERSION = %s
                        """,
                        (metric_name, metric_version),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                active = self._get(connection, metric_name, metric_version)
        except MetricArtifactNotFoundError:
            raise
        except Exception as exc:
            raise MetricArtifactPersistenceError("Metric artifact could not be activated.") from exc
        if active is None:
            raise MetricArtifactPersistenceError("Activated metric artifact was not found.")
        return active

    def get(self, metric_name: str, metric_version: int) -> CompiledMetricArtifact:
        try:
            with connect(self._settings) as connection:
                artifact = self._get(connection, metric_name, metric_version)
        except Exception as exc:
            raise MetricArtifactPersistenceError("Metric artifact could not be read.") from exc
        if artifact is None:
            raise MetricArtifactNotFoundError((metric_name, metric_version))
        return artifact

    def list_active(self) -> tuple[CompiledMetricArtifact, ...]:
        try:
            with connect(self._settings) as connection:
                rows = (
                    connection.cursor()
                    .execute(
                        """
                        SELECT ARTIFACT_ID, METRIC_NAME, METRIC_VERSION, DEFINITION_HASH,
                               DEFINITION, COMPILED_SQL, COMPILED_SQL_HASH, STATUS, COMPILED_AT
                        FROM CONTROL.METRIC_DEFINITIONS
                        WHERE STATUS = 'ACTIVE'
                        ORDER BY METRIC_NAME
                        """
                    )
                    .fetchall()
                )
            return tuple(self._artifact(row) for row in rows)
        except Exception as exc:
            raise MetricArtifactPersistenceError(
                "Active metric artifacts could not be read."
            ) from exc

    def _get(
        self, connection: Any, metric_name: str, metric_version: int
    ) -> CompiledMetricArtifact | None:
        row = (
            connection.cursor()
            .execute(
                """
                SELECT ARTIFACT_ID, METRIC_NAME, METRIC_VERSION, DEFINITION_HASH,
                       DEFINITION, COMPILED_SQL, COMPILED_SQL_HASH, STATUS, COMPILED_AT
                FROM CONTROL.METRIC_DEFINITIONS
                WHERE METRIC_NAME = %s AND METRIC_VERSION = %s
                """,
                (metric_name, metric_version),
            )
            .fetchone()
        )
        if row is None:
            return None
        return self._artifact(row)

    @staticmethod
    def _artifact(row: tuple[Any, ...]) -> CompiledMetricArtifact:
        compiled_at = row[8]
        if compiled_at.tzinfo is None:
            compiled_at = compiled_at.replace(tzinfo=UTC)
        return CompiledMetricArtifact(
            artifact_id=row[0],
            metric_name=row[1],
            metric_version=row[2],
            definition_hash=row[3],
            definition=MetricDefinition.model_validate(_variant(row[4])),
            compiled_sql=row[5],
            compiled_sql_hash=row[6],
            status=MetricDefinitionStatus(row[7]),
            compiled_at=compiled_at,
        )


def run_metric_compile() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and compile Guardrail metric YAML into deterministic Snowflake SQL."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--activate", action="store_true")
    arguments = parser.parse_args()
    if arguments.activate and not arguments.persist:
        parser.error("--activate requires --persist")

    loader = MetricDefinitionLoader(
        METRIC_SCHEMA,
        EventContractCatalog.from_contracts(EVENT_CONTRACTS),
    )
    compiler = MetricCompiler()
    repository = (
        SnowflakeMetricArtifactRepository(SnowflakeSettings())  # type: ignore[call-arg]
        if arguments.persist
        else None
    )
    artifacts: list[CompiledMetricArtifact] = []
    try:
        for path in arguments.paths:
            artifact = compiler.compile(loader.load(path))
            if repository is not None:
                artifact = repository.save(artifact)
                if arguments.activate:
                    artifact = repository.activate(
                        artifact.metric_name,
                        artifact.metric_version,
                    )
            artifacts.append(artifact)
    except (MetricDefinitionError, MetricArtifactPersistenceError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    print(json.dumps([artifact.model_dump(mode="json") for artifact in artifacts], indent=2))
