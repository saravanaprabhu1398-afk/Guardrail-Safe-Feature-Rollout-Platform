from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from guardrail.config import SnowflakeSettings, TransportSettings
from guardrail.domain.events import EventName, ProductEvent
from guardrail.domain.experiments import Variant
from guardrail.domain.ingestion import EventPayload, EventPublisher, EventPublishingError
from guardrail.domain.transport import (
    EventDispatchError,
    EventDispatchReceipt,
    TransportName,
    TransportOffset,
    WorkerBatchResult,
)
from guardrail.infrastructure.snowflake import connect
from guardrail.infrastructure.streaming import SnowpipeEventPublisher

ProducerFactory = Callable[[dict[str, Any]], Any]
ConsumerFactory = Callable[[dict[str, Any]], Any]
TopicPartitionFactory = Callable[[str, int, int], Any]


def _payload(event: EventPayload) -> dict[str, Any]:
    if isinstance(event, ProductEvent):
        return event.model_dump(mode="json")
    return dict(event)


class RedpandaEventDispatcher:
    """Publish API event batches after broker acknowledgement."""

    def __init__(
        self,
        settings: TransportSettings,
        *,
        producer_factory: ProducerFactory | None = None,
        delivery_timeout_seconds: float = 15,
    ) -> None:
        self._settings = settings
        self._producer_factory = producer_factory
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._producer: Any | None = None
        self._lock = Lock()

    def _get_producer(self) -> Any:
        if self._producer is not None:
            return self._producer
        producer_factory = self._producer_factory
        if producer_factory is None:
            from confluent_kafka import Producer

            producer_factory = Producer
        self._producer = producer_factory(
            {
                "bootstrap.servers": self._settings.redpanda_bootstrap_servers,
                "client.id": self._settings.redpanda_client_id,
                "enable.idempotence": True,
                "acks": "all",
                "compression.type": "zstd",
                "linger.ms": 5,
                "message.timeout.ms": 10_000,
            }
        )
        return self._producer

    def dispatch(self, events: list[EventPayload]) -> EventDispatchReceipt:
        if not events:
            raise ValueError("At least one event is required for dispatch.")

        with self._lock:
            producer = self._get_producer()
            batch_id = str(uuid4())
            accepted_at = datetime.now(UTC)
            offsets: list[TransportOffset] = []
            failures: list[str] = []

            def delivered(error: Any, message: Any) -> None:
                if error is not None:
                    failures.append(str(error))
                    return
                offsets.append(
                    TransportOffset(
                        topic=message.topic(),
                        partition=message.partition(),
                        offset=message.offset(),
                    )
                )

            for index, event in enumerate(events):
                payload = _payload(event)
                message_id = str(uuid5(NAMESPACE_URL, f"guardrail:{batch_id}:{index}"))
                envelope = {
                    "transport_version": 1,
                    "transport_batch_id": batch_id,
                    "transport_message_id": message_id,
                    "produced_at": accepted_at.isoformat(),
                    "payload": payload,
                }
                key = self._message_key(payload, message_id)
                try:
                    producer.produce(
                        self._settings.redpanda_topic,
                        key=key.encode("utf-8"),
                        value=json.dumps(
                            envelope,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8"),
                        on_delivery=delivered,
                    )
                except BufferError:
                    producer.poll(1)
                    producer.produce(
                        self._settings.redpanda_topic,
                        key=key.encode("utf-8"),
                        value=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
                        on_delivery=delivered,
                    )

            remaining = producer.flush(self._delivery_timeout_seconds)
            if remaining or failures or len(offsets) != len(events):
                detail = failures[0] if failures else f"{remaining} messages unacknowledged"
                raise EventDispatchError(
                    f"Redpanda did not acknowledge the complete event batch: {detail}"
                )

        return EventDispatchReceipt(
            batch_id=batch_id,
            transport=TransportName.REDPANDA,
            records_submitted=len(events),
            accepted_at=accepted_at,
            topic=self._settings.redpanda_topic,
            offsets=sorted(offsets, key=lambda item: (item.partition, item.offset)),
        )

    @staticmethod
    def _message_key(payload: dict[str, Any], fallback: str) -> str:
        experiment_id = payload.get("experiment_id")
        user_id = payload.get("user_id")
        if isinstance(experiment_id, str) and isinstance(user_id, str):
            return f"{experiment_id}:{user_id}"
        event_id = payload.get("event_id")
        return event_id if isinstance(event_id, str) else fallback

    def close(self) -> None:
        with self._lock:
            if self._producer is not None:
                self._producer.flush(self._delivery_timeout_seconds)
                self._producer = None


class RedpandaToSnowpipeWorker:
    """Commit source offsets only after Snowflake reconciliation succeeds."""

    def __init__(
        self,
        settings: TransportSettings,
        publisher: EventPublisher,
        *,
        consumer_factory: ConsumerFactory | None = None,
        topic_partition_factory: TopicPartitionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._publisher = publisher
        self._consumer_factory = consumer_factory
        self._topic_partition_factory = topic_partition_factory
        self._consumer: Any | None = None

    def _get_consumer(self) -> Any:
        if self._consumer is not None:
            return self._consumer
        consumer_factory = self._consumer_factory
        if consumer_factory is None:
            from confluent_kafka import Consumer

            consumer_factory = Consumer
        self._consumer = consumer_factory(
            {
                "bootstrap.servers": self._settings.redpanda_bootstrap_servers,
                "client.id": "guardrail-snowpipe-worker",
                "group.id": self._settings.redpanda_consumer_group,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": "earliest",
                "isolation.level": "read_committed",
            }
        )
        self._consumer.subscribe([self._settings.redpanda_topic])
        return self._consumer

    def process_once(self) -> WorkerBatchResult | None:
        consumer = self._get_consumer()
        messages: list[Any] = []
        batch_deadline: float | None = None

        while len(messages) < self._settings.redpanda_worker_batch_size:
            if batch_deadline is None:
                timeout = self._settings.redpanda_poll_timeout_seconds
            else:
                remaining = max(batch_deadline - monotonic(), 0)
                if remaining == 0:
                    break
                timeout = min(remaining, 0.25)
            message = consumer.poll(timeout)
            if message is None:
                break
            if message.error() is not None:
                raise EventDispatchError(f"Redpanda consumer error: {message.error()}")
            messages.append(message)
            if batch_deadline is None:
                batch_deadline = monotonic() + self._settings.redpanda_poll_timeout_seconds

        if not messages:
            return None

        payloads = [self._consumer_payload(message) for message in messages]
        ingestion = self._publisher.publish(payloads)
        if not ingestion.is_balanced:
            raise EventPublishingError(
                "Snowflake reconciliation did not balance; Redpanda offsets were not committed."
            )
        offsets = [
            TransportOffset(
                topic=message.topic(),
                partition=message.partition(),
                offset=message.offset(),
            )
            for message in messages
        ]
        self._commit(consumer, offsets)
        return WorkerBatchResult(
            records_consumed=len(messages),
            source_offsets=offsets,
            ingestion=ingestion,
        )

    @staticmethod
    def _consumer_payload(message: Any) -> dict[str, Any]:
        transport = {
            "topic": message.topic(),
            "partition": message.partition(),
            "offset": message.offset(),
            "timestamp": message.timestamp()[1],
        }
        try:
            envelope = json.loads(message.value())
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                raise TypeError("payload is not an object")
            payload = dict(payload)
            transport.update(
                {
                    "transport_batch_id": envelope.get("transport_batch_id"),
                    "transport_message_id": envelope.get("transport_message_id"),
                    "produced_at": envelope.get("produced_at"),
                }
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            payload = {
                "source": "redpanda",
                "attributes": {},
                "_guardrail_transport_error": str(exc),
            }
        payload["_guardrail_transport"] = transport
        return payload

    def _commit(self, consumer: Any, offsets: list[TransportOffset]) -> None:
        greatest: dict[tuple[str, int], int] = {}
        for item in offsets:
            key = (item.topic, item.partition)
            greatest[key] = max(greatest.get(key, -1), item.offset)

        topic_partition_factory = self._topic_partition_factory
        if topic_partition_factory is None:
            from confluent_kafka import TopicPartition

            topic_partition_factory = TopicPartition
        commit_offsets = [
            topic_partition_factory(topic, partition, offset + 1)
            for (topic, partition), offset in greatest.items()
        ]
        consumer.commit(offsets=commit_offsets, asynchronous=False)

    def close(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None


def run_worker() -> None:
    transport = TransportSettings()  # type: ignore[call-arg]
    snowflake = SnowflakeSettings().model_copy(  # type: ignore[call-arg]
        update={"snowflake_streaming_channel": "guardrail-redpanda-worker-v1"}
    )
    publisher = SnowpipeEventPublisher(snowflake)
    worker = RedpandaToSnowpipeWorker(transport, publisher)
    print(f"Consuming {transport.redpanda_topic} from {transport.redpanda_bootstrap_servers}")
    try:
        while True:
            result = worker.process_once()
            if result is not None:
                print(result.model_dump_json())
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()
        publisher.close()


def run_transport_smoke() -> None:
    transport = TransportSettings()  # type: ignore[call-arg]
    snowflake = SnowflakeSettings().model_copy(  # type: ignore[call-arg]
        update={"snowflake_streaming_channel": "guardrail-redpanda-worker-v1"}
    )
    timestamp = datetime.now(UTC)
    valid = ProductEvent(
        event_id=f"redpanda_smoke_{uuid4()}",
        event_name=EventName.EXPERIMENT_EXPOSED,
        user_id="redpanda_smoke_user",
        anonymous_id="redpanda_smoke_anonymous",
        session_id="redpanda_smoke_session",
        experiment_id="new_ranking_algorithm",
        variant_id=Variant.TREATMENT,
        event_timestamp=timestamp,
        sent_timestamp=timestamp,
        received_timestamp=timestamp,
        source="redpanda_smoke",
        attributes={"feature_version": "ranking-v1", "surface": "restaurant_feed"},
    )
    invalid = {
        "event_name": "unsupported_redpanda_smoke",
        "event_version": 1,
        "experiment_id": "new_ranking_algorithm",
        "user_id": "redpanda_smoke_user",
        "source": "redpanda_smoke",
        "attributes": {},
    }

    dispatcher = RedpandaEventDispatcher(transport)
    publisher = SnowpipeEventPublisher(snowflake)
    worker = RedpandaToSnowpipeWorker(transport, publisher)
    try:
        idle_polls = 0
        drain_deadline = monotonic() + 10
        while idle_polls < 2 and monotonic() < drain_deadline:
            if worker.process_once() is None:
                idle_polls += 1
            else:
                idle_polls = 0
        if idle_polls < 2:
            raise RuntimeError("Redpanda smoke could not establish an idle consumer baseline.")
        dispatch = dispatcher.dispatch([valid, valid, invalid])
        result = None
        deadline = monotonic() + 20
        while result is None and monotonic() < deadline:
            result = worker.process_once()
        if result is None:
            raise RuntimeError("Redpanda worker did not receive the smoke batch.")
    finally:
        dispatcher.close()
        worker.close()
        publisher.close()

    with connect(snowflake) as connection:
        lineage_count = (
            connection.cursor()
            .execute(
                """
                SELECT COUNT(*)
                FROM RAW.PRODUCT_EVENTS
                WHERE BATCH_ID = %s
                  AND PAYLOAD:_guardrail_transport:topic::STRING = %s
                  AND PAYLOAD:_guardrail_transport:partition IS NOT NULL
                  AND PAYLOAD:_guardrail_transport:offset IS NOT NULL
                """,
                (result.ingestion.batch_id, transport.redpanda_topic),
            )
            .fetchone()
        )
    preserved = lineage_count[0] if lineage_count is not None else 0
    evidence = {
        "transport_batch_id": dispatch.batch_id,
        "topic": dispatch.topic,
        "broker_offsets": [item.model_dump() for item in dispatch.offsets],
        "records_consumed": result.records_consumed,
        "snowflake_batch_id": result.ingestion.batch_id,
        "raw_count": result.ingestion.raw_count,
        "validated_count": result.ingestion.validated_count,
        "rejected_count": result.ingestion.rejected_count,
        "duplicate_count": result.ingestion.duplicate_count,
        "is_balanced": result.ingestion.is_balanced,
        "source_lineage_rows": preserved,
    }
    expected = {
        "records_consumed": 3,
        "raw_count": 3,
        "validated_count": 2,
        "rejected_count": 1,
        "duplicate_count": 1,
        "is_balanced": True,
        "source_lineage_rows": 3,
    }
    observed = {key: evidence[key] for key in expected}
    if observed != expected:
        raise RuntimeError(
            f"Redpanda transport acceptance failed: expected {expected}, observed {observed}"
        )
    print(json.dumps(evidence, indent=2))
