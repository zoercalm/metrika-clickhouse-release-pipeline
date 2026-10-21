# Metrika → ClickHouse: tested mart publishing

Build and test a complete analytics mart before replacing the table a dashboard reads. A query against that single mart sees the old or new table. **This is not an atomic switch of all raw tables**: joins across raw visits/hits can see mixed versions while the three tables are switched. The first publish has no previous mart.

Synthetic engineering demo, not a production release manager. Interrupted switches and finalisation are journaled and recoverable after stopping the writer.

Everything here runs on **synthetic data**: a stand-in for the Yandex Metrika Logs API and
Reporting API generates visits and hits. No client code, data, counters or domains are included.

![Architecture](docs/architecture.svg)

## The problem

- A scheduled load can finish and still leave a report reading an incomplete dataset
  (new visits next to old hits).
- When numbers freeze, nobody can tell a broken pipeline from a website that stopped
  sending events.
- A test that checks the mart against raw data built with the same join will happily
  report "0 gaps" for data that never arrived.

## Example outcome
The deterministic integration scenario starts with **250 synthetic visits** (five days × 50). The following are executable assertions in `tests/test_release.py`:

| Action | Visible result |
|---|---|
| Publish the baseline | 250 visits; health OK |
| Add a duplicate visit and publish | dbt rejects the build; the old totals remain unchanged |
| Interrupt switch after table 2 | lock held; new writer rejected; health not OK |
| Stop the writer and recover | mart pageviews match raw pageview hits; health OK |
| Recover again | no pending release; totals unchanged |
| Interrupt after `published`, then recover | owning release finishes cleanup/unlock; no duplicate health snapshot |

The last case prevents a finished-looking journal entry from leaving the pipeline permanently locked.

## Try it

Requirements: Docker, Python 3.10+.

```bash
make install        # virtualenv + dependencies (includes dbt-clickhouse)
make demo           # start ClickHouse, publish a release, reconcile, check health
make test           # integration tests against a real ClickHouse
make down           # remove the disposable ClickHouse container and data volume
```

Scenarios to run by hand (`.venv/bin/rp --help`):

| Command | What it shows |
|---|---|
| `rp release` | Stage → full build → dbt build + tests → journal → switch |
| `rp release --duplicate-visit` | A bad feed is rejected by dbt tests; production is untouched |
| `rp release --crash-after 2` then `rp health` then `rp recover --writer-stopped` | An interrupted switch holds the lock; recovery compares UUIDs and finishes only the missing step |

| `rp release --lose-hits 3` then `rp reconcile` then `rp repair` | A short export is caught against external totals and reloaded through the same gate |
| `rp health` | Freshness and lock check; exit code 1 when stale (limit 26 h by default) |

## What a release does

1. Take the lock (a table; one writer, no TTL).
2. Fetch the date range into `stage_<id>`.
3. Build `build_<id>`: history outside the range plus the staged range.
4. Run `dbt build` against the build database: the mart and 17 dbt tests. Failure = stop, drop, publish nothing.
5. Journal the plan: table, old UUID, new UUID.
6. Switch `visits`, `hits`, then the mart (`EXCHANGE TABLES`; the first release uses `RENAME`).
7. Write the health snapshot; keep the previous version as `prev_<id>`; journal publication; release the owned lock. Each finalisation step can be resumed.

Details and the reasoning are in [docs/design-decisions.md](docs/design-decisions.md).

## What the tests cover

- first release publishes and passes the health check;
- a rejected release leaves production and databases clean;
- rollback restores the previous release; the first release cannot be rolled back;
- a crash after step 1, 2 or 3 holds the lock, blocks other writers, is recovered, and recovery is idempotent;
- the lock is exclusive;
- reconcile finds a short export and repair fixes it;
- health flags a stale publish;
- a Telegram bot token never reaches logs.

## What this is not

- Not a production system and not a claim about any client's production. It is the
  engineering pattern, rebuilt from scratch on synthetic data.
- Not a Logs API client: the source is simulated, and no network calls are made unless you
  set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Not a multi-node ClickHouse setup; `EXCHANGE TABLES` needs an Atomic database.
- Reconciliation covers visits and pageviews, not users or attribution.

## Layout

```
src/release_pipeline/   release, recovery, rollback, reconcile, health, notify, CLI
dbt/                    mart + 17 tests (sources.yml, schema.yml, two singular tests)
tests/                  pytest suite (real ClickHouse)
docs/                   architecture and design decisions
```

## Adaptation
I would connect a real source adapter, establish retention and permissions, and test failure modes in the target deployment before scheduling this pattern. Readers must use the completed mart rather than join independently switched raw tables.

## Experimental rollback
`rp rollback` demonstrates a successful reverse switch only. It is **not crash-safe**, and the health snapshot still describes the latest publication rather than the rollback. Use only in a disposable demo; it is not part of the supported recovery guarantee. Recreate the demo database after exploring it.

## Recovery boundaries
Stop the original writer before `rp recover --writer-stopped`, and run only one recovery process. Recovery refuses to clear a lock without its own prepared plan. Finalisation is repeatable after health insertion, previous-version cleanup, database rename, published journaling and before unlocking. The same cases are tested for first and subsequent publication. A crash before a prepared plan still requires manual inspection; this demo deliberately does not guess whether the writer is alive.

[CI checks](https://github.com/zoercalm/metrika-clickhouse-release-pipeline/actions/workflows/ci.yml). MIT licensed.
