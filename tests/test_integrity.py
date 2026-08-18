from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from guardrail.config import SnowflakeSettings
from guardrail.domain.integrity import (
    ExperimentIntegrityEvidence,
    IntegrityAction,
    IntegrityCheckName,
    IntegrityCheckStatus,
)
from guardrail.infrastructure.integrity import SnowflakeIntegrityRepository
from guardrail.services.integrity import IntegrityEvaluator, IntegrityPolicy, IntegrityService


def healthy_evidence(**overrides: object) -> ExperimentIntegrityEvidence:
    values: dict[str, object] = {
        "experiment_id": "ranking_test",
        "allocation_version": 1,
        "control_assignments": 950,
        "treatment_assignments": 50,
        "control_exposed_users": 950,
        "treatment_exposed_users": 50,
        "total_exposures": 1_000,
        "variant_flicker_users": 0,
        "assignment_variant_flicker_users": 0,
        "orphan_exposures": 0,
        "variant_mismatch_exposures": 0,
        "assignments_without_exposure": 0,
        "raw_event_deliveries": 1_000,
        "duplicate_event_deliveries": 0,
        "control_canonical_events": 950,
        "treatment_canonical_events": 50,
        "control_late_events": 0,
        "treatment_late_events": 0,
        "control_p95_receive_lag_seconds": 2.0,
        "treatment_p95_receive_lag_seconds": 3.0,
    }
    values.update(overrides)
    return ExperimentIntegrityEvidence(**values)  # type: ignore[arg-type]


def test_healthy_experiment_passes_all_integrity_checks() -> None:
    report = IntegrityEvaluator().evaluate(healthy_evidence(), expected_treatment_percentage=5)

    assert report.analysis_blocked is False
    assert all(result.status == IntegrityCheckStatus.PASS for result in report.checks)
    sample_ratio = report.checks[0]
    assert sample_ratio.check_name == IntegrityCheckName.SAMPLE_RATIO_MISMATCH
    assert sample_ratio.p_value == pytest.approx(1.0)


def test_sample_ratio_mismatch_blocks_analysis() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(
            control_assignments=850,
            treatment_assignments=150,
            control_exposed_users=850,
            treatment_exposed_users=150,
        ),
        expected_treatment_percentage=5,
    )
    result = report.checks[0]

    assert report.analysis_blocked is True
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.p_value is not None
    assert result.p_value < 0.001
    assert result.recommended_action == IntegrityAction.PAUSE


def test_sample_ratio_check_requires_minimum_expected_cell_count() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(
            control_assignments=19,
            treatment_assignments=1,
            control_exposed_users=19,
            treatment_exposed_users=1,
            total_exposures=20,
        ),
        expected_treatment_percentage=5,
    )
    result = report.checks[0]

    assert result.status == IntegrityCheckStatus.INSUFFICIENT_DATA
    assert result.blocks_analysis is True
    assert result.recommended_action == IntegrityAction.CONTINUE


def test_variant_flicker_blocks_analysis() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(variant_flicker_users=2),
        expected_treatment_percentage=5,
    )
    result = report.checks[1]

    assert result.check_name == IntegrityCheckName.VARIANT_FLICKER
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.observed_value == pytest.approx(0.002)


@pytest.mark.parametrize(
    ("override", "evidence_key"),
    [
        ({"orphan_exposures": 1}, "orphan_exposures"),
        ({"variant_mismatch_exposures": 1}, "variant_mismatch_exposures"),
        ({"assignment_variant_flicker_users": 1}, "assignment_variant_flicker_users"),
    ],
)
def test_assignment_exposure_inconsistency_blocks_analysis(
    override: dict[str, int], evidence_key: str
) -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(**override), expected_treatment_percentage=5
    )
    result = report.checks[2]

    assert result.check_name == IntegrityCheckName.ASSIGNMENT_EXPOSURE_CONSISTENCY
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.evidence[evidence_key] > 0


def test_event_loss_blocks_analysis() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(
            treatment_exposed_users=40,
            assignments_without_exposure=10,
        ),
        expected_treatment_percentage=5,
    )
    result = report.checks[3]

    assert result.check_name == IntegrityCheckName.EVENT_LOSS
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.evidence["treatment_event_loss_rate"] == pytest.approx(0.2)
    assert result.blocks_analysis is True


