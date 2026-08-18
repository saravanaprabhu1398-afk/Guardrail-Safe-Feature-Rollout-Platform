from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from guardrail.domain.analysis import (
    ExperimentAnalysisReport,
    ExperimentAnalysisStatus,
    MetricAnalysisStatus,
)
from guardrail.domain.experiments import Experiment, RolloutState
from guardrail.domain.metrics import DecisionRuleKind, MetricDefinition, MetricRole
from guardrail.domain.rollout import (
    AtomicRolloutRepository,
    DecisionEvidenceRepository,
    DecisionRepository,
    GuardrailCheckName,
    GuardrailCheckResult,
    GuardrailCheckStatus,
    InvalidRolloutTransitionError,
    RolloutDecision,
    RolloutDecisionRecord,
    RolloutGuardrailEvidence,
    RolloutPolicyError,
    RolloutTransitionOutcome,
    StateTransitionRecord,
    TransitionRequest,
)
from guardrail.services.analysis import ExperimentAnalysisService
from guardrail.services.store import ExperimentStore

ROLLOUT_POLICY_VERSION = "phase5-rollout-v1"


@dataclass(frozen=True)
class RolloutPolicy:
    primary_minimum_absolute_improvement: float = 0.02
    statistical_significance_alpha: float = 0.05
    maximum_cancellation_relative_increase: float = 0.02
    maximum_delivery_minutes_increase: float = 3.0
    maximum_payment_failure_rate: float = 0.01

    @classmethod
    def from_metric_definitions(cls, definitions: Iterable[MetricDefinition]) -> RolloutPolicy:
        by_name = {definition.name: definition for definition in definitions}

        def rule(
            name: str,
            role: MetricRole,
            kind: DecisionRuleKind,
        ) -> float:
            definition = by_name.get(name)
            if definition is None:
                raise RolloutPolicyError(f"Active rollout metric '{name}' is missing.")
            if definition.role != role or definition.decision_rule.kind != kind:
                raise RolloutPolicyError(
                    f"Active rollout metric '{name}' has an incompatible decision rule."
                )
            return definition.decision_rule.value

        return cls(
            primary_minimum_absolute_improvement=rule(
                "order_conversion_rate",
                MetricRole.PRIMARY,
                DecisionRuleKind.MINIMUM_ABSOLUTE_IMPROVEMENT,
            ),
            maximum_cancellation_relative_increase=rule(
                "cancellation_rate",
                MetricRole.GUARDRAIL,
                DecisionRuleKind.MAXIMUM_RELATIVE_INCREASE,
            ),
            maximum_delivery_minutes_increase=rule(
                "average_delivery_minutes",
                MetricRole.GUARDRAIL,
                DecisionRuleKind.MAXIMUM_ABSOLUTE_INCREASE,
            ),
            maximum_payment_failure_rate=rule(
                "payment_failure_rate",
                MetricRole.GUARDRAIL,
                DecisionRuleKind.MAXIMUM_VALUE,
            ),
        )

    def __post_init__(self) -> None:
        probabilities = (
            self.primary_minimum_absolute_improvement,
            self.statistical_significance_alpha,
            self.maximum_cancellation_relative_increase,
            self.maximum_payment_failure_rate,
        )
        if any(value < 0 or value > 1 for value in probabilities):
            raise ValueError("Rollout probability thresholds must be between zero and one.")
        if self.statistical_significance_alpha == 0:
            raise ValueError("Rollout significance alpha must be positive.")
        if self.maximum_delivery_minutes_increase < 0:
            raise ValueError("Delivery tolerance cannot be negative.")


