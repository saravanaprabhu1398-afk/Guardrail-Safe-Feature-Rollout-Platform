select *
from {{ ref('fct_attributed_outcomes') }}
where seconds_after_exposure < 0
    or seconds_after_exposure >= {{ var('attribution_window_hours') }} * 60 * 60
