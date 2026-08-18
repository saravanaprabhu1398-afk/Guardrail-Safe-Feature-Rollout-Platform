from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from guardrail.domain.events import EventName, FaultProfile, ProductEvent
from guardrail.domain.experiments import Assignment, Variant
from guardrail.services.simulator import MarketplaceSimulator

ROOT = Path(__file__).parents[1]
COMMON_SCHEMA_PATH = ROOT / "contracts/events/common/product-event-envelope.v1.schema.json"
EVENT_SCHEMA_DIR = ROOT / "contracts/events/v1"
EXAMPLE_DIR = ROOT / "contracts/events/examples/v1"
SNOWFLAKE_DDL = ROOT / "snowflake/ddl/02_ingestion.sql"
NOW = datetime(2026, 8, 6, 10, tzinfo=UTC)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


COMMON_SCHEMA = load_json(COMMON_SCHEMA_PATH)
EVENT_SCHEMAS = {
    schema["x-guardrail-event-name"]: schema
    for path in sorted(EVENT_SCHEMA_DIR.glob("*.schema.json"))
    if (schema := load_json(path))
}
EXAMPLES = {path.stem: load_json(path) for path in sorted(EXAMPLE_DIR.glob("*.json"))}
REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema))
    for schema in [COMMON_SCHEMA, *EVENT_SCHEMAS.values()]
)


def validator(event_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        EVENT_SCHEMAS[event_name],
        registry=REGISTRY,
        format_checker=FormatChecker(),
    )


def test_schema_catalog_matches_python_examples_and_snowflake_registry() -> None:
    python_names = {item.value for item in EventName}
    schema_names = set(EVENT_SCHEMAS)
    example_names = set(EXAMPLES)
    ddl_contracts = {
        (event_name, int(version))
        for event_name, version in re.findall(
            r"\('([a-z_]+)',\s*(\d+),",
            SNOWFLAKE_DDL.read_text(encoding="utf-8"),
        )
    }

    assert schema_names == python_names
    assert example_names == python_names
    assert ddl_contracts == {(event_name, 1) for event_name in python_names}
    assert all(schema["x-guardrail-event-version"] == 1 for schema in EVENT_SCHEMAS.values())


@pytest.mark.parametrize("schema", [COMMON_SCHEMA, *EVENT_SCHEMAS.values()])
def test_contract_is_valid_draft_2020_12_schema(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("event_name", sorted(EVENT_SCHEMAS))
def test_versioned_example_satisfies_json_schema_and_python_model(event_name: str) -> None:
    example = EXAMPLES[event_name]

    validator(event_name).validate(example)
    event = ProductEvent.model_validate(example)

    assert event.event_name.value == event_name
    assert event.event_version == 1


@pytest.mark.parametrize("event_name", sorted(EVENT_SCHEMAS))
def test_contract_rejects_missing_event_specific_attribute(event_name: str) -> None:
    payload = deepcopy(EXAMPLES[event_name])
    required_attributes = EVENT_SCHEMAS[event_name]["allOf"][1]["properties"]["attributes"][
        "required"
    ]
    payload["attributes"].pop(required_attributes[0])

    with pytest.raises(ValidationError):
        validator(event_name).validate(payload)


def test_contract_rejects_name_version_and_unknown_attribute_drift() -> None:
    wrong_name = deepcopy(EXAMPLES[EventName.EXPERIMENT_ASSIGNED.value])
    wrong_name["event_name"] = EventName.ORDER_COMPLETED.value
    with pytest.raises(ValidationError):
        validator(EventName.EXPERIMENT_ASSIGNED.value).validate(wrong_name)

    wrong_version = deepcopy(EXAMPLES[EventName.ORDER_COMPLETED.value])
    wrong_version["event_version"] = 2
    with pytest.raises(ValidationError):
        validator(EventName.ORDER_COMPLETED.value).validate(wrong_version)

    unknown_attribute = deepcopy(EXAMPLES[EventName.ORDER_COMPLETED.value])
    unknown_attribute["attributes"]["unregistered_field"] = True
    with pytest.raises(ValidationError):
        validator(EventName.ORDER_COMPLETED.value).validate(unknown_attribute)


def test_reserved_transport_lineage_is_valid_but_unregistered_top_level_data_is_not() -> None:
    payload = deepcopy(EXAMPLES[EventName.EXPERIMENT_EXPOSED.value])
    payload["_guardrail_transport"] = {
        "topic": "guardrail.product-events.v1",
        "partition": 2,
        "offset": 42,
        "timestamp": 1_786_011_601_200,
        "transport_batch_id": "batch_1",
        "transport_message_id": "message_1",
        "produced_at": "2026-08-06T10:00:01.100Z",
    }
    validator(EventName.EXPERIMENT_EXPOSED.value).validate(payload)

    payload["producer_debug_data"] = "not registered"
    with pytest.raises(ValidationError):
        validator(EventName.EXPERIMENT_EXPOSED.value).validate(payload)


def test_simulator_events_conform_to_the_registered_contracts() -> None:
    simulator = MarketplaceSimulator()
    observed: set[str] = set()

    for index in range(2_000):
        variant = Variant.TREATMENT if index % 2 else Variant.CONTROL
        assignment = Assignment(
            experiment_id="new_ranking_algorithm",
            subject_id=f"contract_user_{index:06d}",
            variant=variant,
            bucket=index % 10_000,
            allocation_version=1,
            assigned_at=NOW,
        )
        events = simulator.generate_journey(
            assignment,
            seed=7,
            started_at=NOW,
            fault_profile=FaultProfile(),
        )
        for event in events:
            payload = event.model_dump(mode="json")
            validator(event.event_name.value).validate(payload)
            observed.add(event.event_name.value)
        if observed == set(EVENT_SCHEMAS):
            break

    assert observed == set(EVENT_SCHEMAS)
