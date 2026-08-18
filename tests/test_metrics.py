from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from guardrail.domain.metrics import MetricDefinitionError, MetricReferenceError
from guardrail.services.metrics import EventContractCatalog, MetricCompiler, MetricDefinitionLoader

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "metrics" / "metric-definition.v1.schema.json"
METRIC_DIR = ROOT / "metrics"
EVENT_CONTRACTS = ROOT / "contracts" / "events"
ROLLOUT_DDL = ROOT / "snowflake" / "ddl" / "05_metrics_rollout.sql"


def loader() -> MetricDefinitionLoader:
    return MetricDefinitionLoader(
        SCHEMA_PATH,
        EventContractCatalog.from_contracts(EVENT_CONTRACTS),
    )


def metric_paths() -> list[Path]:
    return sorted(METRIC_DIR.glob("*.yml"))


def test_metric_schema_is_valid_and_all_catalog_definitions_compile() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    compiler = MetricCompiler()

    artifacts = [compiler.compile(loader().load(path)) for path in metric_paths()]

    assert [artifact.metric_name for artifact in artifacts] == [
        "average_delivery_minutes",
        "cancellation_rate",
        "order_conversion_rate",
        "payment_failure_rate",
    ]
    assert all(artifact.status == "VALIDATED" for artifact in artifacts)
    assert all("%(experiment_id)s" in artifact.compiled_sql for artifact in artifacts)
    assert all("%(allocation_version)s" in artifact.compiled_sql for artifact in artifacts)
    assert all(len(artifact.compiled_sql_hash) == 64 for artifact in artifacts)


def test_metric_compilation_is_deterministic() -> None:
    definition = loader().load(METRIC_DIR / "order_conversion_rate.v1.yml")
    compiler = MetricCompiler()

    first = compiler.compile(definition)
    second = compiler.compile(definition)

    assert first.artifact_id == second.artifact_id
    assert first.definition_hash == second.definition_hash
    assert first.compiled_sql == second.compiled_sql
    assert first.compiled_sql_hash == second.compiled_sql_hash


def test_metric_version_and_content_are_reflected_in_artifact_identity() -> None:
    definition = loader().load(METRIC_DIR / "order_conversion_rate.v1.yml")
    compiler = MetricCompiler()

    original = compiler.compile(definition)
    changed_content = compiler.compile(
        definition.model_copy(update={"description": f"{definition.description} Updated."})
    )
    next_version = compiler.compile(definition.model_copy(update={"version": 2}))

    assert changed_content.definition_hash != original.definition_hash
    assert changed_content.compiled_sql_hash != original.compiled_sql_hash
    assert next_version.metric_version == 2
    assert next_version.artifact_id.startswith("order_conversion_rate_v2_")
    assert next_version.artifact_id != original.artifact_id


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("numerator", "event", "unknown_event"), "not registered"),
        (("entity", None, "account_id"), "not registered"),
        (("denominator", "event", "checkout_started"), "experiment_exposed"),
    ],
)
def test_metric_loader_rejects_unknown_contract_references(
    tmp_path: Path,
    mutation: tuple[str, str | None, str],
    message: str,
) -> None:
    raw = yaml.safe_load((METRIC_DIR / "order_conversion_rate.v1.yml").read_text())
    parent, child, value = mutation
    if child is None:
        raw[parent] = value
    else:
        raw[parent][child] = value
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(MetricReferenceError, match=message):
        loader().load(path)


def test_metric_loader_rejects_unknown_continuous_value_field(tmp_path: Path) -> None:
    raw = yaml.safe_load((METRIC_DIR / "average_delivery_minutes.v1.yml").read_text())
    raw["value"]["field"] = "attributes.unregistered_duration"
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(MetricReferenceError, match="not registered"):
        loader().load(path)


def test_metric_loader_rejects_schema_and_shape_errors(tmp_path: Path) -> None:
    raw = yaml.safe_load((METRIC_DIR / "order_conversion_rate.v1.yml").read_text())
    raw["unexpected"] = True
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(MetricDefinitionError, match="schema validation"):
        loader().load(path)


def test_phase5_ddl_declares_versioned_metrics_and_immutable_decision_history() -> None:
    ddl = ROLLOUT_DDL.read_text(encoding="utf-8")

    assert "CONTROL.METRIC_DEFINITIONS" in ddl
    assert "CONTROL.ROLLOUT_DECISIONS" in ddl
    assert "CONTROL.STATE_TRANSITIONS" in ddl
    assert "PRIMARY KEY (METRIC_NAME, METRIC_VERSION) NOT ENFORCED" in ddl
    assert "PRIMARY KEY (DECISION_ID) NOT ENFORCED" in ddl
    assert "FOREIGN KEY (DECISION_ID)" in ddl
