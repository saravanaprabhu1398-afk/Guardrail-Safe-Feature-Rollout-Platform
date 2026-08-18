from __future__ import annotations

import hashlib

from guardrail.domain.experiments import Assignment, RolloutState, Variant, utc_now
from guardrail.services.store import InMemoryExperimentStore, InvalidExperimentStateError


class AssignmentService:
    ACTIVE_STATES = {
        RolloutState.RUNNING_5,
        RolloutState.RUNNING_25,
        RolloutState.RUNNING_50,
        RolloutState.RELEASED,
    }

    def __init__(self, store: InMemoryExperimentStore) -> None:
        self._store = store

    @staticmethod
    def stable_bucket(experiment_id: str, subject_id: str) -> int:
        identity = f"{experiment_id}:{subject_id}".encode()
        digest = hashlib.sha256(identity).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=False) % 10_000

    def assign(self, experiment_id: str, subject_id: str) -> Assignment:
        experiment = self._store.get_experiment(experiment_id)
        if experiment.state not in self.ACTIVE_STATES:
            message = (
                "Experiment must be running before assignment; "
                f"current state is {experiment.state}."
            )
            raise InvalidExperimentStateError(message)

        cached = self._store.get_assignment(
            experiment_id, subject_id, experiment.allocation_version
        )
        if cached is not None:
            return cached

        bucket = self.stable_bucket(experiment_id, subject_id)
        treatment_threshold = experiment.treatment_percentage * 100
        assignment = Assignment(
            experiment_id=experiment_id,
            subject_id=subject_id,
            variant=(Variant.TREATMENT if bucket < treatment_threshold else Variant.CONTROL),
            bucket=bucket,
            allocation_version=experiment.allocation_version,
            assigned_at=utc_now(),
        )
        return self._store.save_assignment(assignment)
