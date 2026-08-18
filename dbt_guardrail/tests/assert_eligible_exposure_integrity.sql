select *
from {{ ref('fct_experiment_exposures') }}
where is_eligible_exposure
    and (
        assignment_event_id is null
        or assignment_timestamp > exposure_timestamp
        or assigned_variant_id != variant_id
    )
