from guardrail.domain.experiments import ExperimentCreate, Variant
from guardrail.services.assignment import AssignmentService
from guardrail.services.store import InMemoryExperimentStore


def running_service() -> AssignmentService:
    store = InMemoryExperimentStore()
    store.create_experiment(
        ExperimentCreate(
            experiment_id="ranking_test",
            name="Restaurant ranking test",
            hypothesis="Personalized ranking increases completed orders.",
            primary_metric="order_conversion_rate",
        )
    )
    store.launch_experiment("ranking_test")
    return AssignmentService(store)


def test_assignment_is_stable_for_same_subject() -> None:
    service = running_service()

    first = service.assign("ranking_test", "user_123")
    second = service.assign("ranking_test", "user_123")

    assert first == second
    assert first.bucket == service.stable_bucket("ranking_test", "user_123")


def test_assignment_distribution_matches_five_percent_rollout() -> None:
    service = running_service()

    assignments = [service.assign("ranking_test", f"user_{index}") for index in range(20_000)]
    treatment_rate = sum(item.variant == Variant.TREATMENT for item in assignments) / len(
        assignments
    )

    assert 0.04 <= treatment_rate <= 0.06
