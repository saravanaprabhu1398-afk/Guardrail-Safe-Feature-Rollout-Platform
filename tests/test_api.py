from datetime import UTC, datetime

from fastapi.testclient import TestClient

from guardrail.domain.ingestion import EventPublishingError, IngestionReceipt
from guardrail.domain.integrity import ExperimentIntegrityEvidence, IntegrityEvidenceError
from guardrail.domain.replay import (
    RejectedEvent,
    ReplayReceipt,
    ReplayRequest,
    ReplayStatus,
)
from guardrail.main import create_app

EXPERIMENT = {
    "experiment_id": "new_ranking_algorithm",
    "name": "Personalized restaurant ranking",
    "hypothesis": "Personalized ranking increases completed orders per exposed user.",
    "primary_metric": "order_conversion_rate",
}


class RecordingPublisher:
    def __init__(self) -> None:
        self.batches: list[list[object]] = []
        self.closed = False

    def publish(self, events: list[object]) -> IngestionReceipt:
        self.batches.append(events)
        return IngestionReceipt(
            batch_id=f"batch_{len(self.batches)}",
            channel_id="test-channel",
            records_submitted=len(events),
            first_offset=0,
            last_offset=len(events) - 1,
            raw_count=len(events),
            validated_count=len(events),
            rejected_count=0,
            duplicate_count=0,
            is_balanced=True,
            committed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def close(self) -> None:
        self.closed = True


class FailingPublisher:
    def publish(self, events: list[object]) -> IngestionReceipt:
        raise EventPublishingError("provider detail that must not reach the client")


class HealthyIntegrityRepository:
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentIntegrityEvidence:
        return ExperimentIntegrityEvidence(
            experiment_id=experiment_id,
            allocation_version=allocation_version,
            control_assignments=950,
            treatment_assignments=50,
            control_exposed_users=950,
            treatment_exposed_users=50,
            total_exposures=1_000,
            variant_flicker_users=0,
            assignment_variant_flicker_users=0,
            orphan_exposures=0,
            variant_mismatch_exposures=0,
            assignments_without_exposure=0,
        )


class FailingIntegrityRepository:
    def get_evidence(
        self, experiment_id: str, allocation_version: int
    ) -> ExperimentIntegrityEvidence:
        raise IntegrityEvidenceError("provider details")


class RecordingReplayWorkflow:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ReplayRequest]] = []

    def get_rejection(self, rejection_id: str) -> RejectedEvent:
        return RejectedEvent(
            rejection_id=rejection_id,
            ingestion_id="ingestion_1",
            batch_id="batch_1",
            event_id=None,
            payload={"event_name": "unsupported"},
            reason_code="MISSING_EVENT_ID",
            reason_details=None,
            rejected_at=datetime(2026, 1, 1, tzinfo=UTC),
            replay_status="NOT_REQUESTED",
        )

    def replay(self, rejection_id: str, request: ReplayRequest) -> ReplayReceipt:
        self.requests.append((rejection_id, request))
        ingestion = RecordingPublisher().publish([request.corrected_event])
        return ReplayReceipt(
            replay_id="replay_1",
            rejection_id=rejection_id,
            status=ReplayStatus.COMPLETED,
            attempt_count=1,
            ingestion=ingestion,
        )


def test_experiment_lifecycle_and_stable_assignment() -> None:
    with TestClient(create_app()) as client:
        created = client.post("/experiments", json=EXPERIMENT)
        assert created.status_code == 201
        assert created.json()["state"] == "DRAFT"

        before_launch = client.get("/assignments/new_ranking_algorithm/customer_42")
        assert before_launch.status_code == 409

        launched = client.post("/experiments/new_ranking_algorithm/launch")
        assert launched.status_code == 200
        assert launched.json()["state"] == "RUNNING_5"

        first = client.get("/assignments/new_ranking_algorithm/customer_42")
        second = client.get("/assignments/new_ranking_algorithm/customer_42")
        assert first.status_code == 200
        assert first.json() == second.json()


def test_duplicate_experiment_returns_conflict() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/experiments", json=EXPERIMENT).status_code == 201
        duplicate = client.post("/experiments", json=EXPERIMENT)

        assert duplicate.status_code == 409


