select *
from {{ ref('agg_experiment_results') }}
where conversion_rate not between 0 and 1
    or cancellation_user_rate not between 0 and 1
    or payment_failure_user_rate not between 0 and 1
