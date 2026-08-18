from __future__ import annotations

from threading import RLock
from typing import Protocol

from guardrail.domain.experiments import (
    Assignment,
    Experiment,
    ExperimentCreate,
    RolloutState,
    utc_now,
)


class ExperimentNotFoundError(KeyError):
    pass


class ExperimentAlreadyExistsError(ValueError):
    pass


class InvalidExperimentStateError(ValueError):
    pass


class ConcurrentExperimentUpdateError(RuntimeError):
    pass


class ExperimentStore(Protocol):
    def create_experiment(self, request: ExperimentCreate) -> Experiment: ...

    def get_experiment(self, experiment_id: str) -> Experiment: ...

    def launch_experiment(self, experiment_id: str) -> Experiment: ...

    def apply_rollout_state(
        self,
        experiment_id: str,
        state: RolloutState,
        *,
        treatment_percentage: int,
        expected_state: RolloutState | None = None,
        expected_allocation_version: int | None = None,
    ) -> Experiment: ...

    def get_assignment(
        self, experiment_id: str, subject_id: str, allocation_version: int
    ) -> Assignment | None: ...

    def save_assignment(self, assignment: Assignment) -> Assignment: ...

    def ping(self) -> None: ...


class InMemoryExperimentStore:
    """Thread-safe development store.

    This store makes Phase 1 runnable without external infrastructure. It will be
    replaced behind the same service boundary when persistent configuration is added.
    """

    def __init__(self) -> None:
        self._experiments: dict[str, Experiment] = {}
        self._assignments: dict[tuple[str, str, int], Assignment] = {}
        self._lock = RLock()

    def create_experiment(self, request: ExperimentCreate) -> Experiment:
        with self._lock:
            if request.experiment_id in self._experiments:
                raise ExperimentAlreadyExistsError(request.experiment_id)

            now = utc_now()
            experiment = Experiment(
                experiment_id=request.experiment_id,
                name=request.name,
                hypothesis=request.hypothesis,
                primary_metric=request.primary_metric,
                treatment_percentage=request.initial_treatment_percentage,
                allocation_version=1,
                state=RolloutState.DRAFT,
                created_at=now,
                updated_at=now,
            )
            self._experiments[experiment.experiment_id] = experiment
            return experiment

    def get_experiment(self, experiment_id: str) -> Experiment:
        with self._lock:
            try:
                return self._experiments[experiment_id]
            except KeyError as exc:
                raise ExperimentNotFoundError(experiment_id) from exc

    def launch_experiment(self, experiment_id: str) -> Experiment:
        with self._lock:
            experiment = self.get_experiment(experiment_id)
            if experiment.state == RolloutState.RUNNING_5:
                return experiment
            if experiment.state != RolloutState.DRAFT:
                raise InvalidExperimentStateError(
                    f"Cannot launch an experiment from {experiment.state}."
                )

            launched = experiment.model_copy(
                update={"state": RolloutState.RUNNING_5, "updated_at": utc_now()}
            )
            self._experiments[experiment_id] = launched
            return launched

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
        with self._lock:
            experiment = self.get_experiment(experiment_id)
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
            allocation_changed = treatment_percentage != experiment.treatment_percentage
            updated = experiment.model_copy(
                update={
                    "state": state,
                    "treatment_percentage": treatment_percentage,
                    "allocation_version": (
                        experiment.allocation_version + 1
                        if allocation_changed
                        else experiment.allocation_version
                    ),
                    "updated_at": utc_now(),
                }
            )
            self._experiments[experiment_id] = updated
            return updated

    def get_assignment(
        self, experiment_id: str, subject_id: str, allocation_version: int
    ) -> Assignment | None:
        with self._lock:
            return self._assignments.get((experiment_id, subject_id, allocation_version))

    def save_assignment(self, assignment: Assignment) -> Assignment:
        key = (
            assignment.experiment_id,
            assignment.subject_id,
            assignment.allocation_version,
        )
        with self._lock:
            return self._assignments.setdefault(key, assignment)

    def ping(self) -> None:
        return None
