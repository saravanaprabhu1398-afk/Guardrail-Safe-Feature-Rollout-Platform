select
    event_id as assignment_event_id,
    experiment_id,
    user_id,
    anonymous_id,
    session_id,
    variant_id,
    allocation_version,
    assignment_bucket,
    assignment_reason,
    event_timestamp as assignment_timestamp,
    received_timestamp,
    first_ingested_at,
    source,
    row_number() over (
        partition by experiment_id, user_id, allocation_version
        order by event_timestamp, received_timestamp, event_id
    ) as assignment_sequence
from {{ ref('stg_product_events') }}
where event_name = 'experiment_assigned'
