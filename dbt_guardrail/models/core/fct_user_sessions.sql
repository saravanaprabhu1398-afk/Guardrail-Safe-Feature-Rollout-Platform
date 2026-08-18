select
    md5(concat_ws('|', user_id, session_id)) as session_key,
    user_id,
    session_id,
    min(event_timestamp) as session_started_at,
    max(event_timestamp) as session_ended_at,
    datediff('second', min(event_timestamp), max(event_timestamp)) as session_duration_seconds,
    count(*) as event_count,
    count(distinct event_name) as event_type_count,
    count(distinct experiment_id) as experiment_count,
    count(distinct variant_id) as reported_variant_count,
    count(distinct anonymous_id) as anonymous_identity_count,
    min(source) as first_source_alphabetically,
    count_if(is_late_event) as late_event_count
from {{ ref('stg_product_events') }}
group by user_id, session_id
