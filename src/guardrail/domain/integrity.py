from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntegrityCheckName(StrEnum):
    SAMPLE_RATIO_MISMATCH = "sample_ratio_mismatch"
    VARIANT_FLICKER = "variant_flicker"
    ASSIGNMENT_EXPOSURE_CONSISTENCY = "assignment_exposure_consistency"


class IntegrityCheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class IntegritySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class IntegrityAction(StrEnum):
    NONE = "NONE"
    CONTINUE = "CONTINUE"
    PAUSE = "PAUSE"


class ExperimentIntegrityEvidence(BaseModel):
    """Warehouse evidence at experiment and allocation-version grain."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str = Field(min_length=1)
    allocation_version: int = Field(ge=1)
    control_assignments: int = Field(ge=0)
    treatment_assignments: int = Field(ge=0)
    control_exposed_users: int = Field(ge=0)
    treatment_exposed_users: int = Field(ge=0)
    total_exposures: int = Field(ge=0)
    variant_flicker_users: int = Field(ge=0)
    assignment_variant_flicker_users: int = Field(ge=0)
    orphan_exposures: int = Field(ge=0)
    variant_mismatch_exposures: int = Field(ge=0)
    assignments_without_exposure: int = Field(ge=0)

    @property
    def assigned_users(self) -> int:
        return self.control_assignments + self.treatment_assignments

    @property
    def exposed_users(self) -> int:
        return self.control_exposed_users + self.treatment_exposed_users

    @model_validator(mode="after")
    def validate_related_counts(self) -> ExperimentIntegrityEvidence:
        if self.variant_flicker_users > self.exposed_users:
            raise ValueError("variant_flicker_users cannot exceed exposed users")
        if self.assignment_variant_flicker_users > self.assigned_users:
            raise ValueError("assignment_variant_flicker_users cannot exceed assigned users")
        if self.assignments_without_exposure > self.assigned_users:
            raise ValueError("assignments_without_exposure cannot exceed assigned users")
        invalid_exposures = self.orphan_exposures + self.variant_mismatch_exposures
        if invalid_exposures > self.total_exposures:
            raise ValueError("invalid exposure counts cannot exceed total_exposures")
        return self


class IntegrityCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check_name: IntegrityCheckName
    status: IntegrityCheckStatus
    severity: IntegritySeverity
    blocks_analysis: bool
    observed_value: float | None
    expected_range: str
    threshold: float | None = None
    p_value: float | None = None
    evidence: dict[str, int | float]
    message: str
    recommended_action: IntegrityAction


class ExperimentHealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    allocation_version: int
    expected_treatment_percentage: int = Field(gt=0, lt=100)
    analysis_blocked: bool
    checks: tuple[IntegrityCheckResult, ...]


class IntegrityEvidenceRepository(Protocol):
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentIntegrityEvidence: ...


class IntegrityEvidenceError(RuntimeError):
    pass
