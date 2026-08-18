from __future__ import annotations

from threading import RLock

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
