with user_profile as (
    select
        user_id,
        min(event_timestamp) as first_seen_at,
        max(event_timestamp) as last_seen_at,
        count(*) as lifetime_event_count,
        count(distinct anonymous_id) as anonymous_identity_count,
        count(distinct session_id) as session_count,
        count(distinct experiment_id) as experiment_count
    from {{ ref('stg_product_events') }}
    group by user_id
),

latest_identity as (
    select
        user_id,
        anonymous_id as latest_anonymous_id,
        source as latest_source
    from {{ ref('stg_product_events') }}
    qualify row_number() over (
        partition by user_id
        order by event_timestamp desc, received_timestamp desc, event_id desc
    ) = 1
)

select
    md5(user_profile.user_id) as user_key,
    user_profile.user_id,
    latest_identity.latest_anonymous_id,
    latest_identity.latest_source,
    user_profile.first_seen_at,
    user_profile.last_seen_at,
    user_profile.lifetime_event_count,
    user_profile.anonymous_identity_count,
    user_profile.session_count,
    user_profile.experiment_count,
    user_profile.anonymous_identity_count > 1 as is_cross_identity_user
from user_profile
left join latest_identity using (user_id)
