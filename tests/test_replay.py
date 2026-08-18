from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from guardrail.domain.ingestion import EventPublishingError, IngestionReceipt
from guardrail.domain.replay import (
    InvalidReplayPayloadError,
    RejectedEvent,
    ReplayExecutionError,
    ReplayRecord,
    ReplayRequest,
    ReplayStatus,
)
from guardrail.services.replay import ReplayService

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def corrected_event(event_id: str = "corrected_event_1") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_name": "experiment_exposed",
        "event_version": 1,
        "user_id": "user_1",
        "anonymous_id": "anon_1",
        "session_id": "session_1",
        "experiment_id": "ranking_test",
        "variant_id": "treatment",
        "event_timestamp": "2026-01-01T00:00:00Z",
        "sent_timestamp": "2026-01-01T00:00:00Z",
        "received_timestamp": "2026-01-01T00:00:01Z",
        "source": "web",
        "attributes": {"surface": "restaurant_feed"},
    }


def rejection(event_id: str | None = None) -> RejectedEvent:
    return RejectedEvent(
        rejection_id="rejection_1",
        ingestion_id="ingestion_1",
        batch_id="original_batch",
        event_id=event_id,
        payload={"event_name": "unsupported"},
        reason_code="MISSING_EVENT_ID",
        reason_details=None,
        rejected_at=NOW,
        replay_status="NOT_REQUESTED",
    )


def ingestion_receipt() -> IngestionReceipt:
    return IngestionReceipt(
        batch_id="replay_batch",
        channel_id="test-channel",
        records_submitted=1,
        first_offset=10,
        last_offset=10,
        raw_count=1,
        validated_count=1,
        rejected_count=0,
        duplicate_count=0,
        is_balanced=True,
        committed_at=NOW,
    )


class FakeReplayRepository:
    def __init__(self, rejected_event: RejectedEvent) -> None:
        self.rejected_event = rejected_event
        self.record: ReplayRecord | None = None
        self.failed_message: str | None = None

    def get_rejection(self, rejection_id: str) -> RejectedEvent:
        assert rejection_id == self.rejected_event.rejection_id
        return self.rejected_event

    def event_contract_exists(self, event_name: str, event_version: int) -> bool:
        return event_name == "experiment_exposed" and event_version == 1

    def enqueue(self, **kwargs: Any) -> ReplayRecord:
        if self.record is not None:
            return self.record
        self.record = ReplayRecord(
            replay_id=kwargs["replay_id"],
            rejection_id=kwargs["rejection"].rejection_id,
            corrected_payload_hash=kwargs["corrected_payload_hash"],
            replay_batch_id=None,
            status=ReplayStatus.PENDING,
            attempt_count=0,
            requested_by=kwargs["requested_by"],
            requested_at=NOW,
            last_attempt_at=None,
            completed_at=None,
            error_message=None,
        )
        return self.record

    def mark_processing(self, replay_id: str) -> ReplayRecord:
        assert self.record is not None and self.record.replay_id == replay_id
        self.record = self.record.model_copy(
            update={
                "status": ReplayStatus.PROCESSING,
                "attempt_count": self.record.attempt_count + 1,
                "last_attempt_at": NOW,
            }
        )
        return self.record

    def mark_completed(self, replay_id: str, batch_id: str) -> ReplayRecord:
        assert self.record is not None and self.record.replay_id == replay_id
        self.record = self.record.model_copy(
            update={
                "status": ReplayStatus.COMPLETED,
                "replay_batch_id": batch_id,
                "completed_at": NOW,
            }
        )
        return self.record

    def mark_failed(self, replay_id: str, error_message: str) -> ReplayRecord:
        assert self.record is not None and self.record.replay_id == replay_id
        self.failed_message = error_message
        self.record = self.record.model_copy(
            update={"status": ReplayStatus.FAILED, "error_message": error_message}
        )
        return self.record

    def get_ingestion_receipt(self, batch_id: str) -> IngestionReceipt:
        assert batch_id == "replay_batch"
        return ingestion_receipt()


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list[object]] = []

    def publish(self, events: list[object]) -> IngestionReceipt:
        self.batches.append(events)
        if self.fail:
            raise EventPublishingError("stream failed")
        return ingestion_receipt()


def test_replay_preserves_lineage_and_completes() -> None:
    repository = FakeReplayRepository(rejection())
    publisher = RecordingPublisher()
    service = ReplayService(repository, publisher)

    receipt = service.replay("rejection_1", ReplayRequest(corrected_event=corrected_event()))

    assert receipt.status == ReplayStatus.COMPLETED
    assert receipt.attempt_count == 1
    assert receipt.ingestion.is_balanced is True
    replay_payload = publisher.batches[0][0]
    assert isinstance(replay_payload, dict)
    assert replay_payload["_guardrail_replay"]["rejection_id"] == "rejection_1"
    assert replay_payload["_guardrail_replay"]["original_batch_id"] == "original_batch"


def test_completed_replay_is_idempotent() -> None:
    repository = FakeReplayRepository(rejection())
    publisher = RecordingPublisher()
    service = ReplayService(repository, publisher)
    request = ReplayRequest(corrected_event=corrected_event())

    first = service.replay("rejection_1", request)
    second = service.replay("rejection_1", request)

    assert second.replay_id == first.replay_id
    assert second.ingestion.batch_id == first.ingestion.batch_id
    assert len(publisher.batches) == 1


def test_replay_preserves_existing_event_identity() -> None:
    repository = FakeReplayRepository(rejection(event_id="original_event"))
    service = ReplayService(repository, RecordingPublisher())

    with pytest.raises(InvalidReplayPayloadError, match="preserve the original"):
        service.replay(
            "rejection_1",
            ReplayRequest(corrected_event=corrected_event("different_event")),
        )

    assert repository.record is None


def test_failed_publish_marks_replay_failed() -> None:
    repository = FakeReplayRepository(rejection())
    service = ReplayService(repository, RecordingPublisher(fail=True))

    with pytest.raises(ReplayExecutionError, match="Corrected event replay failed"):
        service.replay("rejection_1", ReplayRequest(corrected_event=corrected_event()))

    assert repository.record is not None
    assert repository.record.status == ReplayStatus.FAILED
    assert repository.failed_message == "stream failed"
