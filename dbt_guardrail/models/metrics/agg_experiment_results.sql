with variant_results as (
    select
        md5(concat_ws('|', experiment_id, variant_id, allocation_version))
            as experiment_variant_key,
        experiment_id,
        variant_id,
        allocation_version,
        min(first_exposure_at) as first_exposure_at,
        max(last_exposure_at) as last_exposure_at,
        count(*) as exposed_users,
        sum(exposure_count) as total_exposures,
        count_if(did_convert) as converted_users,
        count_if(did_cancel) as users_with_cancellation,
        count_if(had_payment_failure) as users_with_payment_failure,
        count_if(has_variant_flicker) as variant_flicker_users,
        count_if(variant_mismatch_outcome_count > 0) as variant_mismatch_users,
        sum(completed_order_count) as completed_orders,
        sum(completed_revenue) as completed_revenue,
        sum(completed_revenue) / nullif(count(*), 0) as revenue_per_exposed_user,
        count_if(did_convert) / nullif(count(*), 0)::float as conversion_rate,
        count_if(did_cancel) / nullif(count(*), 0)::float as cancellation_user_rate,
        count_if(had_payment_failure) / nullif(count(*), 0)::float
            as payment_failure_user_rate,
        avg(average_delivery_minutes) as average_delivery_minutes,
        sum(late_exposure_count + late_outcome_count) as late_event_count
    from {{ ref('fct_user_experiment_metrics') }}
    group by experiment_id, variant_id, allocation_version
),

results_with_control as (
    select
        *,
        max(iff(variant_id = 'control', conversion_rate, null)) over (
            partition by experiment_id, allocation_version
        ) as control_conversion_rate
    from variant_results
)

select
    *,
    conversion_rate - control_conversion_rate as absolute_conversion_lift,
    (conversion_rate - control_conversion_rate)
        / nullif(control_conversion_rate, 0) as relative_conversion_lift
from results_with_control
