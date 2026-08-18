from __future__ import annotations

import json
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from guardrail.domain.events import ProductEvent
from guardrail.domain.ingestion import EventPublisher
from guardrail.domain.replay import (
    InvalidReplayPayloadError,
    RejectedEvent,
    ReplayExecutionError,
    ReplayReceipt,
    ReplayRepository,
    ReplayRequest,
    ReplayStatus,
)


class ReplayService:
    def __init__(self, repository: ReplayRepository, publisher: EventPublisher) -> None:
        self._repository = repository
        self._publisher = publisher

    def get_rejection(self, rejection_id: str) -> RejectedEvent:
        return self._repository.get_rejection(rejection_id)

    def replay(self, rejection_id: str, request: ReplayRequest) -> ReplayReceipt:
        rejection = self._repository.get_rejection(rejection_id)
        try:
            event = ProductEvent.model_validate(request.corrected_event)
        except ValidationError as exc:
            raise InvalidReplayPayloadError(
                "Corrected payload does not satisfy the product-event envelope."
            ) from exc

        if rejection.event_id is not None and event.event_id != rejection.event_id:
            raise InvalidReplayPayloadError(
                "A corrected payload must preserve the original event_id."
            )
        if not self._repository.event_contract_exists(event.event_name.value, event.event_version):
            raise InvalidReplayPayloadError(
                "Corrected payload references an inactive or unsupported event contract."
            )

        corrected_payload = event.model_dump(mode="json")
        canonical_payload = json.dumps(
            corrected_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        corrected_hash = sha256(canonical_payload.encode("utf-8")).hexdigest()
        replay_id = str(
            uuid5(
                NAMESPACE_URL,
                f"guardrail:replay:{rejection_id}:{corrected_hash}",
            )
        )
        record = self._repository.enqueue(
            replay_id=replay_id,
            rejection=rejection,
            corrected_payload=corrected_payload,
            corrected_payload_hash=corrected_hash,
            requested_by=request.requested_by,
        )

        if record.status == ReplayStatus.COMPLETED:
            if record.replay_batch_id is None:
                raise ReplayExecutionError(
                    "Completed replay is missing its ingestion batch lineage."
                )
            return ReplayReceipt(
                replay_id=record.replay_id,
                rejection_id=record.rejection_id,
                status=record.status,
                attempt_count=record.attempt_count,
                ingestion=self._repository.get_ingestion_receipt(record.replay_batch_id),
            )

        processing = self._repository.mark_processing(record.replay_id)
        replay_payload = dict(corrected_payload)
        replay_payload["_guardrail_replay"] = {
            "replay_id": record.replay_id,
            "rejection_id": rejection.rejection_id,
            "original_batch_id": rejection.batch_id,
            "original_ingestion_id": rejection.ingestion_id,
            "attempt": processing.attempt_count,
        }

        try:
            ingestion = self._publisher.publish([replay_payload])
            if ingestion.validated_count != 1 or ingestion.rejected_count != 0:
                raise ReplayExecutionError(
                    "Corrected payload was not accepted into the validated layer."
                )
            completed = self._repository.mark_completed(record.replay_id, ingestion.batch_id)
        except Exception as exc:
            self._repository.mark_failed(record.replay_id, str(exc))
            if isinstance(exc, ReplayExecutionError):
                raise
            raise ReplayExecutionError("Corrected event replay failed.") from exc

        return ReplayReceipt(
            replay_id=completed.replay_id,
            rejection_id=completed.rejection_id,
            status=completed.status,
            attempt_count=completed.attempt_count,
            ingestion=ingestion,
        )
