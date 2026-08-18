from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from guardrail.config import SnowflakeSettings
from guardrail.domain.analysis import (
    AnalysisSnapshotRecord,
    ExperimentAnalysisEvidence,
    ExperimentAnalysisSnapshotPayload,
    ExperimentAnalysisStatus,
    MetricAnalysisStatus,
    StatisticalTest,
)
from guardrail.domain.integrity import ExperimentIntegrityEvidence
from guardrail.infrastructure.analysis import SnowflakeAnalysisRepository
from guardrail.services.analysis import (
    AnalysisPolicy,
    ExperimentAnalysisService,
    StatisticalAnalyzer,
)
from guardrail.services.integrity import IntegrityService


def analysis_evidence(**overrides: object) -> ExperimentAnalysisEvidence:
    values: dict[str, object] = {
        "experiment_id": "ranking_test",
        "allocation_version": 1,
        "control_exposed_users": 2_000,
        "control_converted_users": 800,
        "treatment_exposed_users": 2_000,
        "treatment_converted_users": 920,
        "control_delivery_observations": 100,
        "control_delivery_mean": 35.0,
        "control_delivery_variance": 25.0,
        "treatment_delivery_observations": 110,
        "treatment_delivery_mean": 32.0,
        "treatment_delivery_variance": 36.0,
    }
    values.update(overrides)
    return ExperimentAnalysisEvidence(**values)  # type: ignore[arg-type]


def integrity_evidence(**overrides: object) -> ExperimentIntegrityEvidence:
    values: dict[str, object] = {
        "experiment_id": "ranking_test",
        "allocation_version": 1,
        "control_assignments": 2_000,
        "treatment_assignments": 2_000,
        "control_exposed_users": 2_000,
        "treatment_exposed_users": 2_000,
        "total_exposures": 4_000,
        "variant_flicker_users": 0,
        "assignment_variant_flicker_users": 0,
        "orphan_exposures": 0,
        "variant_mismatch_exposures": 0,
        "assignments_without_exposure": 0,
        "raw_event_deliveries": 4_000,
        "duplicate_event_deliveries": 0,
        "control_canonical_events": 2_000,
        "treatment_canonical_events": 2_000,
        "control_late_events": 0,
        "treatment_late_events": 0,
        "control_p95_receive_lag_seconds": 2.0,
        "treatment_p95_receive_lag_seconds": 3.0,
    }
    values.update(overrides)
    return ExperimentIntegrityEvidence(**values)  # type: ignore[arg-type]


def test_proportion_analysis_calculates_treatment_minus_control_lift() -> None:
    result = StatisticalAnalyzer().analyze(analysis_evidence())[0]
    pooled_rate = 1_720 / 4_000
    expected_se = sqrt(pooled_rate * (1 - pooled_rate) * (1 / 2_000 + 1 / 2_000))

    assert result.status == MetricAnalysisStatus.COMPLETE
    assert result.statistical_test == StatisticalTest.TWO_PROPORTION_Z
    assert result.control_estimate == pytest.approx(0.4)
    assert result.treatment_estimate == pytest.approx(0.46)
    assert result.absolute_lift == pytest.approx(0.06)
    assert result.relative_lift == pytest.approx(0.15)
    assert result.standard_error == pytest.approx(expected_se)
    assert result.test_statistic == pytest.approx(0.06 / expected_se)
    assert result.p_value is not None and 0 < result.p_value < 1
    assert result.confidence_level == 0.95
    assert result.confidence_interval_lower is not None
    assert result.confidence_interval_upper is not None
    assert (
        result.confidence_interval_lower < result.absolute_lift < result.confidence_interval_upper
    )
    assert result.minimum_detectable_effect == 0.05
    assert result.target_power == 0.8
    assert result.estimated_power is not None and result.estimated_power >= 0.8
    assert result.required_control_sample_size == 1_531
    assert result.required_treatment_sample_size == 1_531
    assert result.sample_size_satisfied is True


def test_proportion_analysis_rejects_sparse_binary_cells() -> None:
    result = StatisticalAnalyzer().analyze(
        analysis_evidence(
            control_exposed_users=20,
            control_converted_users=2,
            treatment_exposed_users=20,
            treatment_converted_users=4,
        )
    )[0]

    assert result.status == MetricAnalysisStatus.INSUFFICIENT_DATA
    assert result.p_value is None
    assert result.confidence_interval_lower is not None
    assert result.confidence_interval_upper is not None
    assert result.required_control_sample_size is not None
    assert result.required_treatment_sample_size is not None
    assert result.sample_size_satisfied is False


