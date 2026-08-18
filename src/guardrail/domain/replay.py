from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from guardrail.domain.ingestion import IngestionReceipt


class ReplayStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RejectedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    rejection_id: str
    ingestion_id: str | None
    batch_id: str
    event_id: str | None
    payload: dict[str, Any]
    reason_code: str
    reason_details: dict[str, Any] | None
    rejected_at: datetime
    replay_status: str


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrected_event: dict[str, Any]
    requested_by: str = Field(
        default="local_developer",
        min_length=2,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.@-]+$",
    )


class ReplayRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    replay_id: str
    rejection_id: str
    corrected_payload_hash: str
    replay_batch_id: str | None
    status: ReplayStatus
    attempt_count: int = Field(ge=0)
    requested_by: str
    requested_at: datetime
    last_attempt_at: datetime | None
    completed_at: datetime | None
    error_message: str | None


class ReplayReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    replay_id: str
    rejection_id: str
    status: ReplayStatus
    attempt_count: int = Field(ge=1)
    ingestion: IngestionReceipt


class RejectionNotFoundError(KeyError):
    pass


class ReplayConflictError(ValueError):
    pass


class InvalidReplayPayloadError(ValueError):
    pass


class ReplayExecutionError(RuntimeError):
    pass


class ReplayRepository(Protocol):
    def get_rejection(self, rejection_id: str) -> RejectedEvent: ...

    def event_contract_exists(self, event_name: str, event_version: int) -> bool: ...

    def enqueue(
        self,
        *,
        replay_id: str,
        rejection: RejectedEvent,
        corrected_payload: dict[str, Any],
        corrected_payload_hash: str,
        requested_by: str,
    ) -> ReplayRecord: ...

    def mark_processing(self, replay_id: str) -> ReplayRecord: ...

    def mark_completed(self, replay_id: str, batch_id: str) -> ReplayRecord: ...

    def mark_failed(self, replay_id: str, error_message: str) -> ReplayRecord: ...

    def get_ingestion_receipt(self, batch_id: str) -> IngestionReceipt: ...


class ReplayWorkflow(Protocol):
    def get_rejection(self, rejection_id: str) -> RejectedEvent: ...

    def replay(self, rejection_id: str, request: ReplayRequest) -> ReplayReceipt: ...
