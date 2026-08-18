from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from guardrail.config import SnowflakeSettings
from guardrail.domain.observability import CostPerformanceError
from guardrail.infrastructure.observability import SnowflakeCostPerformanceRepository


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], captured: dict[str, Any]) -> None:
        self._rows = rows
        self._captured = captured

    def execute(self, query: str, parameters: tuple[str, str]) -> FakeCursor:
        self._captured["query"] = query
        self._captured["parameters"] = parameters
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]], captured: dict[str, Any]) -> None:
        self._cursor = FakeCursor(rows, captured)

    def cursor(self) -> FakeCursor:
        return self._cursor


def settings(pat_file: Path) -> SnowflakeSettings:
    return SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="USER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )


def test_cost_report_maps_bounded_query_history(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}
    rows = [
        (
            "GUARDRAIL_TRANSFORM_WH",
            "X-Small",
            "SELECT",
            42,
            1,
            12.5,
            900.0,
            0.0,
            1_024,
            200,
            0.001,
            0.0035,
        )
    ]

    @contextmanager
    def fake_connect(connection_settings: SnowflakeSettings):
        captured["settings"] = connection_settings
        yield FakeConnection(rows, captured)

    monkeypatch.setattr("guardrail.infrastructure.observability.connect", fake_connect)
    report = SnowflakeCostPerformanceRepository(settings(tmp_path / "pat")).get_report(
        lookback_hours=12,
        history_limit=500,
    )

    assert report.lookback_hours == 12
    assert report.history_limit == 500
    assert report.slices[0].query_type == "SELECT"
    assert report.slices[0].estimated_active_compute_credits == 0.0035
    assert "DATEADD('hour', -12" in captured["query"]
    assert "RESULT_LIMIT => 500" in captured["query"]
    assert captured["parameters"] == ("GUARDRAIL_INGEST_WH", "GUARDRAIL_TRANSFORM_WH")
    assert captured["settings"].snowflake_schema == "OBSERVABILITY"


@pytest.mark.parametrize(
    ("lookback_hours", "history_limit"),
    [(0, 100), (168, 100), (24, 0), (24, 10_001)],
)
def test_cost_report_rejects_unbounded_inputs(
    tmp_path: Path,
    lookback_hours: int,
    history_limit: int,
) -> None:
    repository = SnowflakeCostPerformanceRepository(settings(tmp_path / "pat"))

    with pytest.raises(ValueError):
        repository.get_report(
            lookback_hours=lookback_hours,
            history_limit=history_limit,
        )


def test_cost_report_masks_snowflake_failure(tmp_path: Path, monkeypatch) -> None:
    @contextmanager
    def failing_connect(connection_settings: SnowflakeSettings):
        raise RuntimeError("provider detail")
        yield

    monkeypatch.setattr("guardrail.infrastructure.observability.connect", failing_connect)

    with pytest.raises(CostPerformanceError, match="evidence is unavailable"):
        SnowflakeCostPerformanceRepository(settings(tmp_path / "pat")).get_report()
