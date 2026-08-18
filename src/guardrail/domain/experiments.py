from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Variant(StrEnum):
    CONTROL = "control"
    TREATMENT = "treatment"


class RolloutState(StrEnum):
    DRAFT = "DRAFT"
    RUNNING_5 = "RUNNING_5"
    RUNNING_25 = "RUNNING_25"
    RUNNING_50 = "RUNNING_50"
    PAUSED = "PAUSED"
    RELEASED = "RELEASED"
    TERMINATED = "TERMINATED"


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        examples=["new_ranking_algorithm"],
    )
    name: str = Field(min_length=3, max_length=120)
    hypothesis: str = Field(min_length=10, max_length=500)
    primary_metric: str = Field(min_length=3, max_length=120)
    initial_treatment_percentage: Literal[5] = 5


class Experiment(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    name: str
    hypothesis: str
    primary_metric: str
    treatment_percentage: int
    allocation_version: int
    state: RolloutState
    created_at: datetime
    updated_at: datetime


class Assignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    subject_id: str
    variant: Variant
    bucket: int = Field(ge=0, le=9999)
    allocation_version: int = Field(ge=1)
    assigned_at: datetime