def test_event_loss_requires_enough_assignments_in_each_variant() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(
            control_assignments=191,
            treatment_assignments=9,
            control_exposed_users=185,
            treatment_exposed_users=9,
            assignments_without_exposure=6,
        ),
        expected_treatment_percentage=5,
    )
    result = report.checks[3]

    assert result.status == IntegrityCheckStatus.INSUFFICIENT_DATA
    assert result.threshold == 20
    assert result.blocks_analysis is True


def test_excessive_duplicate_delivery_is_visible_but_does_not_block_analysis() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(duplicate_event_deliveries=100),
        expected_treatment_percentage=5,
    )
    result = report.checks[4]

    assert result.check_name == IntegrityCheckName.DUPLICATE_DELIVERY
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.observed_value == pytest.approx(0.1)
    assert result.blocks_analysis is False
    assert report.analysis_blocked is False


def test_late_data_blocks_analysis() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(control_late_events=100),
        expected_treatment_percentage=5,
    )
    result = report.checks[5]

    assert result.check_name == IntegrityCheckName.LATE_DATA
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.blocks_analysis is True


def test_variant_freshness_skew_blocks_analysis() -> None:
    report = IntegrityEvaluator().evaluate(
        healthy_evidence(treatment_p95_receive_lag_seconds=300.0),
        expected_treatment_percentage=5,
    )
    result = report.checks[6]

    assert result.check_name == IntegrityCheckName.VARIANT_FRESHNESS_SKEW
    assert result.status == IntegrityCheckStatus.FAIL
    assert result.observed_value == pytest.approx(298.0)
    assert result.blocks_analysis is True


def test_related_evidence_counts_are_validated() -> None:
    with pytest.raises(ValidationError, match="invalid exposure counts"):
        healthy_evidence(total_exposures=1, orphan_exposures=2)

    with pytest.raises(ValidationError, match="duplicate deliveries"):
        healthy_evidence(raw_event_deliveries=1, duplicate_event_deliveries=2)

    with pytest.raises(ValidationError, match="control late events"):
        healthy_evidence(control_canonical_events=1, control_late_events=2)


def test_invalid_integrity_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        IntegrityPolicy(maximum_variant_flicker_rate=1.1)


class FakeIntegrityRepository:
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentIntegrityEvidence:
        assert (experiment_id, allocation_version) == ("ranking_test", 1)
        return healthy_evidence()


def test_integrity_service_loads_repository_evidence() -> None:
    report = IntegrityService(FakeIntegrityRepository()).get_health("ranking_test", 1, 5)

    assert report.experiment_id == "ranking_test"
    assert report.analysis_blocked is False


class FakeCursor:
    def __init__(self, row: tuple[int | float | None, ...], captured: dict[str, Any]) -> None:
        self._row = row
        self._captured = captured

    def execute(self, query: str, parameters: tuple[object, ...]) -> FakeCursor:
        self._captured["query"] = query
        self._captured["parameters"] = parameters
        return self

    def fetchone(self) -> tuple[int | float | None, ...]:
        return self._row


class FakeConnection:
    def __init__(self, row: tuple[int | float | None, ...], captured: dict[str, Any]) -> None:
        self._cursor = FakeCursor(row, captured)

    def cursor(self) -> FakeCursor:
        return self._cursor


def snowflake_settings(pat_file: Path) -> SnowflakeSettings:
    return SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="USER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )


def test_snowflake_repository_maps_versioned_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    row = (
        950,
        50,
        950,
        50,
        1_000,
        0,
        0,
        0,
        0,
        0,
        1_000,
        0,
        950,
        50,
        0,
        0,
        2.0,
        3.0,
    )

    @contextmanager
    def fake_connect(settings: SnowflakeSettings):
        captured["settings"] = settings
        yield FakeConnection(row, captured)

    monkeypatch.setattr("guardrail.infrastructure.integrity.connect", fake_connect)
    repository = SnowflakeIntegrityRepository(snowflake_settings(tmp_path / "pat"))

    evidence = repository.get_evidence("ranking_test", 1)

    assert evidence == healthy_evidence()
    assert captured["parameters"] == ("ranking_test", 1)
    assert captured["settings"].snowflake_warehouse == "GUARDRAIL_TRANSFORM_WH"
    assert captured["settings"].snowflake_schema == "CONTROL"