class RolloutStateMachine:
    _RAMP_TARGETS = {
        RolloutState.RUNNING_5: RolloutState.RUNNING_25,
        RolloutState.RUNNING_25: RolloutState.RUNNING_50,
        RolloutState.RUNNING_50: RolloutState.RELEASED,
    }
    _RUNNING_STATES = frozenset(_RAMP_TARGETS)

    def next_state(self, current: RolloutState, decision: RolloutDecision) -> RolloutState:
        if decision == RolloutDecision.CONTINUE:
            return current
        if decision == RolloutDecision.RAMP:
            try:
                return self._RAMP_TARGETS[current]
            except KeyError as exc:
                raise InvalidRolloutTransitionError(
                    f"Cannot ramp an experiment from {current}."
                ) from exc
        if decision == RolloutDecision.PAUSE:
            if current in self._RUNNING_STATES:
                return RolloutState.PAUSED
            if current == RolloutState.PAUSED:
                return current
            raise InvalidRolloutTransitionError(f"Cannot pause an experiment from {current}.")
        if decision == RolloutDecision.TERMINATE:
            if current in self._RUNNING_STATES or current == RolloutState.PAUSED:
                return RolloutState.TERMINATED
            raise InvalidRolloutTransitionError(f"Cannot terminate an experiment from {current}.")
        raise InvalidRolloutTransitionError(f"Unsupported rollout decision {decision}.")


class RolloutPolicyEvaluator:
    def __init__(self, policy: RolloutPolicy | None = None) -> None:
        self._policy = policy or RolloutPolicy()

    @property
    def policy(self) -> RolloutPolicy:
        return self._policy

    def evaluate(
        self,
        analysis: ExperimentAnalysisReport,
        evidence: RolloutGuardrailEvidence,
        *,
        current_state: RolloutState,
    ) -> tuple[RolloutDecision, str, tuple[GuardrailCheckResult, ...]]:
        if analysis.status == ExperimentAnalysisStatus.BLOCKED:
            decision = (
                RolloutDecision.TERMINATE
                if current_state == RolloutState.PAUSED
                else RolloutDecision.PAUSE
            )
            return decision, "Required experiment-integrity checks block analysis.", ()

        checks = self._guardrail_checks(evidence)
        failed = tuple(check for check in checks if check.status == GuardrailCheckStatus.FAIL)
        if failed:
            decision = (
                RolloutDecision.TERMINATE
                if current_state == RolloutState.PAUSED
                else RolloutDecision.PAUSE
            )
            names = ", ".join(check.check_name for check in failed)
            return decision, f"Blocking product guardrails failed: {names}.", checks

        if analysis.status != ExperimentAnalysisStatus.COMPLETE:
            return (
                RolloutDecision.CONTINUE,
                "Integrity is healthy, but statistical evidence is not decision-ready.",
                checks,
            )
        if any(check.status == GuardrailCheckStatus.INSUFFICIENT_DATA for check in checks):
            return (
                RolloutDecision.CONTINUE,
                "Product guardrails need more observations before ramping.",
                checks,
            )

        primary = next(
            (
                metric
                for metric in analysis.metrics
                if metric.metric_name == "order_conversion_rate"
            ),
            None,
        )
        if primary is None or primary.status != MetricAnalysisStatus.COMPLETE:
            return RolloutDecision.CONTINUE, "The primary metric is not decision-ready.", checks

        interval_lower = primary.confidence_interval_lower
        interval_upper = primary.confidence_interval_upper
        absolute_lift = primary.absolute_lift
        if (
            absolute_lift is not None
            and primary.p_value is not None
            and interval_lower is not None
            and absolute_lift >= self._policy.primary_minimum_absolute_improvement
            and primary.p_value <= self._policy.statistical_significance_alpha
            and interval_lower > 0
        ):
            if current_state == RolloutState.PAUSED:
                return (
                    RolloutDecision.CONTINUE,
                    "Evidence is healthy; human approval is required to resume a paused rollout.",
                    checks,
                )
            return (
                RolloutDecision.RAMP,
                "Primary improvement is statistically and practically significant and all "
                "guardrails pass.",
                checks,
            )
        if (
            interval_upper is not None
            and interval_upper < -self._policy.primary_minimum_absolute_improvement
        ):
            decision = (
                RolloutDecision.TERMINATE
                if current_state == RolloutState.PAUSED
                else RolloutDecision.PAUSE
            )
            return decision, "The primary metric shows a practically harmful regression.", checks
        return (
            RolloutDecision.CONTINUE,
            "Evidence is valid, but the primary metric does not satisfy the ramp policy.",
            checks,
        )

    def _guardrail_checks(
        self, evidence: RolloutGuardrailEvidence
    ) -> tuple[GuardrailCheckResult, ...]:
        cancellation_relative_increase: float | None = None
        if (
            evidence.control_cancellation_rate is not None
            and evidence.treatment_cancellation_rate is not None
        ):
            if evidence.control_cancellation_rate == 0:
                cancellation_relative_increase = (
                    0.0 if evidence.treatment_cancellation_rate == 0 else 1.0
                )
            else:
                cancellation_relative_increase = (
                    evidence.treatment_cancellation_rate / evidence.control_cancellation_rate - 1
                )
        cancellation = self._check(
            GuardrailCheckName.CANCELLATION_RATE,
            cancellation_relative_increase,
            self._policy.maximum_cancellation_relative_increase,
            "relative treatment increase",
        )
        payment = self._check(
            GuardrailCheckName.PAYMENT_FAILURE_RATE,
            evidence.treatment_payment_failure_rate,
            self._policy.maximum_payment_failure_rate,
            "treatment rate",
        )
        delivery_increase = (
            evidence.treatment_delivery_minutes - evidence.control_delivery_minutes
            if evidence.treatment_delivery_minutes is not None
            and evidence.control_delivery_minutes is not None
            else None
        )
        delivery = self._check(
            GuardrailCheckName.DELIVERY_MINUTES,
            delivery_increase,
            self._policy.maximum_delivery_minutes_increase,
            "treatment-minus-control minutes",
        )
        return cancellation, payment, delivery

    @staticmethod
    def _check(
        name: GuardrailCheckName,
        observed: float | None,
        threshold: float,
        measure: str,
    ) -> GuardrailCheckResult:
        if observed is None:
            return GuardrailCheckResult(
                check_name=name,
                status=GuardrailCheckStatus.INSUFFICIENT_DATA,
                observed_value=None,
                threshold=threshold,
                message=f"No {measure} evidence is available.",
            )
        failed = observed > threshold
        return GuardrailCheckResult(
            check_name=name,
            status=GuardrailCheckStatus.FAIL if failed else GuardrailCheckStatus.PASS,
            observed_value=observed,
            threshold=threshold,
            message=(
                f"{measure.capitalize()} exceeds policy."
                if failed
                else f"{measure.capitalize()} is within policy."
            ),
        )


