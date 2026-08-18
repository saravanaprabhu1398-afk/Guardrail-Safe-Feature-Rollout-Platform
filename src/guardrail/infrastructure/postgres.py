from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from guardrail.config import RuntimeSettings
from guardrail.domain.analysis import ExperimentAnalysisStatus
from guardrail.domain.experiments import (
    Assignment,
    Experiment,
    ExperimentCreate,
    RolloutState,
    Variant,
    utc_now,
)
from guardrail.domain.rollout import (
    DecisionPersistenceError,
    GuardrailCheckResult,
    RolloutDecision,
    RolloutDecisionRecord,
    RolloutGuardrailEvidence,
    StateTransitionRecord,
)
from guardrail.services.store import (
    ConcurrentExperimentUpdateError,
    ExperimentAlreadyExistsError,
    ExperimentNotFoundError,
    InvalidExperimentStateError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)
json_type = JSON().with_variant(JSONB(), "postgresql")

experiments = Table(
    "experiments",
    metadata,
    Column("experiment_id", String(64), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("hypothesis", String(500), nullable=False),
    Column("primary_metric", String(120), nullable=False),
    Column("treatment_percentage", Integer, nullable=False),
    Column("allocation_version", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("lock_version", Integer, nullable=False, default=1),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "treatment_percentage BETWEEN 0 AND 100",
        name="treatment_percentage",
    ),
    CheckConstraint("allocation_version >= 1", name="allocation_version"),
    CheckConstraint("lock_version >= 1", name="lock_version"),
)

assignments = Table(
    "assignments",
    metadata,
    Column(
        "experiment_id",
        String(64),
        ForeignKey("experiments.experiment_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("subject_id", String(255), primary_key=True),
    Column("allocation_version", Integer, primary_key=True),
    Column("variant", String(16), nullable=False),
    Column("bucket", Integer, nullable=False),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("allocation_version >= 1", name="allocation_version"),
    CheckConstraint("bucket BETWEEN 0 AND 9999", name="bucket"),
)
Index(
    "ix_assignments_experiment_allocation",
    assignments.c.experiment_id,
    assignments.c.allocation_version,
)

rollout_decisions = Table(
    "rollout_decisions",
    metadata,
    Column("decision_id", String(80), primary_key=True),
    Column(
        "experiment_id",
        String(64),
        ForeignKey("experiments.experiment_id"),
        nullable=False,
    ),
    Column("allocation_version", Integer, nullable=False),
    Column("previous_state", String(32), nullable=False),
    Column("recommended_state", String(32), nullable=False),
    Column("decision", String(16), nullable=False),
    Column("decision_reason", Text, nullable=False),
    Column("analysis_status", String(32), nullable=False),
    Column("analysis_snapshot_id", String(80)),
    Column("health_snapshot_id", String(80)),
    Column("policy_version", String(80), nullable=False),
    Column("policy", json_type, nullable=False),
    Column("guardrail_evidence", json_type, nullable=False),
    Column("guardrail_checks", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("allocation_version >= 1", name="allocation_version"),
)
Index(
    "ix_rollout_decisions_experiment_created",
    rollout_decisions.c.experiment_id,
    rollout_decisions.c.created_at,
)

state_transitions = Table(
    "state_transitions",
    metadata,
    Column("transition_id", String(80), primary_key=True),
    Column(
        "decision_id",
        String(80),
        ForeignKey("rollout_decisions.decision_id"),
        nullable=False,
        unique=True,
    ),
    Column(
        "experiment_id",
        String(64),
        ForeignKey("experiments.experiment_id"),
        nullable=False,
    ),
    Column("allocation_version", Integer, nullable=False),
    Column("previous_state", String(32), nullable=False),
    Column("new_state", String(32), nullable=False),
    Column("decision", String(16), nullable=False),
    Column("decision_reason", Text, nullable=False),
    Column("analysis_snapshot_id", String(80)),
    Column("health_snapshot_id", String(80)),
    Column("policy_version", String(80), nullable=False),
    Column("requested_by", String(255), nullable=False),
    Column("approved_by", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("allocation_version >= 1", name="allocation_version"),
)
Index(
    "ix_state_transitions_experiment_created",
    state_transitions.c.experiment_id,
    state_transitions.c.created_at,
)

outbox_events = Table(
    "outbox_events",
    metadata,
    Column("event_id", String(96), primary_key=True),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", String(96), nullable=False),
    Column("event_type", String(96), nullable=False),
    Column("payload", json_type, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("processed_at", DateTime(timezone=True)),
    Column("locked_at", DateTime(timezone=True)),
    Column("lock_token", String(64)),
    Column("dead_lettered_at", DateTime(timezone=True)),
    Column("last_error", Text),
    CheckConstraint("attempt_count >= 0", name="attempt_count"),
)
Index("ix_outbox_events_pending", outbox_events.c.processed_at, outbox_events.c.available_at)


def create_postgres_engine(settings: RuntimeSettings) -> Engine:
    database_url = settings.database_url_value
    if database_url is None:
        raise ValueError("DATABASE_URL is required for PostgreSQL persistence.")
    arguments: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": 1_800,
    }
    if not database_url.startswith("sqlite"):
        arguments.update(
            {
                "pool_size": settings.database_pool_size,
                "max_overflow": settings.database_max_overflow,
            }
        )
    return create_engine(database_url, **arguments)


class PostgresExperimentStore:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None:
            engine = create_postgres_engine(settings or RuntimeSettings())
        self.engine = engine

    def create_experiment(self, request: ExperimentCreate) -> Experiment:
        now = utc_now()
        values = {
            "experiment_id": request.experiment_id,
            "name": request.name,
            "hypothesis": request.hypothesis,
            "primary_metric": request.primary_metric,
            "treatment_percentage": request.initial_treatment_percentage,
            "allocation_version": 1,
            "state": RolloutState.DRAFT.value,
            "lock_version": 1,
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(experiments).values(**values))
        except IntegrityError as exc:
            raise ExperimentAlreadyExistsError(request.experiment_id) from exc
        return Experiment(**{key: value for key, value in values.items() if key != "lock_version"})

    def get_experiment(self, experiment_id: str) -> Experiment:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(experiments).where(experiments.c.experiment_id == experiment_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ExperimentNotFoundError(experiment_id)
        return self._experiment(row)

    def launch_experiment(self, experiment_id: str) -> Experiment:
        with self.engine.begin() as connection:
            row = self._locked_experiment(connection, experiment_id)
            current = self._experiment(row)
            if current.state == RolloutState.RUNNING_5:
                return current
            if current.state != RolloutState.DRAFT:
                raise InvalidExperimentStateError(
                    f"Cannot launch an experiment from {current.state}."
                )
            now = utc_now()
            connection.execute(
                update(experiments)
                .where(experiments.c.experiment_id == experiment_id)
                .values(
                    state=RolloutState.RUNNING_5.value,
                    lock_version=experiments.c.lock_version + 1,
                    updated_at=now,
                )
            )
            launched = dict(row)
            launched.update(
                {
                    "state": RolloutState.RUNNING_5.value,
                    "lock_version": row["lock_version"] + 1,
                    "updated_at": now,
                }
            )
            return self._experiment(launched)

    def apply_rollout_state(
        self,
        experiment_id: str,
        state: RolloutState,
        *,
        treatment_percentage: int,
        expected_state: RolloutState | None = None,
        expected_allocation_version: int | None = None,
    ) -> Experiment:
        if not 0 <= treatment_percentage <= 100:
            raise ValueError("Treatment percentage must be between zero and 100.")
        with self.engine.begin() as connection:
            row = self._locked_experiment(connection, experiment_id)
            current = self._experiment(row)
            self._validate_expected(current, expected_state, expected_allocation_version)
            allocation_changed = treatment_percentage != current.treatment_percentage
            now = utc_now()
            next_allocation_version = current.allocation_version + int(allocation_changed)
            connection.execute(
                update(experiments)
                .where(experiments.c.experiment_id == experiment_id)
                .values(
                    state=state.value,
                    treatment_percentage=treatment_percentage,
                    allocation_version=next_allocation_version,
                    lock_version=experiments.c.lock_version + 1,
                    updated_at=now,
                )
            )
            return current.model_copy(
                update={
                    "state": state,
                    "treatment_percentage": treatment_percentage,
                    "allocation_version": next_allocation_version,
                    "updated_at": now,
                }
            )

    def get_assignment(
        self, experiment_id: str, subject_id: str, allocation_version: int
    ) -> Assignment | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(assignments).where(
                        assignments.c.experiment_id == experiment_id,
                        assignments.c.subject_id == subject_id,
                        assignments.c.allocation_version == allocation_version,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._assignment(row) if row is not None else None

    def save_assignment(self, assignment: Assignment) -> Assignment:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(assignments).values(
                        experiment_id=assignment.experiment_id,
                        subject_id=assignment.subject_id,
                        allocation_version=assignment.allocation_version,
                        variant=assignment.variant.value,
                        bucket=assignment.bucket,
                        assigned_at=assignment.assigned_at,
                    )
                )
            return assignment
        except IntegrityError:
            existing = self.get_assignment(
                assignment.experiment_id,
                assignment.subject_id,
                assignment.allocation_version,
            )
            if existing is None:
                raise
            return existing

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(select(experiments.c.experiment_id).limit(1))

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _locked_experiment(connection: Any, experiment_id: str) -> RowMapping:
        row = (
            connection.execute(
                select(experiments)
                .where(experiments.c.experiment_id == experiment_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ExperimentNotFoundError(experiment_id)
        return row

    @staticmethod
    def _validate_expected(
        experiment: Experiment,
        expected_state: RolloutState | None,
        expected_allocation_version: int | None,
    ) -> None:
        if expected_state is not None and experiment.state != expected_state:
            raise ConcurrentExperimentUpdateError(
                f"Experiment state changed from expected {expected_state}."
            )
        if (
            expected_allocation_version is not None
            and experiment.allocation_version != expected_allocation_version
        ):
            raise ConcurrentExperimentUpdateError(
                "Experiment allocation version changed during rollout evaluation."
            )

    @staticmethod
    def _experiment(row: RowMapping | dict[str, Any]) -> Experiment:
        return Experiment(
            experiment_id=row["experiment_id"],
            name=row["name"],
            hypothesis=row["hypothesis"],
            primary_metric=row["primary_metric"],
            treatment_percentage=row["treatment_percentage"],
            allocation_version=row["allocation_version"],
            state=RolloutState(row["state"]),
            created_at=_aware(row["created_at"]),
            updated_at=_aware(row["updated_at"]),
        )

    @staticmethod
    def _assignment(row: RowMapping) -> Assignment:
        return Assignment(
            experiment_id=row["experiment_id"],
            subject_id=row["subject_id"],
            allocation_version=row["allocation_version"],
            variant=Variant(row["variant"]),
            bucket=row["bucket"],
            assigned_at=_aware(row["assigned_at"]),
        )


class PostgresRolloutRepository:
    """Authoritative rollout journal and atomic state-transition boundary."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def persist_outcome(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord]:
        raise DecisionPersistenceError(
            "PostgreSQL rollout outcomes must be persisted with their state transition."
        )

    def persist_outcome_and_apply(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
        *,
        treatment_percentage: int,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord, Experiment]:
        if not 0 <= treatment_percentage <= 100:
            raise ValueError("Treatment percentage must be between zero and 100.")
        self._validate_pair(decision, transition)
        try:
            with self.engine.begin() as connection:
                experiment_row = PostgresExperimentStore._locked_experiment(
                    connection, decision.experiment_id
                )
                current = PostgresExperimentStore._experiment(experiment_row)
                existing = self._existing_outcome(connection, decision.decision_id)
                if existing is not None:
                    persisted_decision, persisted_transition = existing
                    return persisted_decision, persisted_transition, current

                PostgresExperimentStore._validate_expected(
                    current,
                    decision.previous_state,
                    decision.allocation_version,
                )
                connection.execute(
                    insert(rollout_decisions).values(**self._decision_values(decision))
                )
                connection.execute(
                    insert(state_transitions).values(**self._transition_values(transition))
                )

                allocation_changed = treatment_percentage != current.treatment_percentage
                next_allocation_version = current.allocation_version + int(allocation_changed)
                now = utc_now()
                connection.execute(
                    update(experiments)
                    .where(experiments.c.experiment_id == current.experiment_id)
                    .values(
                        state=transition.new_state.value,
                        treatment_percentage=treatment_percentage,
                        allocation_version=next_allocation_version,
                        lock_version=experiments.c.lock_version + 1,
                        updated_at=now,
                    )
                )
                connection.execute(
                    insert(outbox_events).values(
                        event_id=f"outbox_{decision.decision_id}",
                        aggregate_type="rollout_decision",
                        aggregate_id=decision.decision_id,
                        event_type="rollout.outcome-recorded.v1",
                        payload={
                            "decision": decision.model_dump(mode="json"),
                            "transition": transition.model_dump(mode="json"),
                        },
                        occurred_at=decision.created_at,
                        available_at=decision.created_at,
                        attempt_count=0,
                    )
                )
                updated_experiment = current.model_copy(
                    update={
                        "state": transition.new_state,
                        "treatment_percentage": treatment_percentage,
                        "allocation_version": next_allocation_version,
                        "updated_at": now,
                    }
                )
                return decision, transition, updated_experiment
        except (ConcurrentExperimentUpdateError, ExperimentNotFoundError):
            raise
        except SQLAlchemyError as exc:
            raise DecisionPersistenceError(
                "The operational rollout outcome could not be persisted."
            ) from exc

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]:
        try:
            with self.engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(rollout_decisions)
                        .where(rollout_decisions.c.experiment_id == experiment_id)
                        .order_by(
                            rollout_decisions.c.created_at.desc(),
                            rollout_decisions.c.decision_id.desc(),
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise DecisionPersistenceError(
                "The operational rollout decision history is unavailable."
            ) from exc
        return tuple(self._decision(row) for row in rows)

    def _existing_outcome(
        self, connection: Any, decision_id: str
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord] | None:
        decision_row = (
            connection.execute(
                select(rollout_decisions).where(rollout_decisions.c.decision_id == decision_id)
            )
            .mappings()
            .one_or_none()
        )
        if decision_row is None:
            return None
        transition_row = (
            connection.execute(
                select(state_transitions).where(state_transitions.c.decision_id == decision_id)
            )
            .mappings()
            .one_or_none()
        )
        if transition_row is None:
            raise DecisionPersistenceError(
                "An operational decision exists without its state transition."
            )
        return self._decision(decision_row), self._transition(transition_row)

    @staticmethod
    def _validate_pair(
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
    ) -> None:
        if (
            transition.decision_id != decision.decision_id
            or transition.experiment_id != decision.experiment_id
            or transition.allocation_version != decision.allocation_version
            or transition.previous_state != decision.previous_state
            or transition.new_state != decision.recommended_state
            or transition.decision != decision.decision
        ):
            raise ValueError("Rollout decision and transition do not describe one outcome.")

    @staticmethod
    def _decision_values(decision: RolloutDecisionRecord) -> dict[str, Any]:
        return {
            "decision_id": decision.decision_id,
            "experiment_id": decision.experiment_id,
            "allocation_version": decision.allocation_version,
            "previous_state": decision.previous_state.value,
            "recommended_state": decision.recommended_state.value,
            "decision": decision.decision.value,
            "decision_reason": decision.decision_reason,
            "analysis_status": decision.analysis_status.value,
            "analysis_snapshot_id": decision.analysis_snapshot_id,
            "health_snapshot_id": decision.health_snapshot_id,
            "policy_version": decision.policy_version,
            "policy": decision.policy,
            "guardrail_evidence": decision.guardrail_evidence.model_dump(mode="json"),
            "guardrail_checks": [
                check.model_dump(mode="json") for check in decision.guardrail_checks
            ],
            "created_at": decision.created_at,
        }

    @staticmethod
    def _transition_values(transition: StateTransitionRecord) -> dict[str, Any]:
        return {
            "transition_id": transition.transition_id,
            "decision_id": transition.decision_id,
            "experiment_id": transition.experiment_id,
            "allocation_version": transition.allocation_version,
            "previous_state": transition.previous_state.value,
            "new_state": transition.new_state.value,
            "decision": transition.decision.value,
            "decision_reason": transition.decision_reason,
            "analysis_snapshot_id": transition.analysis_snapshot_id,
            "health_snapshot_id": transition.health_snapshot_id,
            "policy_version": transition.policy_version,
            "requested_by": transition.requested_by,
            "approved_by": transition.approved_by,
            "created_at": transition.created_at,
        }

    @staticmethod
    def _decision(row: RowMapping) -> RolloutDecisionRecord:
        return RolloutDecisionRecord(
            decision_id=row["decision_id"],
            experiment_id=row["experiment_id"],
            allocation_version=row["allocation_version"],
            previous_state=RolloutState(row["previous_state"]),
            recommended_state=RolloutState(row["recommended_state"]),
            decision=RolloutDecision(row["decision"]),
            decision_reason=row["decision_reason"],
            analysis_status=ExperimentAnalysisStatus(row["analysis_status"]),
            analysis_snapshot_id=row["analysis_snapshot_id"],
            health_snapshot_id=row["health_snapshot_id"],
            policy_version=row["policy_version"],
            policy=row["policy"],
            guardrail_evidence=RolloutGuardrailEvidence.model_validate(row["guardrail_evidence"]),
            guardrail_checks=tuple(
                GuardrailCheckResult.model_validate(check) for check in row["guardrail_checks"]
            ),
            created_at=_aware(row["created_at"]),
        )

    @staticmethod
    def _transition(row: RowMapping) -> StateTransitionRecord:
        return StateTransitionRecord(
            transition_id=row["transition_id"],
            decision_id=row["decision_id"],
            experiment_id=row["experiment_id"],
            allocation_version=row["allocation_version"],
            previous_state=RolloutState(row["previous_state"]),
            new_state=RolloutState(row["new_state"]),
            decision=RolloutDecision(row["decision"]),
            decision_reason=row["decision_reason"],
            analysis_snapshot_id=row["analysis_snapshot_id"],
            health_snapshot_id=row["health_snapshot_id"],
            policy_version=row["policy_version"],
            requested_by=row["requested_by"],
            approved_by=row["approved_by"],
            created_at=_aware(row["created_at"]),
        )


def _aware(value: Any) -> Any:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def run_migrations() -> None:
    settings = RuntimeSettings()
    if settings.database_url_value is None:
        raise SystemExit("DATABASE_URL is required to run migrations.")
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    configuration.attributes["connection"] = create_postgres_engine(settings)
    command.upgrade(configuration, "head")
