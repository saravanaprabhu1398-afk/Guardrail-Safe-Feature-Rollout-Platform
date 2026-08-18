from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from random import Random
from uuid import NAMESPACE_URL, uuid5

from guardrail.domain.events import (
    EventName,
    FaultProfile,
    ProductEvent,
    SimulationResult,
)
from guardrail.domain.experiments import Assignment, Variant


class MarketplaceSimulator:
    def generate_journey(
        self,
        assignment: Assignment,
        *,
        seed: int,
        started_at: datetime,
        fault_profile: FaultProfile,
    ) -> list[ProductEvent]:
        random = Random(f"{seed}:{assignment.experiment_id}:{assignment.subject_id}")
        session_id = f"session_{assignment.subject_id}_{seed}"
        anonymous_id = f"anon_{assignment.subject_id}"
        source = random.choice(["ios", "android", "web"])
        events: list[ProductEvent] = []

        def event(
            name: EventName,
            seconds_after_start: int,
            *,
            variant: Variant | None = None,
            attributes: dict[str, object] | None = None,
            occurrence: int = 1,
        ) -> ProductEvent:
            event_time = started_at + timedelta(seconds=seconds_after_start)
            event_key = (
                f"{assignment.experiment_id}:{assignment.subject_id}:{name}:{occurrence}:{seed}"
            )
            network_delay = timedelta(milliseconds=random.randint(50, 1800))
            return ProductEvent(
                event_id=str(uuid5(NAMESPACE_URL, event_key)),
                event_name=name,
                user_id=assignment.subject_id,
                anonymous_id=anonymous_id,
                session_id=session_id,
                experiment_id=assignment.experiment_id,
                variant_id=variant or assignment.variant,
                event_timestamp=event_time,
                sent_timestamp=event_time + timedelta(milliseconds=20),
                received_timestamp=event_time + network_delay,
                source=source,
                attributes=attributes or {},
            )

        events.append(
            event(
                EventName.EXPERIMENT_ASSIGNED,
                0,
                attributes={
                    "bucket": assignment.bucket,
                    "allocation_version": assignment.allocation_version,
                    "assignment_reason": "deterministic_bucket",
                },
            )
        )

        if random.random() < fault_profile.exposure_drop_rate:
            return self._apply_transport_faults(events, random, fault_profile)

        exposure_attributes = {
            "feature_version": "ranking-v1",
            "surface": "restaurant_feed",
        }
        events.append(
            event(
                EventName.EXPERIMENT_EXPOSED,
                1,
                attributes=exposure_attributes,
            )
        )
        restaurant_id = f"restaurant_{random.randint(1, 40)}"
        events.append(
            event(
                EventName.RESTAURANT_VIEWED,
                4,
                attributes={
                    "restaurant_id": restaurant_id,
                    "rank": random.randint(1, 10),
                    "query_id": f"query_{assignment.subject_id}_{seed}",
                },
            )
        )

        treatment_bonus = 0.05 if assignment.variant == Variant.TREATMENT else 0.0
        if random.random() < 0.55 + treatment_bonus:
            amount = round(random.uniform(25, 180), 2)
            events.append(
                event(
                    EventName.ITEM_ADDED,
                    12,
                    attributes={
                        "restaurant_id": restaurant_id,
                        "item_id": f"item_{random.randint(1, 200)}",
                        "price": amount,
                    },
                )
            )

            if random.random() < 0.72:
                order_id = f"order_{assignment.subject_id}_{seed}"
                cart_id = f"cart_{assignment.subject_id}_{seed}"
                events.append(
                    event(
                        EventName.CHECKOUT_STARTED,
                        25,
                        attributes={
                            "cart_id": cart_id,
                            "order_id": order_id,
                            "cart_value": amount,
                        },
                    )
                )

                if random.random() < 0.04:
                    events.append(
                        event(
                            EventName.PAYMENT_FAILED,
                            34,
                            attributes={
                                "order_id": order_id,
                                "payment_provider": "mock_pay",
                                "error_category": "declined",
                            },
                        )
                    )
                elif random.random() < 0.76 + treatment_bonus:
                    events.append(
                        event(
                            EventName.ORDER_COMPLETED,
                            36,
                            attributes={
                                "order_id": order_id,
                                "order_amount": amount,
                                "currency": "AED",
                            },
                        )
                    )
                    if random.random() < 0.08:
                        events.append(
                            event(
                                EventName.ORDER_CANCELLED,
                                180,
                                attributes={
                                    "order_id": order_id,
                                    "actor": "customer",
                                    "reason": "changed_mind",
                                },
                            )
                        )
                    else:
                        delivery_minutes = random.randint(18, 55)
                        events.append(
                            event(
                                EventName.DELIVERY_COMPLETED,
                                delivery_minutes * 60,
                                attributes={
                                    "order_id": order_id,
                                    "delivery_minutes": delivery_minutes,
                                },
                            )
                        )

        if random.random() < fault_profile.flicker_rate:
            opposite = (
                Variant.CONTROL if assignment.variant == Variant.TREATMENT else Variant.TREATMENT
            )
            events.append(
                event(
                    EventName.EXPERIMENT_EXPOSED,
                    2,
                    variant=opposite,
                    attributes={
                        **exposure_attributes,
                        "fault_injected": "variant_flicker",
                    },
                    occurrence=2,
                )
            )

        return self._apply_transport_faults(events, random, fault_profile)

    @staticmethod
    def _apply_transport_faults(
        events: list[ProductEvent], random: Random, fault_profile: FaultProfile
    ) -> list[ProductEvent]:
        transported: list[ProductEvent] = []
        for product_event in events:
            if random.random() < fault_profile.late_event_rate:
                product_event = product_event.model_copy(
                    update={
                        "received_timestamp": product_event.event_timestamp + timedelta(minutes=15)
                    }
                )
            transported.append(product_event)
            if random.random() < fault_profile.duplicate_rate:
                transported.append(product_event)
        return transported

    def summarize(
        self,
        experiment_id: str,
        assignments: list[Assignment],
        events: list[ProductEvent],
    ) -> SimulationResult:
        assignment_counts = Counter(item.variant for item in assignments)
        event_counts = Counter(item.event_id for item in events)
        late_events = sum(
            item.received_timestamp - item.event_timestamp >= timedelta(minutes=5)
            for item in events
        )
        variants_by_subject: dict[str, set[Variant]] = defaultdict(set)
        for item in events:
            if item.event_name == EventName.EXPERIMENT_EXPOSED:
                variants_by_subject[item.user_id].add(item.variant_id)

        return SimulationResult(
            experiment_id=experiment_id,
            subjects=len(assignments),
            control_assignments=assignment_counts[Variant.CONTROL],
            treatment_assignments=assignment_counts[Variant.TREATMENT],
            events_generated=len(events),
            unique_events=len(event_counts),
            duplicate_events=sum(count - 1 for count in event_counts.values()),
            late_events=late_events,
            flicker_subjects=sum(len(variants) > 1 for variants in variants_by_subject.values()),
            sample_events=events[:10],
        )


DEFAULT_SIMULATION_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
