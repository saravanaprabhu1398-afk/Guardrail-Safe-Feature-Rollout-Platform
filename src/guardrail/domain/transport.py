from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from guardrail.domain.ingestion import EventPayload, IngestionReceipt


class TransportName(StrEnum):
    SNOWPIPE = "snowpipe"
    REDPANDA = "redpanda"


class TransportOffset(BaseModel):
    model_config = ConfigDict(frozen=True)

    topic: str
    partition: int = Field(ge=0)
    offset: int = Field(ge=0)


class EventDispatchReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    transport: TransportName
    records_submitted: int = Field(ge=1)
    accepted_at: datetime
    topic: str | None = None
    offsets: list[TransportOffset] = Field(default_factory=list)
    ingestion: IngestionReceipt | None = None


class EventDispatcher(Protocol):
    def dispatch(self, events: list[EventPayload]) -> EventDispatchReceipt: ...


class EventDispatchError(RuntimeError):
    pass


class WorkerBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    records_consumed: int = Field(ge=1)
    source_offsets: list[TransportOffset]
    ingestion: IngestionReceipt
