from datetime import UTC, datetime

from guardrail.domain.events import EventName, FaultProfile
from guardrail.domain.experiments import Assignment, Variant
from guardrail.services.simulator import MarketplaceSimulator


def assignment() -> Assignment:
    return Assignment(
        experiment_id="ranking_test",
        subject_id="user_123",
        variant=Variant.TREATMENT,
        bucket=123,
        allocation_version=1,
        assigned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_journey_starts_with_assignment_and_exposure() -> None:
    simulator = MarketplaceSimulator()

    events = simulator.generate_journey(
        assignment(),
        seed=42,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        fault_profile=FaultProfile(),
    )

    assert events[0].event_name == EventName.EXPERIMENT_ASSIGNED
    assert events[1].event_name == EventName.EXPERIMENT_EXPOSED
    assert all(event.experiment_id == "ranking_test" for event in events)
    assert all(event.event_timestamp <= event.received_timestamp for event in events)


def test_full_fault_profile_creates_duplicates_lateness_and_flicker() -> None:
    simulator = MarketplaceSimulator()

    events = simulator.generate_journey(
        assignment(),
        seed=42,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        fault_profile=FaultProfile(
            duplicate_rate=0.5,
            late_event_rate=0.5,
            flicker_rate=0.2,
        ),
    )
    summary = simulator.summarize("ranking_test", [assignment()], events)

    assert summary.duplicate_events > 0
    assert summary.late_events > 0
