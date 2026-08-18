from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from guardrail.domain.experiments import Variant


class EventName(StrEnum):
    EXPERIMENT_ASSIGNED = "experiment_assigned"
    EXPERIMENT_EXPOSED = "experiment_exposed"
    RESTAURANT_VIEWED = "restaurant_viewed"
    ITEM_ADDED = "item_added"
    CHECKOUT_STARTED = "checkout_started"
    ORDER_COMPLETED = "order_completed"
    ORDER_CANCELLED = "order_cancelled"
    PAYMENT_FAILED = "payment_failed"
    DELIVERY_COMPLETED = "delivery_completed"


class ProductEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    event_name: EventName
    event_version: int = Field(default=1, ge=1)
    user_id: str
    anonymous_id: str
    session_id: str
    experiment_id: str
    variant_id: Variant
    event_timestamp: datetime
    sent_timestamp: datetime
    received_timestamp: datetime
    source: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class FaultProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_rate: float = Field(default=0.0, ge=0.0, le=0.5)
    late_event_rate: float = Field(default=0.0, ge=0.0, le=0.5)
    exposure_drop_rate: float = Field(default=0.0, ge=0.0, le=0.5)
    flicker_rate: float = Field(default=0.0, ge=0.0, le=0.2)


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    subjects: int = Field(default=100, ge=1, le=1000)
    seed: int = 42
    fault_profile: FaultProfile = Field(default_factory=FaultProfile)
    persist_events: bool = False


class SimulationResult(BaseModel):
    experiment_id: str
    subjects: int
    control_assignments: int
    treatment_assignments: int
    events_generated: int
    unique_events: int
    duplicate_events: int
    late_events: int
    flicker_subjects: int
    sample_events: list[ProductEvent]
    ingestion_batch_id: str | None = None
    telemetry_batch_id: str | None = None
    telemetry_transport: str | None = None