def test_proportion_analysis_marks_valid_but_underpowered_results() -> None:
    result = StatisticalAnalyzer().analyze(
        analysis_evidence(
            control_exposed_users=500,
            control_converted_users=200,
            treatment_exposed_users=500,
            treatment_converted_users=230,
        )
    )[0]

    assert result.status == MetricAnalysisStatus.UNDERPOWERED
    assert result.p_value is not None
    assert result.confidence_interval_lower is not None
    assert result.estimated_power == pytest.approx(0.3601008805920538)
    assert result.sample_size_satisfied is False


def test_sample_size_plan_accounts_for_unequal_allocation() -> None:
    analyzer = StatisticalAnalyzer()

    balanced = analyzer.analyze(analysis_evidence(), expected_treatment_percentage=50)[0]
    five_percent = analyzer.analyze(analysis_evidence(), expected_treatment_percentage=5)[0]

    assert five_percent.required_control_sample_size is not None
    assert balanced.required_control_sample_size is not None
    assert five_percent.required_treatment_sample_size is not None
    assert balanced.required_treatment_sample_size is not None
    assert five_percent.required_control_sample_size > balanced.required_control_sample_size
    assert five_percent.required_treatment_sample_size < balanced.required_treatment_sample_size


def test_welch_analysis_uses_unequal_variance_standard_error() -> None:
    result = StatisticalAnalyzer().analyze(analysis_evidence())[1]
    expected_se = sqrt(25 / 100 + 36 / 110)

    assert result.status == MetricAnalysisStatus.COMPLETE
    assert result.statistical_test == StatisticalTest.WELCH_T
    assert result.absolute_lift == pytest.approx(-3.0)
    assert result.relative_lift == pytest.approx(-3 / 35)
    assert result.standard_error == pytest.approx(expected_se)
    assert result.test_statistic == pytest.approx(-3 / expected_se)
    assert result.degrees_of_freedom is not None and result.degrees_of_freedom > 100
    assert result.p_value is not None and result.p_value < 0.001
    assert result.confidence_interval_lower is not None
    assert result.confidence_interval_upper is not None
    assert (
        result.confidence_interval_lower < result.absolute_lift < result.confidence_interval_upper
    )
    assert result.minimum_detectable_effect == 5.0
    assert result.estimated_power is not None and result.estimated_power > 0.99
    assert result.sample_size_satisfied is True


def test_welch_analysis_rejects_zero_variance() -> None:
    result = StatisticalAnalyzer().analyze(
        analysis_evidence(
            control_delivery_variance=0.0,
            treatment_delivery_variance=0.0,
        )
    )[1]

    assert result.status == MetricAnalysisStatus.INSUFFICIENT_DATA
    assert result.standard_error == 0
    assert result.p_value is None


