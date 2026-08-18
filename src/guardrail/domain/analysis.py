from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guardrail.domain.integrity import IntegrityCheckName


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
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ExperimentAnalysisStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
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
    message: str


class ExperimentAnalysisReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    allocation_version: int
    status: ExperimentAnalysisStatus
    analysis_blocked: bool
    blocking_checks: tuple[IntegrityCheckName, ...]
    metrics: tuple[MetricAnalysisResult, ...]


class AnalysisEvidenceRepository(Protocol):
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentAnalysisEvidence: ...


class AnalysisEvidenceError(RuntimeError):
    pass
