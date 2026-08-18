from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from guardrail.config import RuntimeSettings
from guardrail.domain.experiments import ExperimentCreate, RolloutState
from guardrail.infrastructure.postgres import PostgresExperimentStore
from guardrail.services.assignment import AssignmentService

DATABASE_URL = os.getenv("GUARDRAIL_POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="GUARDRAIL_POSTGRES_TEST_URL is required for PostgreSQL integration tests",
)


def test_real_postgres_store_supports_concurrent_idempotent_assignment() -> None:
    assert DATABASE_URL is not None
    store = PostgresExperimentStore(
        RuntimeSettings(
            guardrail_store_backend="postgres",
            database_url=DATABASE_URL,
            _env_file=None,
        )
    )
    experiment_id = f"postgres_{uuid4().hex[:16]}"
    store.create_experiment(
        ExperimentCreate(
            experiment_id=experiment_id,
            name="PostgreSQL integration",
            hypothesis="Concurrent callers receive one durable assignment identity.",
            primary_metric="order_conversion_rate",
        )
    )
    launched = store.launch_experiment(experiment_id)
    service = AssignmentService(store)

    with ThreadPoolExecutor(max_workers=8) as executor:
        assignments = tuple(
            executor.map(lambda _: service.assign(experiment_id, "customer-42"), range(24))
        )

    assert launched.state == RolloutState.RUNNING_5
    assert all(assignment == assignments[0] for assignment in assignments)
    store.close()
