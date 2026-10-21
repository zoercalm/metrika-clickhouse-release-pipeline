-- Returns rows (= failure) when the mart and the raw visits disagree on a day's count.
-- NOTE: this checks the mart against raw, built with the same join. It cannot
-- notice data that never reached raw. External reconciliation (rp reconcile) does that.
with raw_n as (
    select date, count() as n from {{ source('raw', 'visits') }} group by date
),
mart_n as (
    select date, count() as n from {{ ref('visits_hits_flat') }} group by date
)
select r.date
from raw_n as r
left join mart_n as m on m.date = r.date
where coalesce(m.n, 0) != r.n
