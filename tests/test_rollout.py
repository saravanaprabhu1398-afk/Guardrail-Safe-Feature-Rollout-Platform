from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from guardrail.domain.analysis import (
    ExperimentAnalysisEvidence,
    ExperimentAnalysisReport,
    ExperimentAnalysisStatus,
)
from guardrail.domain.experiments import ExperimentCreate, RolloutState
from guardrail.domain.rollout import (
    InvalidRolloutTransitionError,
    RolloutDecision,
    RolloutDecisionRecord,
    RolloutGuardrailEvidence,
    RolloutPolicyError,
    StateTransitionRecord,
    TransitionRequest,
)
from guardrail.services.analysis import StatisticalAnalyzer
from guardrail.services.metrics import EventContractCatalog, MetricDefinitionLoader
from guardrail.services.rollout import (
    RolloutPolicy,
    RolloutPolicyEvaluator,
    RolloutService,
    RolloutStateMachine,
)
from guardrail.services.store import InMemoryExperimentStore

ROOT = Path(__file__).resolve().parents[1]


def complete_analysis() -> ExperimentAnalysisReport:
    evidence = ExperimentAnalysisEvidence(
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
    return ExperimentAnalysisReport(
        experiment_id="ranking_test",
        allocation_version=1,
        status=ExperimentAnalysisStatus.COMPLETE,
        analysis_blocked=False,
        blocking_checks=(),
        metrics=StatisticalAnalyzer().analyze(evidence),
        health_snapshot_id="health_test",
        analysis_snapshot_id="analysis_test",
        snapshot_created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def healthy_guardrails(**overrides: float | None) -> RolloutGuardrailEvidence:
    values: dict[str, object] = {
        "experiment_id": "ranking_test",
        "allocation_version": 1,
        "control_cancellation_rate": 0.10,
        "treatment_cancellation_rate": 0.101,
        "control_payment_failure_rate": 0.008,
        "treatment_payment_failure_rate": 0.009,
        "control_delivery_minutes": 35.0,
        "treatment_delivery_minutes": 32.0,
    }
    values.update(overrides)
    return RolloutGuardrailEvidence(**values)  # type: ignore[arg-type]


def active_metric_definitions():
    loader = MetricDefinitionLoader(
        ROOT / "contracts" / "metrics" / "metric-definition.v1.schema.json",
        EventContractCatalog.from_contracts(ROOT / "contracts" / "events"),
    )
    return tuple(loader.load(path) for path in sorted((ROOT / "metrics").glob("*.yml")))


def test_rollout_policy_is_loaded_from_active_metric_definitions() -> None:
    definitions = active_metric_definitions()

    policy = RolloutPolicy.from_metric_definitions(definitions)

    assert policy.primary_minimum_absolute_improvement == 0.02
    assert policy.maximum_cancellation_relative_increase == 0.02
    assert policy.maximum_delivery_minutes_increase == 3.0
    assert policy.maximum_payment_failure_rate == 0.01
    with pytest.raises(RolloutPolicyError, match="payment_failure_rate.*missing"):
        RolloutPolicy.from_metric_definitions(
            definition for definition in definitions if definition.name != "payment_failure_rate"
        )


def test_healthy_powered_experiment_ramps() -> None:
    decision, reason, checks = RolloutPolicyEvaluator().evaluate(
        complete_analysis(),
        healthy_guardrails(),
        current_state=RolloutState.RUNNING_5,
    )

    assert decision == RolloutDecision.RAMP
    assert "statistically and practically" in reason
    assert all(check.status == "PASS" for check in checks)


def test_blocked_or_harmful_experiment_pauses_and_repeated_harm_terminates() -> None:
    blocked = complete_analysis().model_copy(
        update={
            "status": ExperimentAnalysisStatus.BLOCKED,
            "analysis_blocked": True,
            "metrics": (),
        }
    )
    evaluator = RolloutPolicyEvaluator()

    blocked_decision, _, _ = evaluator.evaluate(
        blocked,
        healthy_guardrails(),
        current_state=RolloutState.RUNNING_5,
    )
    harmful_decision, _, checks = evaluator.evaluate(
        complete_analysis(),
        healthy_guardrails(treatment_payment_failure_rate=0.02),
        current_state=RolloutState.RUNNING_25,
    )
    repeated_decision, _, _ = evaluator.evaluate(
        complete_analysis(),
        healthy_guardrails(treatment_payment_failure_rate=0.02),
        current_state=RolloutState.PAUSED,
    )

    assert blocked_decision == RolloutDecision.PAUSE
    assert harmful_decision == RolloutDecision.PAUSE
    assert any(check.status == "FAIL" for check in checks)
    assert repeated_decision == RolloutDecision.TERMINATE


def test_zero_control_cancellation_with_treatment_fail_is_json_safe() -> None:
    decision, _, checks = RolloutPolicyEvaluator().evaluate(
        complete_analysis(),
        healthy_guardrails(
            control_cancellation_rate=0.0,
            treatment_cancellation_rate=0.01,
        ),
        current_state=RolloutState.RUNNING_5,
    )

    cancellation = next(check for check in checks if check.check_name == "cancellation_rate")
    assert decision == RolloutDecision.PAUSE
    assert cancellation.status == "FAIL"
    assert cancellation.observed_value == 1.0
    json.dumps(cancellation.model_dump(mode="json"), allow_nan=False)


def test_underpowered_experiment_continues_collecting_data() -> None:
    analysis = complete_analysis().model_copy(
        update={
            "status": ExperimentAnalysisStatus.UNDERPOWERED,
            "analysis_blocked": True,
        }
    )

    decision, reason, _ = RolloutPolicyEvaluator().evaluate(
        analysis,
        healthy_guardrails(),
        current_state=RolloutState.RUNNING_5,
    )

    assert decision == RolloutDecision.CONTINUE
    assert "not decision-ready" in reason


def test_rollout_state_machine_enforces_progression_and_terminal_rules() -> None:
    machine = RolloutStateMachine()

    assert (
        machine.next_state(RolloutState.RUNNING_5, RolloutDecision.RAMP) == RolloutState.RUNNING_25
    )
    assert (
        machine.next_state(RolloutState.RUNNING_25, RolloutDecision.RAMP) == RolloutState.RUNNING_50
    )
    assert (
        machine.next_state(RolloutState.RUNNING_50, RolloutDecision.RAMP) == RolloutState.RELEASED
    )
    assert machine.next_state(RolloutState.RUNNING_25, RolloutDecision.PAUSE) == RolloutState.PAUSED
    assert (
        machine.next_state(RolloutState.PAUSED, RolloutDecision.TERMINATE)
        == RolloutState.TERMINATED
    )
    with pytest.raises(InvalidRolloutTransitionError):
        machine.next_state(RolloutState.RELEASED, RolloutDecision.RAMP)


class StaticAnalysisService:
    def __init__(self, report: ExperimentAnalysisReport) -> None:
        self._report = report

    def get_results(
        self,
        experiment_id: str,
        allocation_version: int,
        expected_treatment_percentage: int,
    ) -> ExperimentAnalysisReport:
        return self._report


class StaticEvidenceRepository:
    def get_evidence(self, experiment_id: str, allocation_version: int) -> RolloutGuardrailEvidence:
        return healthy_guardrails()


class InMemoryDecisionRepository:
    def __init__(self) -> None:
        self.decisions: list[RolloutDecisionRecord] = []
        self.transitions: list[StateTransitionRecord] = []

    def persist_outcome(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord]:
        self.decisions.append(decision)
        self.transitions.append(transition)
        return decision, transition

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]:
        return tuple(item for item in self.decisions if item.experiment_id == experiment_id)


def test_rollout_service_persists_evidence_and_applies_ramp() -> None:
    store = InMemoryExperimentStore()
    store.create_experiment(
        ExperimentCreate(
            experiment_id="ranking_test",
            name="Ranking test",
            hypothesis="Ranking improves completed order conversion.",
            primary_metric="order_conversion_rate",
        )
    )
    store.launch_experiment("ranking_test")
    decisions = InMemoryDecisionRepository()
    service = RolloutService(
        store,
        StaticAnalysisService(complete_analysis()),  # type: ignore[arg-type]
        StaticEvidenceRepository(),
        decisions,
    )

    outcome = service.evaluate_and_transition(
        "ranking_test",
        TransitionRequest(requested_by="guardrail", approved_by="product-owner"),
    )

    assert outcome.decision.decision == RolloutDecision.RAMP
    assert outcome.experiment.state == RolloutState.RUNNING_25
    assert outcome.experiment.treatment_percentage == 25
    assert outcome.experiment.allocation_version == 2
    assert outcome.transition.previous_state == RolloutState.RUNNING_5
    assert outcome.transition.new_state == RolloutState.RUNNING_25
    assert service.list_decisions("ranking_test") == (outcome.decision,)
