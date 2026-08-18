from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from guardrail.domain.analysis import ExperimentAnalysisReport, ExperimentAnalysisStatus
from guardrail.domain.experiments import Experiment, RolloutState


class RolloutDecision(StrEnum):
    RAMP = "RAMP"
    CONTINUE = "CONTINUE"
    PAUSE = "PAUSE"
    TERMINATE = "TERMINATE"


class GuardrailCheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class GuardrailCheckName(StrEnum):
    CANCELLATION_RATE = "cancellation_rate"
    PAYMENT_FAILURE_RATE = "payment_failure_rate"
    DELIVERY_MINUTES = "average_delivery_minutes"


class RolloutGuardrailEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    allocation_version: int = Field(ge=1)
    control_cancellation_rate: float | None = Field(default=None, ge=0, le=1)
    treatment_cancellation_rate: float | None = Field(default=None, ge=0, le=1)
    control_payment_failure_rate: float | None = Field(default=None, ge=0, le=1)
    treatment_payment_failure_rate: float | None = Field(default=None, ge=0, le=1)
    control_delivery_minutes: float | None = Field(default=None, ge=0)
    treatment_delivery_minutes: float | None = Field(default=None, ge=0)


class GuardrailCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check_name: GuardrailCheckName
    status: GuardrailCheckStatus
    observed_value: float | None
    threshold: float
    message: str


class RolloutDecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(min_length=1)
    experiment_id: str
    allocation_version: int = Field(ge=1)
    previous_state: RolloutState
    recommended_state: RolloutState
    decision: RolloutDecision
    decision_reason: str
    analysis_status: ExperimentAnalysisStatus
    analysis_snapshot_id: str | None
    health_snapshot_id: str | None
    policy_version: str = Field(min_length=1)
    policy: dict[str, int | float]
    guardrail_evidence: RolloutGuardrailEvidence
    guardrail_checks: tuple[GuardrailCheckResult, ...]
    created_at: datetime


class StateTransitionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    transition_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    experiment_id: str
    allocation_version: int = Field(ge=1)
    previous_state: RolloutState
    new_state: RolloutState
    decision: RolloutDecision
    decision_reason: str
    analysis_snapshot_id: str | None
    health_snapshot_id: str | None
    policy_version: str
    requested_by: str = Field(min_length=1, max_length=120)
    approved_by: str = Field(min_length=1, max_length=120)
    created_at: datetime


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_by: str = Field(min_length=1, max_length=120)
    approved_by: str = Field(min_length=1, max_length=120)


class RolloutTransitionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment: Experiment
    analysis: ExperimentAnalysisReport
    decision: RolloutDecisionRecord
    transition: StateTransitionRecord


class DecisionEvidenceRepository(Protocol):
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> RolloutGuardrailEvidence: ...


class DecisionRepository(Protocol):
    def persist_outcome(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord]: ...

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]: ...


class AtomicRolloutRepository(Protocol):
    """Persists a decision, transition, and state mutation in one transaction."""

    def persist_outcome_and_apply(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
        *,
        treatment_percentage: int,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord, Experiment]: ...


class RolloutWorkflow(Protocol):
    def evaluate_and_transition(
        self, experiment_id: str, request: TransitionRequest
    ) -> RolloutTransitionOutcome: ...

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]: ...


class InvalidRolloutTransitionError(ValueError):
    pass


class DecisionEvidenceError(RuntimeError):
    pass


class DecisionPersistenceError(RuntimeError):
    pass


class RolloutPolicyError(ValueError):
    pass
