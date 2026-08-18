from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from guardrail.domain.metrics import (
    CompiledMetricArtifact,
    MetricDefinition,
    MetricDefinitionError,
    MetricDefinitionStatus,
    MetricReferenceError,
    MetricType,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class EventContractCatalog:
    def __init__(
        self,
        *,
        events: dict[str, frozenset[str]],
        entities: frozenset[str],
    ) -> None:
        self._events = events
        self._entities = entities

    @classmethod
    def from_contracts(cls, event_contract_root: Path) -> EventContractCatalog:
        common_path = event_contract_root / "common" / "product-event-envelope.v1.schema.json"
        common = json.loads(common_path.read_text(encoding="utf-8"))
        entities = frozenset(common["properties"])
        events: dict[str, frozenset[str]] = {}
        for path in sorted((event_contract_root / "v1").glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            event_name = schema["x-guardrail-event-name"]
            attributes = schema["allOf"][1]["properties"]["attributes"]["properties"]
            events[event_name] = frozenset(f"attributes.{name}" for name in attributes)
        return cls(events=events, entities=entities)

    def validate(self, definition: MetricDefinition) -> None:
        if definition.entity not in self._entities:
            raise MetricReferenceError(
                f"Metric entity '{definition.entity}' is not registered in the event envelope."
            )
        references = [definition.denominator]
        if definition.numerator is not None:
            references.append(definition.numerator)
        if definition.value is not None:
            references.append(definition.value)
        for reference in references:
            if reference.event not in self._events:
                raise MetricReferenceError(
                    f"Metric event '{reference.event}' is not registered in version 1 contracts."
                )
        if definition.denominator.event != "experiment_exposed":
            raise MetricReferenceError(
                "Experiment metrics must use experiment_exposed as their denominator event."
            )
        if definition.value is not None and (
            definition.value.field not in self._events[definition.value.event]
        ):
            raise MetricReferenceError(
                f"Metric field '{definition.value.field}' is not registered for "
                f"event '{definition.value.event}'."
            )


class MetricDefinitionLoader:
    def __init__(self, schema_path: Path, catalog: EventContractCatalog) -> None:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(schema)
        self._catalog = catalog

    def load(self, path: Path) -> MetricDefinition:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise MetricDefinitionError(f"Metric definition '{path}' could not be read.") from exc
        if not isinstance(raw, dict):
            raise MetricDefinitionError("Metric definition must be a YAML object.")
        errors = sorted(self._validator.iter_errors(raw), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.path) or "root"
            raise MetricDefinitionError(
                f"Metric schema validation failed at {location}: {first.message}"
            )
        try:
            definition = MetricDefinition.model_validate(raw)
        except ValueError as exc:
            raise MetricDefinitionError("Metric semantic validation failed.") from exc
        self._catalog.validate(definition)
        return definition


class MetricCompiler:
    _VALUE_COLUMNS = {
        "attributes.delivery_minutes": "delivery_minutes",
        "attributes.order_amount": "order_amount",
    }

    def compile(self, definition: MetricDefinition) -> CompiledMetricArtifact:
        definition_json = _canonical_json(definition.model_dump(mode="json"))
        definition_hash = _sha256(definition_json)
        sql = self._compile_sql(definition, definition_hash)
        sql_hash = _sha256(sql)
        return CompiledMetricArtifact(
            artifact_id=f"{definition.name}_v{definition.version}_{sql_hash[:16]}",
            metric_name=definition.name,
            metric_version=definition.version,
            definition_hash=definition_hash,
            compiled_sql=sql,
            compiled_sql_hash=sql_hash,
            status=MetricDefinitionStatus.VALIDATED,
            definition=definition,
            compiled_at=datetime.now(UTC),
        )

    def _compile_sql(self, definition: MetricDefinition, definition_hash: str) -> str:
        entity = definition.entity
        hours = definition.attribution_window.value
        if definition.type == MetricType.PROPORTION:
            assert definition.numerator is not None
            numerator_event = definition.numerator.event
            metric_expression = (
                "COUNT(DISTINCT IFF(outcome.outcome_event_id IS NOT NULL, "
                "exposure.metric_entity_id, NULL)) "
                "/ NULLIF(COUNT(DISTINCT exposure.metric_entity_id), 0)::FLOAT"
            )
            outcome_filter = f"outcome.outcome_name = '{numerator_event}'"
        else:
            assert definition.value is not None
            value_column = self._VALUE_COLUMNS.get(definition.value.field)
            if value_column is None:
                raise MetricReferenceError(
                    f"No analytical column mapping exists for '{definition.value.field}'."
                )
            metric_expression = f"AVG(outcome.{value_column})"
            outcome_filter = f"outcome.outcome_name = '{definition.value.event}'"

        return f"""-- Guardrail metric: {definition.name} v{definition.version}
-- Definition SHA-256: {definition_hash}
WITH scoped_exposures AS (
    SELECT
        experiment_id,
        allocation_version,
        variant_id,
        {entity} AS metric_entity_id,
        MIN(exposure_timestamp) AS first_exposure_at
    FROM GUARDRAIL.CORE.FCT_EXPERIMENT_EXPOSURES
    WHERE experiment_id = %(experiment_id)s
      AND allocation_version = %(allocation_version)s
      AND is_eligible_exposure
    GROUP BY experiment_id, allocation_version, variant_id, {entity}
),
scoped_outcomes AS (
    SELECT outcome.*
    FROM GUARDRAIL.CORE.FCT_ATTRIBUTED_OUTCOMES AS outcome
    WHERE outcome.experiment_id = %(experiment_id)s
      AND outcome.allocation_version = %(allocation_version)s
      AND {outcome_filter}
      AND outcome.seconds_after_exposure < {hours * 3600}
)
SELECT
    exposure.experiment_id,
    exposure.allocation_version,
    exposure.variant_id,
    '{definition.name}' AS metric_name,
    {definition.version} AS metric_version,
    COUNT(DISTINCT exposure.metric_entity_id) AS denominator_count,
    {metric_expression} AS metric_value
FROM scoped_exposures AS exposure
LEFT JOIN scoped_outcomes AS outcome
  ON exposure.experiment_id = outcome.experiment_id
 AND exposure.allocation_version = outcome.allocation_version
 AND exposure.variant_id = outcome.variant_id
 AND exposure.metric_entity_id = outcome.{entity}
GROUP BY exposure.experiment_id, exposure.allocation_version, exposure.variant_id
ORDER BY exposure.variant_id
"""
