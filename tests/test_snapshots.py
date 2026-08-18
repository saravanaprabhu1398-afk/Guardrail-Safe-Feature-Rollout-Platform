from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from guardrail.config import SnowflakeSettings
from guardrail.domain.analysis import (
    AnalysisSnapshotError,
    ExperimentAnalysisEvidence,
    ExperimentAnalysisReport,
    ExperimentAnalysisSnapshotPayload,
    ExperimentAnalysisStatus,
)
from guardrail.domain.integrity import ExperimentIntegrityEvidence
from guardrail.infrastructure.snapshots import SnowflakeAnalysisSnapshotRepository
from guardrail.services.analysis import AnalysisPolicy, StatisticalAnalyzer
from guardrail.services.integrity import IntegrityEvaluator, IntegrityPolicy

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DDL = ROOT / "snowflake" / "ddl" / "04_analysis_snapshots.sql"


def snapshot_payload() -> ExperimentAnalysisSnapshotPayload:
    integrity_evidence = ExperimentIntegrityEvidence(
        experiment_id="ranking_test",
        allocation_version=1,
        control_assignments=2_000,
        treatment_assignments=2_000,
        control_exposed_users=2_000,
        treatment_exposed_users=2_000,
        total_exposures=4_000,
        variant_flicker_users=0,
        assignment_variant_flicker_users=0,
        orphan_exposures=0,
        variant_mismatch_exposures=0,
        assignments_without_exposure=0,
        raw_event_deliveries=4_000,
        duplicate_event_deliveries=0,
        control_canonical_events=2_000,
        treatment_canonical_events=2_000,
        control_late_events=0,
        treatment_late_events=0,
        control_p95_receive_lag_seconds=2.0,
        treatment_p95_receive_lag_seconds=3.0,
    )
    health_report = IntegrityEvaluator().evaluate(
        integrity_evidence,
        expected_treatment_percentage=50,
    )
    analysis_evidence = ExperimentAnalysisEvidence(
        experiment_id="ranking_test",
        allocation_version=1,
        control_exposed_users=2_000,
        control_converted_users=800,
        treatment_exposed_users=2_000,
        treatment_converted_users=920,
        control_delivery_observations=100,
        control_delivery_mean=35.0,
        control_delivery_variance=25.0,
        treatment_delivery_observations=110,
        treatment_delivery_mean=32.0,
        treatment_delivery_variance=36.0,
    )
    metrics = StatisticalAnalyzer().analyze(analysis_evidence)
    analysis_report = ExperimentAnalysisReport(
        experiment_id="ranking_test",
        allocation_version=1,
        status=ExperimentAnalysisStatus.COMPLETE,
        analysis_blocked=False,
        blocking_checks=(),
        metrics=metrics,
    )
    return ExperimentAnalysisSnapshotPayload(
        integrity_policy_version="phase4-integrity-v1",
        integrity_policy=asdict(IntegrityPolicy()),
        integrity_evidence=integrity_evidence,
        health_report=health_report,
        analysis_policy_version="phase4-analysis-v1",
        analysis_policy=asdict(AnalysisPolicy()),
        analysis_evidence=analysis_evidence,
        analysis_report=analysis_report,
    )


class FakeCursor:
    def __init__(self, captured: dict[str, Any], *, fail_analysis: bool = False) -> None:
        self._captured = captured
        self._fail_analysis = fail_analysis

    def execute(self, query: str, parameters: tuple[object, ...]) -> FakeCursor:
        self._captured.setdefault("calls", []).append((query, parameters))
        if self._fail_analysis and "MERGE INTO CONTROL.EXPERIMENT_ANALYSIS" in query:
            raise RuntimeError("warehouse failure")
        return self

    def fetchone(self) -> tuple[datetime]:
        return (datetime(2026, 8, 18, 12, 0),)


class FakeConnection:
    def __init__(self, captured: dict[str, Any], *, fail_analysis: bool = False) -> None:
        self._cursor = FakeCursor(captured, fail_analysis=fail_analysis)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self._cursor

    def autocommit(self, enabled: bool) -> None:
        assert enabled is False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def settings(pat_file: Path) -> SnowflakeSettings:
    return SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="USER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )


def test_snapshot_repository_uses_stable_content_addressed_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    connection = FakeConnection(captured)

    @contextmanager
    def fake_connect(connection_settings: SnowflakeSettings):
        captured["settings"] = connection_settings
        yield connection

    monkeypatch.setattr("guardrail.infrastructure.snapshots.connect", fake_connect)
    repository = SnowflakeAnalysisSnapshotRepository(settings(tmp_path / "pat"))
    payload = snapshot_payload()

    first = repository.persist(payload)
    second = repository.persist(payload)
    changed = repository.persist(
        payload.model_copy(update={"analysis_policy_version": "phase4-analysis-v2"})
    )

    assert first == second
    assert first.health_snapshot_id.startswith("health_")
    assert first.analysis_snapshot_id.startswith("analysis_")
    assert changed.health_snapshot_id == first.health_snapshot_id
    assert changed.analysis_snapshot_id != first.analysis_snapshot_id
    assert first.created_at.tzinfo is not None
    assert connection.commits == 3
    assert connection.rollbacks == 0
    assert captured["settings"].snowflake_warehouse == "GUARDRAIL_TRANSFORM_WH"
    assert captured["settings"].snowflake_schema == "CONTROL"
    assert "MERGE INTO CONTROL.EXPERIMENT_HEALTH_SNAPSHOTS" in captured["calls"][0][0]
    assert "MERGE INTO CONTROL.EXPERIMENT_ANALYSIS_SNAPSHOTS" in captured["calls"][1][0]


def test_snapshot_repository_rolls_back_and_masks_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    connection = FakeConnection(captured, fail_analysis=True)

    @contextmanager
    def fake_connect(connection_settings: SnowflakeSettings):
        yield connection

    monkeypatch.setattr("guardrail.infrastructure.snapshots.connect", fake_connect)
    repository = SnowflakeAnalysisSnapshotRepository(settings(tmp_path / "pat"))

    with pytest.raises(AnalysisSnapshotError, match="could not be persisted"):
        repository.persist(snapshot_payload())

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_phase4_snapshot_ddl_declares_both_immutable_control_tables() -> None:
    ddl = SNAPSHOT_DDL.read_text(encoding="utf-8")

    assert "CONTROL.EXPERIMENT_HEALTH_SNAPSHOTS" in ddl
    assert "CONTROL.EXPERIMENT_ANALYSIS_SNAPSHOTS" in ddl
    assert "INTEGRITY_EVIDENCE VARIANT NOT NULL" in ddl
    assert "ANALYSIS_EVIDENCE VARIANT" in ddl
    assert "CONTENT_HASH STRING NOT NULL" in ddl
    assert "PRIMARY KEY (HEALTH_SNAPSHOT_ID) NOT ENFORCED" in ddl
    assert "PRIMARY KEY (ANALYSIS_SNAPSHOT_ID) NOT ENFORCED" in ddl
