from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntegrityCheckName(StrEnum):
    SAMPLE_RATIO_MISMATCH = "sample_ratio_mismatch"
    VARIANT_FLICKER = "variant_flicker"
    ASSIGNMENT_EXPOSURE_CONSISTENCY = "assignment_exposure_consistency"
    EVENT_LOSS = "event_loss"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    LATE_DATA = "late_data"
    VARIANT_FRESHNESS_SKEW = "variant_freshness_skew"


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
    raw_event_deliveries: int = Field(ge=0)
    duplicate_event_deliveries: int = Field(ge=0)
    control_canonical_events: int = Field(ge=0)
    treatment_canonical_events: int = Field(ge=0)
    control_late_events: int = Field(ge=0)
    treatment_late_events: int = Field(ge=0)
    control_p95_receive_lag_seconds: float | None
    treatment_p95_receive_lag_seconds: float | None

    @property
    def assigned_users(self) -> int:
        return self.control_assignments + self.treatment_assignments

    @property
    def exposed_users(self) -> int:
        return self.control_exposed_users + self.treatment_exposed_users

    @property
    def canonical_events(self) -> int:
        return self.control_canonical_events + self.treatment_canonical_events

    @property
    def late_events(self) -> int:
        return self.control_late_events + self.treatment_late_events

    @model_validator(mode="after")
    def validate_related_counts(self) -> ExperimentIntegrityEvidence:
        if self.variant_flicker_users > self.exposed_users:
            raise ValueError("variant_flicker_users cannot exceed exposed users")
        if self.assignment_variant_flicker_users > self.assigned_users:
            raise ValueError("assignment_variant_flicker_users cannot exceed assigned users")
        if self.assignments_without_exposure > self.assigned_users:
            raise ValueError("assignments_without_exposure cannot exceed assigned users")
        if self.control_exposed_users > self.control_assignments:
            raise ValueError("control exposed users cannot exceed control assignments")
        if self.treatment_exposed_users > self.treatment_assignments:
            raise ValueError("treatment exposed users cannot exceed treatment assignments")
        invalid_exposures = self.orphan_exposures + self.variant_mismatch_exposures
        if invalid_exposures > self.total_exposures:
            raise ValueError("invalid exposure counts cannot exceed total_exposures")
        if self.duplicate_event_deliveries > self.raw_event_deliveries:
            raise ValueError("duplicate deliveries cannot exceed raw event deliveries")
        if self.control_late_events > self.control_canonical_events:
            raise ValueError("control late events cannot exceed control canonical events")
        if self.treatment_late_events > self.treatment_canonical_events:
            raise ValueError("treatment late events cannot exceed treatment canonical events")
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
