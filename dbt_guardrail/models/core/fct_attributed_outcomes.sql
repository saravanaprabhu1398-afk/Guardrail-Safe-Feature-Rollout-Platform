with candidate_attribution as (
    select
        outcomes.*,
        exposures.exposure_event_id,
        exposures.variant_id as attributed_variant_id,
        exposures.allocation_version,
        exposures.exposure_timestamp,
        outcomes.reported_variant_id = exposures.variant_id as is_variant_consistent,
        datediff('second', exposures.exposure_timestamp, outcomes.outcome_timestamp)
            as seconds_after_exposure,
        row_number() over (
            partition by outcomes.outcome_event_id
            order by exposures.exposure_timestamp desc, exposures.exposure_event_id desc
        ) as attribution_rank
    from {{ ref('fct_product_outcomes') }} as outcomes
    inner join {{ ref('fct_experiment_exposures') }} as exposures
        on outcomes.experiment_id = exposures.experiment_id
        and outcomes.user_id = exposures.user_id
        and exposures.is_eligible_exposure
        and outcomes.outcome_timestamp >= exposures.exposure_timestamp
        and outcomes.outcome_timestamp < dateadd(
            'hour',
            {{ var('attribution_window_hours') }},
            exposures.exposure_timestamp
        )
)

select
    outcome_event_id,
    outcome_name,
    experiment_id,
    user_id,
    anonymous_id,
    session_id,
    attributed_variant_id as variant_id,
    reported_variant_id,
    allocation_version,
    exposure_event_id,
    exposure_timestamp,
    outcome_timestamp,
    seconds_after_exposure,
    is_variant_consistent,
    is_late_event,
    order_id,
    order_amount,
    currency,
    cancellation_actor,
    cancellation_reason,
    payment_provider,
    payment_error_category,
    delivery_minutes
from candidate_attribution
where attribution_rank = 1