class RolloutService:
    _STATE_PERCENTAGES = {
        RolloutState.RUNNING_5: 5,
        RolloutState.RUNNING_25: 25,
        RolloutState.RUNNING_50: 50,
        RolloutState.RELEASED: 100,
    }

    def __init__(
        self,
        store: ExperimentStore,
        analysis_service: ExperimentAnalysisService,
        evidence_repository: DecisionEvidenceRepository,
        decision_repository: DecisionRepository,
        atomic_repository: AtomicRolloutRepository | None = None,
        evaluator: RolloutPolicyEvaluator | None = None,
        state_machine: RolloutStateMachine | None = None,
    ) -> None:
        self._store = store
        self._analysis_service = analysis_service
        self._evidence_repository = evidence_repository
        self._decision_repository = decision_repository
        self._atomic_repository = atomic_repository
        self._evaluator = evaluator or RolloutPolicyEvaluator()
        self._state_machine = state_machine or RolloutStateMachine()

    def evaluate_and_transition(
        self, experiment_id: str, request: TransitionRequest
    ) -> RolloutTransitionOutcome:
        experiment = self._store.get_experiment(experiment_id)
        analysis = self._analysis_service.get_results(
            experiment.experiment_id,
            experiment.allocation_version,
            experiment.treatment_percentage,
        )
        evidence = (
            RolloutGuardrailEvidence(
                experiment_id=experiment.experiment_id,
                allocation_version=experiment.allocation_version,
            )
            if analysis.status == ExperimentAnalysisStatus.BLOCKED
            else self._evidence_repository.get_evidence(
                experiment.experiment_id,
                experiment.allocation_version,
            )
        )
        decision, reason, checks = self._evaluator.evaluate(
            analysis,
            evidence,
            current_state=experiment.state,
        )
        target_state = self._state_machine.next_state(experiment.state, decision)
        created_at = datetime.now(UTC)
        decision_id = self._decision_id(experiment, analysis, evidence, decision, target_state)
        decision_record = RolloutDecisionRecord(
            decision_id=decision_id,
            experiment_id=experiment.experiment_id,
            allocation_version=experiment.allocation_version,
            previous_state=experiment.state,
            recommended_state=target_state,
            decision=decision,
            decision_reason=reason,
            analysis_status=analysis.status,
            analysis_snapshot_id=analysis.analysis_snapshot_id,
            health_snapshot_id=analysis.health_snapshot_id,
            policy_version=ROLLOUT_POLICY_VERSION,
            policy=asdict(self._evaluator.policy),
            guardrail_evidence=evidence,
            guardrail_checks=checks,
            created_at=created_at,
        )
        transition = StateTransitionRecord(
            transition_id=self._transition_id(decision_id, experiment.state, target_state),
            decision_id=decision_id,
            experiment_id=experiment.experiment_id,
            allocation_version=experiment.allocation_version,
            previous_state=experiment.state,
            new_state=target_state,
            decision=decision,
            decision_reason=reason,
            analysis_snapshot_id=analysis.analysis_snapshot_id,
            health_snapshot_id=analysis.health_snapshot_id,
            policy_version=ROLLOUT_POLICY_VERSION,
            requested_by=request.requested_by,
            approved_by=request.approved_by,
            created_at=created_at,
        )
        treatment_percentage = self._STATE_PERCENTAGES.get(
            target_state, experiment.treatment_percentage
        )
        if self._atomic_repository is not None:
            persisted_decision, persisted_transition, updated = (
                self._atomic_repository.persist_outcome_and_apply(
                    decision_record,
                    transition,
                    treatment_percentage=treatment_percentage,
                )
            )
        else:
            persisted_decision, persisted_transition = self._decision_repository.persist_outcome(
                decision_record,
                transition,
            )
            updated = self._store.apply_rollout_state(
                experiment.experiment_id,
                target_state,
                treatment_percentage=treatment_percentage,
                expected_state=experiment.state,
                expected_allocation_version=experiment.allocation_version,
            )
        return RolloutTransitionOutcome(
            experiment=updated,
            analysis=analysis,
            decision=persisted_decision,
            transition=persisted_transition,
        )

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]:
        self._store.get_experiment(experiment_id)
        return self._decision_repository.list_decisions(experiment_id)

    def _decision_id(
        self,
        experiment: Experiment,
        analysis: ExperimentAnalysisReport,
        evidence: RolloutGuardrailEvidence,
        decision: RolloutDecision,
        target_state: RolloutState,
    ) -> str:
        content = {
            "experiment_id": experiment.experiment_id,
            "allocation_version": experiment.allocation_version,
            "state": experiment.state,
            "analysis_snapshot_id": analysis.analysis_snapshot_id,
            "analysis": analysis.model_dump(mode="json"),
            "guardrail_evidence": evidence.model_dump(mode="json"),
            "policy_version": ROLLOUT_POLICY_VERSION,
            "policy": asdict(self._evaluator.policy),
            "decision": decision,
            "target_state": target_state,
        }
        digest = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"decision_{digest}"

    @staticmethod
    def _transition_id(
        decision_id: str, previous_state: RolloutState, new_state: RolloutState
    ) -> str:
        digest = hashlib.sha256(f"{decision_id}:{previous_state}:{new_state}".encode()).hexdigest()
        return f"transition_{digest}"
