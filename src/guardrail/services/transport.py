from __future__ import annotations

from guardrail.domain.ingestion import EventPayload, EventPublisher
from guardrail.domain.transport import (
    EventDispatchReceipt,
    TransportName,
)


class DirectSnowpipeDispatcher:
    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher

    def dispatch(self, events: list[EventPayload]) -> EventDispatchReceipt:
        ingestion = self._publisher.publish(events)
        return EventDispatchReceipt(
            batch_id=ingestion.batch_id,
            transport=TransportName.SNOWPIPE,
            records_submitted=ingestion.records_submitted,
            accepted_at=ingestion.committed_at,
            ingestion=ingestion,
        )
