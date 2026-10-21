{{ config(materialized='table', engine='MergeTree()', order_by='(date, visit_id)') }}

-- One row per visit with hit counts and two readable robot rules.
-- The Logs API has no robot flag, so the rules are explicit and explainable.

with hits_per_visit as (
    select
        visit_id,
        count() as hit_count,
        countIf(hit_type = 'pageview') as pageviews
    from {{ source('raw', 'hits') }}
    group by visit_id
),

client_day as (
    select
        client_id,
        date,
        count() as visits_in_day,
        countIf(is_new = 1) as new_visits_in_day
    from {{ source('raw', 'visits') }}
    group by client_id, date
),

subnet_day as (
    select
        ip_subnet,
        date,
        countIf(is_new = 1 and duration_s <= 10 and page_views <= 1) as short_new_visits
    from {{ source('raw', 'visits') }}
    where ip_subnet != ''          -- a missing subnet is never evidence
    group by ip_subnet, date
)

select
    v.visit_id as visit_id,
    v.counter_id as counter_id,
    v.client_id as client_id,
    v.date as date,
    v.start_ts as start_ts,
    v.is_new as is_new,
    v.device as device,
    v.traffic_source as traffic_source,
    coalesce(h.hit_count, 0) as hit_count,
    coalesce(h.pageviews, 0) as pageviews,
    -- Rule 1: one cookie, 5+ visits in a day, every one of them "new".
    (c.visits_in_day >= 5 and c.new_visits_in_day = c.visits_in_day) as is_robot_client_flood,
    -- Rule 2: a /24 subnet with 20+ short single-page visits by new users in a day.
    (coalesce(s.short_new_visits, 0) >= 20 and v.is_new = 1 and v.duration_s <= 10 and v.page_views <= 1) as is_robot_subnet_farm
from {{ source('raw', 'visits') }} as v
left join hits_per_visit as h on h.visit_id = v.visit_id
left join client_day as c on c.client_id = v.client_id and c.date = v.date
left join subnet_day as s on s.ip_subnet = v.ip_subnet and s.date = v.date