def test_experiment_health_endpoint_returns_integrity_results() -> None:
    with TestClient(create_app(integrity_repository=HealthyIntegrityRepository())) as client:
        client.post("/experiments", json=EXPERIMENT)
        response = client.get("/experiments/new_ranking_algorithm/health")

    assert response.status_code == 200
    result = response.json()
    assert result["analysis_blocked"] is False
    assert [check["check_name"] for check in result["checks"]] == [
        "sample_ratio_mismatch",
        "variant_flicker",
        "assignment_exposure_consistency",
    ]


def test_experiment_health_endpoint_masks_evidence_provider_failure() -> None:
    with TestClient(create_app(integrity_repository=FailingIntegrityRepository())) as client:
        client.post("/experiments", json=EXPERIMENT)
        response = client.get("/experiments/new_ranking_algorithm/health")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Experiment integrity evidence is temporarily unavailable."
    }


def test_simulation_reports_injected_faults() -> None:
    with TestClient(create_app()) as client:
        client.post("/experiments", json=EXPERIMENT)
        client.post("/experiments/new_ranking_algorithm/launch")

        response = client.post(
            "/simulations",
            json={
                "experiment_id": "new_ranking_algorithm",
                "subjects": 100,
                "seed": 7,
                "fault_profile": {
                    "duplicate_rate": 0.2,
                    "late_event_rate": 0.2,
                    "flicker_rate": 0.1,
                },
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["subjects"] == 100
        assert result["events_generated"] > 100
        assert result["duplicate_events"] > 0
        assert result["late_events"] > 0
        assert result["flicker_subjects"] > 0


def test_event_batch_is_published_with_reconciliation_receipt() -> None:
    publisher = RecordingPublisher()
    event = {
        "event_id": "event_1",
        "event_name": "experiment_exposed",
        "event_version": 1,
    }

    with TestClient(create_app(event_publisher=publisher)) as client:
        response = client.post("/events", json={"events": [event]})

    assert response.status_code == 202
    assert response.json()["batch_id"] == "batch_1"
    assert response.json()["transport"] == "snowpipe"
    assert response.json()["ingestion"]["is_balanced"] is True
    assert publisher.batches == [[event]]
    assert publisher.closed is True


def test_simulation_can_persist_all_generated_events() -> None:
    publisher = RecordingPublisher()

    with TestClient(create_app(event_publisher=publisher)) as client:
        client.post("/experiments", json=EXPERIMENT)
        client.post("/experiments/new_ranking_algorithm/launch")
        response = client.post(
            "/simulations",
            json={
                "experiment_id": "new_ranking_algorithm",
                "subjects": 5,
                "seed": 7,
                "persist_events": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["ingestion_batch_id"] == "batch_1"
    assert response.json()["telemetry_batch_id"] == "batch_1"
    assert response.json()["telemetry_transport"] == "snowpipe"
    assert len(publisher.batches) == 1
    assert len(publisher.batches[0]) == response.json()["events_generated"]


def test_event_batch_rejects_non_object_payloads() -> None:
    with TestClient(create_app(event_publisher=RecordingPublisher())) as client:
        response = client.post("/events", json={"events": ["not-an-object"]})

    assert response.status_code == 422


def test_ingestion_provider_failures_return_safe_service_error() -> None:
    with TestClient(create_app(event_publisher=FailingPublisher())) as client:
        response = client.post("/events", json={"events": [{"event_id": "event_1"}]})

    assert response.status_code == 503
    assert response.json() == {"detail": "Event transport is temporarily unavailable."}


def test_rejection_lookup_and_replay_endpoints() -> None:
    workflow = RecordingReplayWorkflow()
    corrected = {
        "event_id": "corrected_1",
        "event_name": "experiment_exposed",
    }

    with TestClient(create_app(replay_workflow=workflow)) as client:
        lookup = client.get("/rejections/rejection_1")
        replay = client.post(
            "/rejections/rejection_1/replay",
            json={"corrected_event": corrected, "requested_by": "test_user"},
        )

    assert lookup.status_code == 200
    assert lookup.json()["reason_code"] == "MISSING_EVENT_ID"
    assert replay.status_code == 200
    assert replay.json()["status"] == "COMPLETED"
    assert workflow.requests[0][0] == "rejection_1"
    assert workflow.requests[0][1].requested_by == "test_user"
