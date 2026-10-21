# Design decisions

## 1. Publish whole releases, not partial loads

A scheduled job that finishes is not enough: a report can read half a night of data
(new visits next to old hits). So data is never written onto the tables a dashboard
reads. Each release is built in its own database, tested with dbt, and only then switched.

## 2. The switch is not a transaction, so it is journaled

`EXCHANGE TABLES` works on one pair of tables at a time. Switching `visits`, `hits` and the
mart takes three statements. The journal records, before anything is touched:

    table | old uuid | new uuid

If the process dies half-way, `rp recover` reads the plan and compares the **current UUID**
of each public table: equal to `new_uuid` means the step is done, equal to `old_uuid` means
it is missing, anything else stops with `PublicationUncertain`. A blind retry of an
`EXCHANGE` would swap a finished step back, which is why recovery never retries blindly.

## 3. One writer, no lease stealing

The lock is a table. Creating it succeeds for exactly one process. There is no TTL: a stale
lock after a crash is a signal, not something to overwrite. `rp health` reports it.

## 4. Reconcile against the source, not against yourself

`dbt/tests/mart_covers_raw_visits.sql` compares the mart with raw. It passes even if rows
never reached raw, because both sides come from the same data. `rp reconcile` compares the
published tables with the external daily totals, which is what finds a short export.
`rp repair` reloads the affected days as a normal release, so the fix passes the same gate.

## 5. Readable robot rules

The Logs API has no robot flag. The mart marks visits with two rules you can read in
`dbt/models/marts/visits_hits_flat.sql`: one cookie with 5+ "new" visits a day, and a /24
subnet with 20+ short single-page visits by new users. A missing subnet is never evidence.

## 6. Independent freshness check

`rp health` reads only the health snapshot written after each publish, plus the lock. It can
run from a different machine or scheduler than the pipeline itself.
