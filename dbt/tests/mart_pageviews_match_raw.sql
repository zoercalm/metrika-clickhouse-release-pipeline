-- Total pageviews in the mart must equal pageview hits in raw.
select 1
from (
    select
        (select count() from {{ source('raw', 'hits') }} where hit_type = 'pageview') as raw_pv,
        (select sum(pageviews) from {{ ref('visits_hits_flat') }}) as mart_pv
)
where raw_pv != mart_pv
