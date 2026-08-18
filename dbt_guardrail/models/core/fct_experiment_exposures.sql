with exposure_events as (
    select
        event_id as exposure_event_id,
        experiment_id,
        user_id,
        anonymous_id,
        session_id,
        variant_id as exposed_variant_id,
        feature_version,
        exposure_surface,
        event_timestamp as exposure_timestamp,
        received_timestamp,
        first_ingested_at,
        source,
        is_late_event
    from {{ ref('stg_product_events') }}
    where event_name = 'experiment_exposed'
),

candidate_assignments as (
    select
        exposure_events.*,
        assignments.assignment_event_id,
        assignments.variant_id as assigned_variant_id,
        assignments.allocation_version,
        assignments.assignment_timestamp,
        assignments.assignment_bucket,
        row_number() over (
            partition by exposure_events.exposure_event_id
            order by
                assignments.assignment_timestamp desc nulls last,
                assignments.assignment_event_id desc nulls last
        ) as assignment_match_rank
    from exposure_events
    left join {{ ref('fct_experiment_assignments') }} as assignments
        on exposure_events.experiment_id = assignments.experiment_id
        and exposure_events.user_id = assignments.user_id
        and assignments.assignment_timestamp <= exposure_events.exposure_timestamp
)

select
    exposure_event_id,
    experiment_id,
    user_id,
    anonymous_id,
    session_id,
    exposed_variant_id as variant_id,
    feature_version,
    exposure_surface,
    exposure_timestamp,
    received_timestamp,
    first_ingested_at,
    source,
    is_late_event,
    assignment_event_id,
    assigned_variant_id,
    allocation_version,
    assignment_timestamp,
    assignment_bucket,
    datediff('second', assignment_timestamp, exposure_timestamp) as seconds_after_assignment,
    assignment_event_id is not null
        and assigned_variant_id = exposed_variant_id as is_eligible_exposure
from candidate_assignments
where assignment_match_rank = 1
