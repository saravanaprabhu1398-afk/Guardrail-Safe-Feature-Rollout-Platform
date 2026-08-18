with eligible_exposures as (
    select *
    from {{ ref('fct_experiment_exposures') }}
    where is_eligible_exposure
),

exposure_summary as (
    select
        experiment_id,
        user_id,
        allocation_version,
        count(*) as exposure_count,
        min(exposure_timestamp) as first_exposure_at,
        max(exposure_timestamp) as last_exposure_at,
        count_if(is_late_event) as late_exposure_count
    from eligible_exposures
    group by experiment_id, user_id, allocation_version
),

exposure_integrity_summary as (
    select
        experiment_id,
        user_id,
        allocation_version,
        count(*) as observed_exposure_count,
        count(distinct variant_id) as exposed_variant_count
    from {{ ref('fct_experiment_exposures') }}
    where assignment_event_id is not null
    group by experiment_id, user_id, allocation_version
),

first_exposure as (
    select
        experiment_id,
        user_id,
        anonymous_id,
        allocation_version,
        variant_id,
        exposure_event_id as first_exposure_event_id,
        session_id as first_exposure_session_id,
        exposure_surface as first_exposure_surface,
        feature_version as first_feature_version
    from eligible_exposures
    qualify row_number() over (
        partition by experiment_id, user_id, allocation_version
        order by exposure_timestamp, exposure_event_id
    ) = 1
),

attributed_outcomes as (
    select
        experiment_id,
        user_id,
        allocation_version,
        variant_id,
        count_if(outcome_name = 'order_completed') as order_completed_event_count,
        count(distinct iff(outcome_name = 'order_completed', order_id, null))
            as completed_order_count,
        count_if(outcome_name = 'order_cancelled') as cancellation_event_count,
        count_if(outcome_name = 'payment_failed') as payment_failure_event_count,
        count_if(outcome_name = 'delivery_completed') as delivery_event_count,
        coalesce(sum(iff(outcome_name = 'order_completed', order_amount, 0)), 0)
            as completed_revenue,
        avg(iff(outcome_name = 'delivery_completed', delivery_minutes, null))
            as average_delivery_minutes,
        count_if(not is_variant_consistent) as variant_mismatch_outcome_count,
        count_if(is_late_event) as late_outcome_count
    from {{ ref('fct_attributed_outcomes') }}
    group by experiment_id, user_id, allocation_version, variant_id
)

select
    md5(concat_ws(
        '|',
        first_exposure.experiment_id,
        first_exposure.user_id,
        first_exposure.allocation_version
    )) as user_experiment_key,
    first_exposure.experiment_id,
    first_exposure.user_id,
    first_exposure.anonymous_id,
    first_exposure.allocation_version,
    first_exposure.variant_id,
    first_exposure.first_exposure_event_id,
    first_exposure.first_exposure_session_id,
    first_exposure.first_exposure_surface,
    first_exposure.first_feature_version,
    exposure_summary.first_exposure_at,
    exposure_summary.last_exposure_at,
    exposure_summary.exposure_count,
    exposure_integrity_summary.observed_exposure_count,
    exposure_integrity_summary.exposed_variant_count,
    exposure_integrity_summary.exposed_variant_count > 1 as has_variant_flicker,
    exposure_summary.late_exposure_count,
    coalesce(attributed_outcomes.order_completed_event_count, 0)
        as order_completed_event_count,
    coalesce(attributed_outcomes.completed_order_count, 0) as completed_order_count,
    coalesce(attributed_outcomes.cancellation_event_count, 0) as cancellation_event_count,
    coalesce(attributed_outcomes.payment_failure_event_count, 0)
        as payment_failure_event_count,
    coalesce(attributed_outcomes.delivery_event_count, 0) as delivery_event_count,
    coalesce(attributed_outcomes.completed_revenue, 0) as completed_revenue,
    attributed_outcomes.average_delivery_minutes,
    coalesce(attributed_outcomes.variant_mismatch_outcome_count, 0)
        as variant_mismatch_outcome_count,
    coalesce(attributed_outcomes.late_outcome_count, 0) as late_outcome_count,
    coalesce(attributed_outcomes.completed_order_count, 0) > 0 as did_convert,
    coalesce(attributed_outcomes.cancellation_event_count, 0) > 0 as did_cancel,
    coalesce(attributed_outcomes.payment_failure_event_count, 0) > 0 as had_payment_failure
from first_exposure
inner join exposure_summary
    on first_exposure.experiment_id = exposure_summary.experiment_id
    and first_exposure.user_id = exposure_summary.user_id
    and first_exposure.allocation_version = exposure_summary.allocation_version
inner join exposure_integrity_summary
    on first_exposure.experiment_id = exposure_integrity_summary.experiment_id
    and first_exposure.user_id = exposure_integrity_summary.user_id
    and first_exposure.allocation_version = exposure_integrity_summary.allocation_version
left join attributed_outcomes
    on first_exposure.experiment_id = attributed_outcomes.experiment_id
    and first_exposure.user_id = attributed_outcomes.user_id
    and first_exposure.allocation_version = attributed_outcomes.allocation_version
    and first_exposure.variant_id = attributed_outcomes.variant_id
