from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from guardrail.config import TransportSettings
from guardrail.domain.ingestion import EventPublishingError, IngestionReceipt
from guardrail.infrastructure.redpanda import (
    RedpandaEventDispatcher,
    RedpandaToSnowpipeWorker,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def settings() -> TransportSettings:
    return TransportSettings(
        redpanda_bootstrap_servers="broker:9092",
        redpanda_topic="events.v1",
        redpanda_client_id="test-producer",
        redpanda_consumer_group="test-consumer",
        redpanda_poll_timeout_seconds=0.01,
        _env_file=None,
    )


def receipt(records: int, *, balanced: bool = True) -> IngestionReceipt:
    return IngestionReceipt(
        batch_id="snowflake_batch",
        channel_id="worker-channel",
        records_submitted=records,
        first_offset=20,
        last_offset=20 + records - 1,
        raw_count=records,
        validated_count=records,
        rejected_count=0,
        duplicate_count=0,
        is_balanced=balanced,
        committed_at=NOW,
    )


class DeliveryMessage:
    def __init__(self, topic: str, partition: int, offset: int) -> None:
        self._topic = topic
        self._partition = partition
        self._offset = offset

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class FakeProducer:
    def __init__(self, configuration: dict[str, Any]) -> None:
        self.configuration = configuration
        self.produced: list[dict[str, Any]] = []

    def produce(
        self,
        topic: str,
        *,
        key: bytes,
        value: bytes,
        on_delivery: Any,
    ) -> None:
        offset = len(self.produced)
        self.produced.append({"topic": topic, "key": key, "value": value})
        on_delivery(None, DeliveryMessage(topic, 1, offset))

    def poll(self, timeout: float) -> None:
        return None

    def flush(self, timeout: float) -> int:
        return 0


def test_redpanda_dispatch_waits_for_acknowledged_offsets() -> None:
    captured: dict[str, Any] = {}

    def factory(configuration: dict[str, Any]) -> FakeProducer:
        captured.update(configuration)
        return FakeProducer(configuration)

    dispatcher = RedpandaEventDispatcher(settings(), producer_factory=factory)
    events = [
        {"event_id": "one", "experiment_id": "exp", "user_id": "user"},
        {"event_id": "two", "experiment_id": "exp", "user_id": "user"},
    ]

    result = dispatcher.dispatch(events)

    assert result.transport == "redpanda"
    assert result.records_submitted == 2
    assert [(item.partition, item.offset) for item in result.offsets] == [(1, 0), (1, 1)]
    assert captured["enable.idempotence"] is True
    assert captured["acks"] == "all"


class ConsumerMessage:
    def __init__(self, payload: dict[str, Any], offset: int) -> None:
        self._value = json.dumps(
            {
                "transport_batch_id": "transport_batch",
                "transport_message_id": f"message_{offset}",
                "produced_at": NOW.isoformat(),
                "payload": payload,
            }
        ).encode()
        self._offset = offset

    def value(self) -> bytes:
        return self._value

    def topic(self) -> str:
        return "events.v1"

    def partition(self) -> int:
        return 2

    def offset(self) -> int:
        return self._offset

    def timestamp(self) -> tuple[int, int]:
        return 1, 1_767_225_600_000

    def error(self) -> None:
        return None


class FakeConsumer:
    def __init__(self, messages: list[ConsumerMessage]) -> None:
        self.messages = messages
        self.subscriptions: list[str] = []
        self.commits: list[list[tuple[str, int, int]]] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        self.subscriptions = topics

    def poll(self, timeout: float) -> ConsumerMessage | None:
        return self.messages.pop(0) if self.messages else None

    def commit(self, *, offsets: list[tuple[str, int, int]], asynchronous: bool) -> None:
        assert asynchronous is False
        self.commits.append(offsets)

    def close(self) -> None:
        self.closed = True


class WorkerPublisher:
    def __init__(self, *, fail: bool = False, balanced: bool = True) -> None:
        self.fail = fail
        self.balanced = balanced
        self.batches: list[list[dict[str, Any]]] = []

    def publish(self, events: list[dict[str, Any]]) -> IngestionReceipt:
        self.batches.append(events)
        if self.fail:
            raise EventPublishingError("snowflake unavailable")
        return receipt(len(events), balanced=self.balanced)


def test_worker_preserves_source_lineage_before_manual_commit() -> None:
    consumer = FakeConsumer(
        [
            ConsumerMessage({"event_id": "one"}, 4),
            ConsumerMessage({"event_id": "two"}, 5),
        ]
    )
    publisher = WorkerPublisher()
    worker = RedpandaToSnowpipeWorker(
        settings(),
        publisher,
        consumer_factory=lambda _: consumer,
        topic_partition_factory=lambda topic, partition, offset: (
            topic,
            partition,
            offset,
        ),
    )

    result = worker.process_once()

    assert result is not None
    assert result.records_consumed == 2
    assert publisher.batches[0][0]["_guardrail_transport"]["offset"] == 4
    assert consumer.commits == [[("events.v1", 2, 6)]]


def test_worker_does_not_commit_when_snowflake_fails() -> None:
    consumer = FakeConsumer([ConsumerMessage({"event_id": "one"}, 4)])
    worker = RedpandaToSnowpipeWorker(
        settings(),
        WorkerPublisher(fail=True),
        consumer_factory=lambda _: consumer,
        topic_partition_factory=lambda topic, partition, offset: (
            topic,
            partition,
            offset,
        ),
    )

    with pytest.raises(EventPublishingError, match="snowflake unavailable"):
        worker.process_once()

    assert consumer.commits == []


def test_worker_does_not_commit_when_reconciliation_is_unbalanced() -> None:
    consumer = FakeConsumer([ConsumerMessage({"event_id": "one"}, 4)])
    worker = RedpandaToSnowpipeWorker(
        settings(),
        WorkerPublisher(balanced=False),
        consumer_factory=lambda _: consumer,
        topic_partition_factory=lambda topic, partition, offset: (
            topic,
            partition,
            offset,
        ),
    )

    with pytest.raises(EventPublishingError, match="did not balance"):
        worker.process_once()

    assert consumer.commits == []