def test_analysis_evidence_rejects_impossible_summaries() -> None:
    with pytest.raises(ValidationError, match="control conversions"):
        analysis_evidence(control_exposed_users=10, control_converted_users=11)

    with pytest.raises(ValidationError, match="variance cannot be negative"):
        analysis_evidence(control_delivery_variance=-1)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"confidence_level": 0.5}, "confidence level"),
        ({"target_power": 0.5}, "target power"),
        ({"conversion_minimum_detectable_effect": 1.0}, "Conversion minimum"),
        ({"delivery_minimum_detectable_effect": 0.0}, "Delivery minimum"),
    ],
)
def test_analysis_policy_rejects_invalid_uncertainty_settings(
    override: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AnalysisPolicy(**override)  # type: ignore[arg-type]


class StaticIntegrityRepository:
    def __init__(self, evidence: ExperimentIntegrityEvidence) -> None:
        self._evidence = evidence

    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentIntegrityEvidence:
        return self._evidence


class RecordingAnalysisRepository:
    def __init__(self, evidence: ExperimentAnalysisEvidence) -> None:
        self._evidence = evidence
        self.called = False

    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentAnalysisEvidence:
        self.called = True
        return self._evidence


class RecordingSnapshotRepository:
    def __init__(self) -> None:
        self.payloads: list[ExperimentAnalysisSnapshotPayload] = []

    def persist(self, payload: ExperimentAnalysisSnapshotPayload) -> AnalysisSnapshotRecord:
        self.payloads.append(payload)
        return AnalysisSnapshotRecord(
            health_snapshot_id="health_test",
            analysis_snapshot_id="analysis_test",
            created_at=datetime(2026, 8, 18, tzinfo=UTC),
        )


def test_integrity_failure_prevents_statistical_evidence_query() -> None:
    analysis_repository = RecordingAnalysisRepository(analysis_evidence())
    snapshot_repository = RecordingSnapshotRepository()
    service = ExperimentAnalysisService(
        IntegrityService(StaticIntegrityRepository(integrity_evidence(variant_flicker_users=1))),
        analysis_repository,
        snapshot_repository=snapshot_repository,
    )

    report = service.get_results("ranking_test", 1, 50)

    assert report.status == ExperimentAnalysisStatus.BLOCKED
    assert report.analysis_blocked is True
    assert report.metrics == ()
    assert analysis_repository.called is False
    assert report.health_snapshot_id == "health_test"
    assert report.analysis_snapshot_id == "analysis_test"
    assert snapshot_repository.payloads[0].analysis_evidence is None
    assert (
        snapshot_repository.payloads[0].analysis_report.status == ExperimentAnalysisStatus.BLOCKED
    )


def test_healthy_integrity_allows_complete_statistical_analysis() -> None:
    analysis_repository = RecordingAnalysisRepository(analysis_evidence())
    snapshot_repository = RecordingSnapshotRepository()
    service = ExperimentAnalysisService(
        IntegrityService(StaticIntegrityRepository(integrity_evidence())),
        analysis_repository,
        snapshot_repository=snapshot_repository,
    )

    report = service.get_results("ranking_test", 1, 50)

    assert report.status == ExperimentAnalysisStatus.COMPLETE
    assert report.analysis_blocked is False
    assert len(report.metrics) == 2
    assert analysis_repository.called is True
    assert report.snapshot_created_at == datetime(2026, 8, 18, tzinfo=UTC)
    snapshot = snapshot_repository.payloads[0]
    assert snapshot.analysis_evidence == analysis_evidence()
    assert snapshot.integrity_policy_version == "phase4-integrity-v1"
    assert snapshot.analysis_policy_version == "phase4-analysis-v1"
    assert snapshot.analysis_policy["target_power"] == 0.8


def test_incomplete_metric_evidence_blocks_a_complete_analysis_result() -> None:
    analysis_repository = RecordingAnalysisRepository(
        analysis_evidence(treatment_converted_users=4)
    )
    service = ExperimentAnalysisService(
        IntegrityService(StaticIntegrityRepository(integrity_evidence())),
        analysis_repository,
    )

    report = service.get_results("ranking_test", 1, 50)

    assert report.status == ExperimentAnalysisStatus.PARTIAL
    assert report.analysis_blocked is True
    assert report.metrics[0].status == MetricAnalysisStatus.INSUFFICIENT_DATA
    assert report.metrics[1].status == MetricAnalysisStatus.COMPLETE


def test_underpowered_metrics_block_a_statistical_decision() -> None:
    analysis_repository = RecordingAnalysisRepository(
        analysis_evidence(
            control_exposed_users=500,
            control_converted_users=200,
            treatment_exposed_users=500,
            treatment_converted_users=230,
            control_delivery_observations=5,
            treatment_delivery_observations=5,
        )
    )
    service = ExperimentAnalysisService(
        IntegrityService(StaticIntegrityRepository(integrity_evidence())),
        analysis_repository,
    )

    report = service.get_results("ranking_test", 1, 50)

    assert report.status == ExperimentAnalysisStatus.UNDERPOWERED
    assert report.analysis_blocked is True
    assert all(metric.status == MetricAnalysisStatus.UNDERPOWERED for metric in report.metrics)


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


def test_snowflake_repository_maps_analysis_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    row = (2_000, 800, 2_000, 920, 100, 35.0, 25.0, 110, 32.0, 36.0)

    @contextmanager
    def fake_connect(settings: SnowflakeSettings):
        captured["settings"] = settings
        yield FakeConnection(row, captured)

    monkeypatch.setattr("guardrail.infrastructure.analysis.connect", fake_connect)
    repository = SnowflakeAnalysisRepository(snowflake_settings(tmp_path / "pat"))

    evidence = repository.get_evidence("ranking_test", 1)

    assert evidence == analysis_evidence()
    assert captured["parameters"] == ("ranking_test", 1)
    assert captured["settings"].snowflake_warehouse == "GUARDRAIL_TRANSFORM_WH"
    assert captured["settings"].snowflake_schema == "METRICS"
