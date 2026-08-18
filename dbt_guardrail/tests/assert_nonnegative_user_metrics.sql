select *
from {{ ref('fct_user_experiment_metrics') }}
where exposure_count < 1
    or completed_order_count < 0
    or cancellation_event_count < 0
    or payment_failure_event_count < 0
    or completed_revenue < 0
