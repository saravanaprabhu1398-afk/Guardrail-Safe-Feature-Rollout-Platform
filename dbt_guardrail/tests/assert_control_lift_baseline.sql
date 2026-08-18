select *
from {{ ref('agg_experiment_results') }}
where control_conversion_rate not between 0 and 1
    or (
        variant_id = 'control'
        and abs(absolute_conversion_lift) > 0.000000000001
    )
