select
    event_id as outcome_event_id,
    event_name as outcome_name,
    experiment_id,
    user_id,
    anonymous_id,
    session_id,
    variant_id as reported_variant_id,
    event_timestamp as outcome_timestamp,
    received_timestamp,
    first_ingested_at,
    source,
    is_late_event,
    order_id,
    order_amount,
    currency,
    cancellation_actor,
    cancellation_reason,
    payment_provider,
    payment_error_category,
    delivery_minutes
from {{ ref('stg_product_events') }}
where event_name in (
    'order_completed',
    'order_cancelled',
    'payment_failed',
    'delivery_completed'
)
