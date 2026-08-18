from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guardrail.domain.integrity import (
    ExperimentHealthReport,
    ExperimentIntegrityEvidence,
    IntegrityCheckName,
)


class MetricType(StrEnum):
    PROPORTION = "PROPORTION"
    CONTINUOUS = "CONTINUOUS"


class MetricDirection(StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"


class StatisticalTest(StrEnum):
    TWO_PROPORTION_Z = "TWO_PROPORTION_Z"
    WELCH_T = "WELCH_T"


class MetricAnalysisStatus(StrEnum):
    COMPLETE = "COMPLETE"
    UNDERPOWERED = "UNDERPOWERED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ExperimentAnalysisStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNDERPOWERED = "UNDERPOWERED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    BLOCKED = "BLOCKED"


class ExperimentAnalysisEvidence(BaseModel):
    """Summary statistics at experiment and allocation-version grain."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str = Field(min_length=1)
    allocation_version: int = Field(ge=1)
    control_exposed_users: int = Field(ge=0)
    control_converted_users: int = Field(ge=0)
    treatment_exposed_users: int = Field(ge=0)
    treatment_converted_users: int = Field(ge=0)
    control_delivery_observations: int = Field(ge=0)
    control_delivery_mean: float | None
    control_delivery_variance: float | None
    treatment_delivery_observations: int = Field(ge=0)
    treatment_delivery_mean: float | None
    treatment_delivery_variance: float | None

    @model_validator(mode="after")
    def validate_summary_statistics(self) -> ExperimentAnalysisEvidence:
        if self.control_converted_users > self.control_exposed_users:
            raise ValueError("control conversions cannot exceed exposed users")
        if self.treatment_converted_users > self.treatment_exposed_users:
            raise ValueError("treatment conversions cannot exceed exposed users")

        continuous_groups = (
            (
                "control",
                self.control_delivery_observations,
                self.control_delivery_mean,
                self.control_delivery_variance,
            ),
            (
                "treatment",
                self.treatment_delivery_observations,
                self.treatment_delivery_mean,
                self.treatment_delivery_variance,
            ),
        )
        for name, count, mean, variance in continuous_groups:
            if count == 0 and (mean is not None or variance is not None):
                raise ValueError(f"{name} delivery statistics require observations")
            if count > 0 and mean is None:
                raise ValueError(f"{name} delivery mean is required")
            if variance is not None and variance < 0:
                raise ValueError(f"{name} delivery variance cannot be negative")
        return self


class MetricAnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric_name: str
    metric_type: MetricType
    desired_direction: MetricDirection
    status: MetricAnalysisStatus
    statistical_test: StatisticalTest
    control_sample_size: int = Field(ge=0)
    treatment_sample_size: int = Field(ge=0)
    control_estimate: float | None
    treatment_estimate: float | None
    absolute_lift: float | None
    relative_lift: float | None
    standard_error: float | None
    test_statistic: float | None
    degrees_of_freedom: float | None
    p_value: float | None
    confidence_level: float | None = Field(default=None, gt=0, lt=1)
    confidence_interval_lower: float | None = None
    confidence_interval_upper: float | None = None
    minimum_detectable_effect: float | None = Field(default=None, gt=0)
    target_power: float | None = Field(default=None, gt=0, lt=1)
    estimated_power: float | None = Field(default=None, ge=0, le=1)
    required_control_sample_size: int | None = Field(default=None, ge=1)
    required_treatment_sample_size: int | None = Field(default=None, ge=1)
    sample_size_satisfied: bool | None = None
    message: str

    @model_validator(mode="after")
    def validate_interval_and_sample_plan(self) -> MetricAnalysisResult:
        interval = (
            self.confidence_level,
            self.confidence_interval_lower,
            self.confidence_interval_upper,
        )
        if any(value is not None for value in interval) and any(
            value is None for value in interval
        ):
            raise ValueError("confidence interval fields must be provided together")
        if interval[1] is not None and interval[2] is not None and interval[1] > interval[2]:
            raise ValueError("confidence interval lower bound cannot exceed upper bound")

        sample_plan = (
            self.minimum_detectable_effect,
            self.target_power,
            self.estimated_power,
            self.required_control_sample_size,
            self.required_treatment_sample_size,
            self.sample_size_satisfied,
        )
        if any(value is not None for value in sample_plan) and any(
            value is None for value in sample_plan
        ):
            raise ValueError("sample-size planning fields must be provided together")
        return self


class ExperimentAnalysisReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    allocation_version: int
    status: ExperimentAnalysisStatus
    analysis_blocked: bool
    blocking_checks: tuple[IntegrityCheckName, ...]
    metrics: tuple[MetricAnalysisResult, ...]
    health_snapshot_id: str | None = None
    analysis_snapshot_id: str | None = None
    snapshot_created_at: datetime | None = None


class ExperimentAnalysisSnapshotPayload(BaseModel):
    """Complete inputs and outputs required to reproduce one analysis."""

    model_config = ConfigDict(frozen=True)

    integrity_policy_version: str = Field(min_length=1)
    integrity_policy: dict[str, int | float]
    integrity_evidence: ExperimentIntegrityEvidence
    health_report: ExperimentHealthReport
    analysis_policy_version: str = Field(min_length=1)
    analysis_policy: dict[str, int | float]
    analysis_evidence: ExperimentAnalysisEvidence | None
    analysis_report: ExperimentAnalysisReport


class AnalysisSnapshotRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    health_snapshot_id: str = Field(min_length=1)
    analysis_snapshot_id: str = Field(min_length=1)
    created_at: datetime


class AnalysisEvidenceRepository(Protocol):
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentAnalysisEvidence: ...


class AnalysisSnapshotRepository(Protocol):
    def persist(self, payload: ExperimentAnalysisSnapshotPayload) -> AnalysisSnapshotRecord: ...


class AnalysisEvidenceError(RuntimeError):
    pass


class AnalysisSnapshotError(RuntimeError):
    pass
