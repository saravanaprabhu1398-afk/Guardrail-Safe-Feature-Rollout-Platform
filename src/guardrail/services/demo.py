from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from guardrail.domain.analysis import (
    ExperimentAnalysisEvidence,
    ExperimentAnalysisReport,
    ExperimentAnalysisStatus,
)
from guardrail.domain.experiments import ExperimentCreate
from guardrail.domain.integrity import IntegrityCheckName
from guardrail.domain.rollout import (
    RolloutDecision,
    RolloutDecisionRecord,
    RolloutGuardrailEvidence,
    RolloutTransitionOutcome,
    StateTransitionRecord,
    TransitionRequest,
)
from guardrail.services.analysis import StatisticalAnalyzer
from guardrail.services.rollout import RolloutService
from guardrail.services.store import InMemoryExperimentStore


class DemoScenario(StrEnum):
    HEALTHY_ROLLOUT = "healthy-rollout"
    TELEMETRY_CORRUPTION = "telemetry-corruption"
    HARMFUL_GUARDRAIL = "harmful-guardrail"


class DemoRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario: DemoScenario
    title: str
    narrative: str
    expected_decision: RolloutDecision
    passed: bool
    outcome: RolloutTransitionOutcome


class _StaticAnalysisService:
    def __init__(self, report: ExperimentAnalysisReport) -> None:
        self._report = report

    def get_results(
        self,
        experiment_id: str,
        allocation_version: int,
        expected_treatment_percentage: int,
    ) -> ExperimentAnalysisReport:
        return self._report


class _StaticEvidenceRepository:
    def __init__(self, evidence: RolloutGuardrailEvidence) -> None:
        self._evidence = evidence

    def get_evidence(self, experiment_id: str, allocation_version: int) -> RolloutGuardrailEvidence:
        return self._evidence


class _DecisionRepository:
    def __init__(self) -> None:
        self._decisions: list[RolloutDecisionRecord] = []

    def persist_outcome(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord]:
        self._decisions.append(decision)
        return decision, transition

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]:
        return tuple(
            decision for decision in self._decisions if decision.experiment_id == experiment_id
        )


def run_demo(scenario: DemoScenario) -> DemoRun:
    experiment_id = f"phase6_{scenario.value.replace('-', '_')}"
    analysis = _healthy_analysis(experiment_id)
    evidence = _healthy_guardrails(experiment_id)
    expected = RolloutDecision.RAMP
    title = "Healthy experiment ramps from 5% to 25%"
    narrative = (
        "Integrity and power are ready, conversion clears the practical threshold, "
        "and all product guardrails pass."
    )

    if scenario == DemoScenario.TELEMETRY_CORRUPTION:
        analysis = analysis.model_copy(
            update={
                "status": ExperimentAnalysisStatus.BLOCKED,
                "analysis_blocked": True,
                "blocking_checks": (
                    IntegrityCheckName.VARIANT_FLICKER,
                    IntegrityCheckName.EVENT_LOSS,
                ),
                "metrics": (),
            }
        )
        expected = RolloutDecision.PAUSE
        title = "A false winner is paused when telemetry is corrupt"
        narrative = (
            "The apparent conversion win cannot be interpreted because variant flicker and "
            "event loss fail required integrity gates."
        )
    elif scenario == DemoScenario.HARMFUL_GUARDRAIL:
        evidence = evidence.model_copy(
            update={
                "treatment_cancellation_rate": 0.14,
                "treatment_payment_failure_rate": 0.02,
                "treatment_delivery_minutes": 40.0,
            }
        )
        expected = RolloutDecision.PAUSE
        title = "A local conversion win pauses on global harm"
        narrative = (
            "Conversion wins, but cancellation, payment failure, and delivery-time "
            "guardrails exceed their active tolerances."
        )

    store = InMemoryExperimentStore()
    store.create_experiment(
        ExperimentCreate(
            experiment_id=experiment_id,
            name=title,
            hypothesis="Personalized ranking improves conversion without harming the marketplace.",
            primary_metric="order_conversion_rate",
        )
    )
    store.launch_experiment(experiment_id)
    service = RolloutService(
        store,
        _StaticAnalysisService(analysis),  # type: ignore[arg-type]
        _StaticEvidenceRepository(evidence),
        _DecisionRepository(),
    )
    outcome = service.evaluate_and_transition(
        experiment_id,
        TransitionRequest(requested_by="phase6-demo", approved_by="portfolio-demo"),
    )
    return DemoRun(
        scenario=scenario,
        title=title,
        narrative=narrative,
        expected_decision=expected,
        passed=outcome.decision.decision == expected,
        outcome=outcome,
    )


def _healthy_analysis(experiment_id: str) -> ExperimentAnalysisReport:
    evidence = ExperimentAnalysisEvidence(
        experiment_id=experiment_id,
        allocation_version=1,
        control_exposed_users=19_000,
        control_converted_users=7_600,
        treatment_exposed_users=1_000,
        treatment_converted_users=500,
        control_delivery_observations=400,
        control_delivery_mean=35.0,
        control_delivery_variance=25.0,
        treatment_delivery_observations=30,
        treatment_delivery_mean=32.0,
        treatment_delivery_variance=36.0,
    )
    return ExperimentAnalysisReport(
        experiment_id=experiment_id,
        allocation_version=1,
        status=ExperimentAnalysisStatus.COMPLETE,
        analysis_blocked=False,
        blocking_checks=(),
        metrics=StatisticalAnalyzer().analyze(evidence, expected_treatment_percentage=5),
        health_snapshot_id=f"health_{scenario_id(experiment_id)}",
        analysis_snapshot_id=f"analysis_{scenario_id(experiment_id)}",
        snapshot_created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def _healthy_guardrails(experiment_id: str) -> RolloutGuardrailEvidence:
    return RolloutGuardrailEvidence(
        experiment_id=experiment_id,
        allocation_version=1,
        control_cancellation_rate=0.10,
        treatment_cancellation_rate=0.101,
        control_payment_failure_rate=0.008,
        treatment_payment_failure_rate=0.009,
        control_delivery_minutes=35.0,
        treatment_delivery_minutes=32.0,
    )


def scenario_id(experiment_id: str) -> str:
    return experiment_id.removeprefix("phase6_")
