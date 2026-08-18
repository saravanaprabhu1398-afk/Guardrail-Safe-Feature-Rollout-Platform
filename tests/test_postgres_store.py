from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from guardrail.config import RuntimeSettings
from guardrail.domain.analysis import ExperimentAnalysisStatus
from guardrail.domain.experiments import (
    Assignment,
    ExperimentCreate,
    RolloutState,
    Variant,
    utc_now,
)
from guardrail.domain.rollout import (
    RolloutDecision,
    RolloutDecisionRecord,
    RolloutGuardrailEvidence,
    StateTransitionRecord,
)
from guardrail.infrastructure.outbox import OutboxEnvelope, PostgresOutboxWorker
from guardrail.infrastructure.postgres import (
    PostgresExperimentStore,
    PostgresRolloutRepository,
    metadata,
    outbox_events,
    rollout_decisions,
    state_transitions,
)
from guardrail.services.assignment import AssignmentService
from guardrail.services.store import (
    ConcurrentExperimentUpdateError,
    ExperimentAlreadyExistsError,
)


def store(tmp_path: Path) -> PostgresExperimentStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'control.db'}")
    metadata.create_all(engine)
    return PostgresExperimentStore(engine=engine)


def request() -> ExperimentCreate:
    return ExperimentCreate(
        experiment_id="production_ranking",
        name="Production ranking",
        hypothesis="Personalized ranking improves completed-order conversion.",
        primary_metric="order_conversion_rate",
    )


def rollout_outcome() -> tuple[RolloutDecisionRecord, StateTransitionRecord]:
    created_at = utc_now()
    decision = RolloutDecisionRecord(
        decision_id="decision_atomic_test",
        experiment_id=request().experiment_id,
        allocation_version=1,
        previous_state=RolloutState.RUNNING_5,
        recommended_state=RolloutState.RUNNING_25,
        decision=RolloutDecision.RAMP,
        decision_reason="All production guardrails passed.",
        analysis_status=ExperimentAnalysisStatus.COMPLETE,
        analysis_snapshot_id="analysis_atomic_test",
        health_snapshot_id="health_atomic_test",
        policy_version="phase5-rollout-v1",
        policy={"statistical_significance_alpha": 0.05},
        guardrail_evidence=RolloutGuardrailEvidence(
            experiment_id=request().experiment_id,
            allocation_version=1,
            control_cancellation_rate=0.05,
            treatment_cancellation_rate=0.04,
        ),
        guardrail_checks=(),
        created_at=created_at,
    )
    transition = StateTransitionRecord(
        transition_id="transition_atomic_test",
        decision_id=decision.decision_id,
        experiment_id=decision.experiment_id,
        allocation_version=decision.allocation_version,
        previous_state=decision.previous_state,
        new_state=decision.recommended_state,
        decision=decision.decision,
        decision_reason=decision.decision_reason,
        analysis_snapshot_id=decision.analysis_snapshot_id,
        health_snapshot_id=decision.health_snapshot_id,
        policy_version=decision.policy_version,
        requested_by="rollout-controller",
        approved_by="release-owner",
        created_at=created_at,
    )
    return decision, transition


def test_postgres_store_persists_lifecycle_and_assignments(tmp_path: Path) -> None:
    repository = store(tmp_path)
    created = repository.create_experiment(request())
    launched = repository.launch_experiment(created.experiment_id)
    assignment_service = AssignmentService(repository)

    first = assignment_service.assign(created.experiment_id, "customer-42")
    second = assignment_service.assign(created.experiment_id, "customer-42")
    ramped = repository.apply_rollout_state(
        created.experiment_id,
        RolloutState.RUNNING_25,
        treatment_percentage=25,
        expected_state=RolloutState.RUNNING_5,
        expected_allocation_version=1,
    )

    assert launched.state == RolloutState.RUNNING_5
    assert first == second
    assert ramped.state == RolloutState.RUNNING_25
    assert ramped.treatment_percentage == 25
    assert ramped.allocation_version == 2
    assert repository.get_experiment(created.experiment_id) == ramped
    repository.ping()


def test_postgres_store_rejects_duplicates_and_stale_rollout_updates(tmp_path: Path) -> None:
    repository = store(tmp_path)
    repository.create_experiment(request())
    repository.launch_experiment(request().experiment_id)

    with pytest.raises(ExperimentAlreadyExistsError):
        repository.create_experiment(request())
    with pytest.raises(ConcurrentExperimentUpdateError, match="state changed"):
        repository.apply_rollout_state(
            request().experiment_id,
            RolloutState.RUNNING_25,
            treatment_percentage=25,
            expected_state=RolloutState.PAUSED,
            expected_allocation_version=1,
        )


