from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class QueryPerformanceSlice(BaseModel):
    model_config = ConfigDict(frozen=True)

    warehouse_name: str
    warehouse_size: str | None
    query_type: str
    successful_queries: int = Field(ge=0)
    failed_queries: int = Field(ge=0)
    total_execution_seconds: float = Field(ge=0)
    p95_total_elapsed_ms: float = Field(ge=0)
    average_queued_overload_ms: float = Field(ge=0)
    bytes_scanned: int = Field(ge=0)
    rows_produced: int = Field(ge=0)
    cloud_services_credits: float = Field(ge=0)
    estimated_active_compute_credits: float = Field(ge=0)


class CostPerformanceReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    lookback_hours: int = Field(ge=1, le=167)
    history_limit: int = Field(ge=1, le=10_000)
    slices: tuple[QueryPerformanceSlice, ...]
    cost_caveat: str


class CostPerformanceError(RuntimeError):
    pass
