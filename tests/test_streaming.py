from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.domain.events import EventName, ProductEvent
from guardrail.domain.experiments import Variant
from guardrail.infrastructure.streaming import SnowpipeEventPublisher


class FakeChannel:
    def __init__(self) -> None:
        self.append_calls: list[tuple[list[dict[str, Any]], str, str]] = []
        self.closed = False

    def get_latest_committed_offset_token(self) -> str:
        return "9"

    def append_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        start_offset_token: str,
        end_offset_token: str,
    ) -> None:
        self.append_calls.append((rows, start_offset_token, end_offset_token))

    def wait_for_commit(self, token_checker: Any, *, timeout_seconds: int) -> None:
        assert timeout_seconds == 30
        assert token_checker(None) is False
        assert token_checker(self.append_calls[-1][2])

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, channel: FakeChannel, captured: dict[str, Any], **kwargs: Any) -> None:
        self.channel = channel
        self.captured = captured
        self.captured.update(kwargs)
        self.closed = False

    def open_channel(self, channel_name: str) -> tuple[FakeChannel, object]:
        self.captured["channel_name"] = channel_name
        return self.channel, object()

    def close(self) -> None:
        self.closed = True


def settings(pat_file: Path) -> SnowflakeSettings:
    return SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="USER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )


def product_event(event_id: str) -> ProductEvent:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return ProductEvent(
        event_id=event_id,
        event_name=EventName.EXPERIMENT_EXPOSED,
        user_id="user_1",
        anonymous_id="anon_1",
        session_id="session_1",
        experiment_id="ranking_test",
        variant_id=Variant.TREATMENT,
        event_timestamp=timestamp,
        sent_timestamp=timestamp,
        received_timestamp=timestamp,
        source="web",
        attributes={"feature_version": "ranking-v1", "surface": "restaurant_feed"},
    )


def test_streaming_publisher_resumes_offsets_and_builds_auditable_rows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    pat_file = tmp_path / "pat"
    pat_file.write_text("secret-token", encoding="utf-8")
    channel = FakeChannel()
    captured: dict[str, Any] = {}
    client = FakeClient(channel, captured)
    publisher = SnowpipeEventPublisher(settings(pat_file), client_factory=lambda **_: client)
    monkeypatch.setattr(
        publisher,
        "_process_batch",
        lambda **_: {
            "raw_count": 2,
            "validated_count": 2,
            "rejected_count": 0,
            "duplicate_count": 0,
            "is_balanced": True,
        },
    )

    receipt = publisher.publish([product_event("event_1"), product_event("event_2")])

    rows, start_offset, end_offset = channel.append_calls[0]
    assert (start_offset, end_offset) == ("10", "11")
    assert [row["stream_offset"] for row in rows] == [10, 11]
    assert rows[0]["payload"]["event_name"] == "experiment_exposed"
    assert len(rows[0]["payload_hash"]) == 64
    assert receipt.first_offset == 10
    assert receipt.last_offset == 11
    assert receipt.is_balanced is True
    assert captured["channel_name"] == "guardrail-local-dev"

    publisher.close()
    assert channel.closed is True
    assert client.closed is True