def test_postgres_assignment_insert_is_idempotent_under_duplicate_identity(tmp_path: Path) -> None:
    repository = store(tmp_path)
    repository.create_experiment(request())
    repository.launch_experiment(request().experiment_id)
    assignment = Assignment(
        experiment_id=request().experiment_id,
        subject_id="customer-42",
        variant=Variant.CONTROL,
        bucket=9_999,
        allocation_version=1,
        assigned_at=utc_now(),
    )

    first = repository.save_assignment(assignment)
    second = repository.save_assignment(
        assignment.model_copy(update={"variant": Variant.TREATMENT, "bucket": 1})
    )

    assert second == first


def test_rollout_outcome_state_and_outbox_are_committed_atomically(tmp_path: Path) -> None:
    experiment_store = store(tmp_path)
    experiment_store.create_experiment(request())
    experiment_store.launch_experiment(request().experiment_id)
    repository = PostgresRolloutRepository(experiment_store.engine)
    decision, transition = rollout_outcome()

    persisted_decision, persisted_transition, updated = repository.persist_outcome_and_apply(
        decision,
        transition,
        treatment_percentage=25,
    )

    assert persisted_decision == decision
    assert persisted_transition == transition
    assert updated.state == RolloutState.RUNNING_25
    assert updated.allocation_version == 2
    assert repository.list_decisions(request().experiment_id) == (decision,)
    with experiment_store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(rollout_decisions)) == 1
        assert connection.scalar(select(func.count()).select_from(state_transitions)) == 1
        outbox = connection.execute(select(outbox_events)).mappings().one()
    assert outbox["event_type"] == "rollout.outcome-recorded.v1"
    assert outbox["payload"]["decision"]["decision_id"] == decision.decision_id


def test_atomic_rollout_is_idempotent_and_rejects_a_new_stale_decision(tmp_path: Path) -> None:
    experiment_store = store(tmp_path)
    experiment_store.create_experiment(request())
    experiment_store.launch_experiment(request().experiment_id)
    repository = PostgresRolloutRepository(experiment_store.engine)
    decision, transition = rollout_outcome()

    first = repository.persist_outcome_and_apply(decision, transition, treatment_percentage=25)
    duplicate = repository.persist_outcome_and_apply(decision, transition, treatment_percentage=25)
    stale_decision = decision.model_copy(update={"decision_id": "decision_stale_test"})
    stale_transition = transition.model_copy(
        update={
            "transition_id": "transition_stale_test",
            "decision_id": stale_decision.decision_id,
        }
    )

    assert duplicate == first
    with pytest.raises(ConcurrentExperimentUpdateError):
        repository.persist_outcome_and_apply(
            stale_decision,
            stale_transition,
            treatment_percentage=50,
        )
    with experiment_store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(rollout_decisions)) == 1
        assert connection.scalar(select(func.count()).select_from(state_transitions)) == 1
        assert connection.scalar(select(func.count()).select_from(outbox_events)) == 1


class RecordingOutboxPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.envelopes: list[OutboxEnvelope] = []

    def publish(self, envelope: OutboxEnvelope) -> None:
        self.envelopes.append(envelope)
        if self.fail:
            raise ConnectionError("provider credential must not be persisted")


def test_outbox_worker_marks_success_and_dead_letters_terminal_failure(tmp_path: Path) -> None:
    successful_store = store(tmp_path)
    successful_store.create_experiment(request())
    successful_store.launch_experiment(request().experiment_id)
    decision, transition = rollout_outcome()
    PostgresRolloutRepository(successful_store.engine).persist_outcome_and_apply(
        decision,
        transition,
        treatment_percentage=25,
    )
    publisher = RecordingOutboxPublisher()
    worker = PostgresOutboxWorker(
        successful_store.engine,
        publisher,
        RuntimeSettings(_env_file=None),
    )

    result = worker.run_once()

    assert result.processed == 1
    assert publisher.envelopes[0].payload["decision"]["decision_id"] == decision.decision_id
    with successful_store.engine.connect() as connection:
        successful_row = connection.execute(select(outbox_events)).mappings().one()
    assert successful_row["processed_at"] is not None
    assert successful_row["lock_token"] is None

    failure_engine = create_engine(f"sqlite:///{tmp_path / 'failure.db'}")
    metadata.create_all(failure_engine)
    failure_store = PostgresExperimentStore(engine=failure_engine)
    failure_store.create_experiment(request())
    failure_store.launch_experiment(request().experiment_id)
    PostgresRolloutRepository(failure_engine).persist_outcome_and_apply(
        decision,
        transition,
        treatment_percentage=25,
    )
    failed = PostgresOutboxWorker(
        failure_engine,
        RecordingOutboxPublisher(fail=True),
        RuntimeSettings(outbox_max_attempts=1, _env_file=None),
    ).run_once()

    assert failed.dead_lettered == 1
    with failure_engine.connect() as connection:
        failure_row = connection.execute(select(outbox_events)).mappings().one()
    assert failure_row["dead_lettered_at"] is not None
    assert "credential" not in failure_row["last_error"]
