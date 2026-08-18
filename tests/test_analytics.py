import pytest

from guardrail.infrastructure.analytics import (
    EXPECTED_DYNAMIC_TABLES,
    Phase3Evidence,
)


def valid_evidence(**overrides: object) -> Phase3Evidence:
    values: dict[str, object] = {
        "staged_events": 900,
        "assignments": 200,
        "exposures": 203,
        "eligible_exposures": 194,
        "product_outcomes": 135,
        "attributed_outcomes": 135,
        "user_experiment_metrics": 194,
        "experiment_variant_metrics": 2,
        "variant_flicker_users": 3,
        "attribution_window_violations": 0,
        "eligible_exposure_violations": 0,
        "rate_bound_violations": 0,
        "dynamic_tables": frozenset(EXPECTED_DYNAMIC_TABLES),
    }
    values.update(overrides)
    return Phase3Evidence(**values)  # type: ignore[arg-type]


def test_phase3_evidence_accepts_complete_analytics_slice() -> None:
    valid_evidence().validate()


def test_phase3_evidence_requires_all_dynamic_tables() -> None:
    dynamic_tables = EXPECTED_DYNAMIC_TABLES - {("METRICS", "AGG_EXPERIMENT_RESULTS")}

    with pytest.raises(RuntimeError, match="METRICS.AGG_EXPERIMENT_RESULTS"):
        valid_evidence(dynamic_tables=frozenset(dynamic_tables)).validate()


def test_phase3_evidence_rejects_empty_analytical_facts() -> None:
    with pytest.raises(RuntimeError, match="attributed_outcomes"):
        valid_evidence(attributed_outcomes=0).validate()


def test_phase3_evidence_rejects_invariant_violations() -> None:
    with pytest.raises(RuntimeError, match="attribution_window=1"):
        valid_evidence(attribution_window_violations=1).validate()
