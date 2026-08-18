from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from guardrail.domain.events import ProductEvent

EventPayload = ProductEvent | dict[str, object]


class EventPublishingError(RuntimeError):
    """The external ingestion system did not complete a requested event batch."""


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, object]] = Field(min_length=1, max_length=1_000)


class IngestionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    channel_id: str
    records_submitted: int = Field(ge=1)
    first_offset: int = Field(ge=0)
    last_offset: int = Field(ge=0)
    raw_count: int = Field(ge=0)
    validated_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    is_balanced: bool
    committed_at: datetime


class EventPublisher(Protocol):
    def publish(self, events: list[EventPayload]) -> IngestionReceipt: ...
