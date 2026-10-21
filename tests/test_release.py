from datetime import date

import pytest

from release_pipeline.health import check
from release_pipeline.release import (PipelineBusy, PipelineLock, ReleaseRejected, SimulatedCrash,
                                      journal_events, recover, rollback, run_release)
from release_pipeline.reconcile import reconcile, repair
from release_pipeline.notify import redact
from conftest import totals


def span(src):
    d = src.days()
    return d[0], d[-1]


def test_first_release_publishes_and_is_healthy(client, cfg, source):
    run_release(client, source, *span(source), cfg)
    visits, pv, _ = totals(client, cfg)
    assert visits == 5 * (20 + 6 + 24) and pv > 0
    assert check(client, cfg)["ok"]
    assert [e for _, e, _ in journal_events(client, cfg.database)].count("exchanged") == 3


def test_rejected_release_leaves_production_untouched(client, cfg, source):
    run_release(client, source, *span(source), cfg)
    before = totals(client, cfg)

    class Duplicating:
        def fetch(self, a, b):
            v, h = source.fetch(a, b)
            return v + v[:1], h  # duplicate visit_id -> dbt unique test must fail

    with pytest.raises(ReleaseRejected):
        run_release(client, Duplicating(), *span(source), cfg)
    assert totals(client, cfg) == before
    events = [e for _, e, _ in journal_events(client, cfg.database)]
    assert "rejected" in events
    leftovers = [e for e in ("build_", "stage_") for r in {x for x, _, _ in journal_events(client, cfg.database)}
                 if client.query("SELECT count() FROM system.databases WHERE name = {n:String}",
                                 parameters={"n": e + r}).result_rows[0][0]]
    assert leftovers == []  # a rejected release cleans up after itself
    assert check(client, cfg)["ok"]  # the lock was released


def test_rollback_restores_previous_release(client, cfg, source, other_source):
    run_release(client, source, *span(source), cfg)
    first = totals(client, cfg)
    run_release(client, other_source, *span(other_source), cfg)
    assert totals(client, cfg) != first
    rollback(client, cfg)
    assert totals(client, cfg) == first


def test_first_release_cannot_be_rolled_back(client, cfg, source):
    run_release(client, source, *span(source), cfg)
    with pytest.raises(RuntimeError):
        rollback(client, cfg)


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_interrupted_switch_is_recovered_without_swapping_back(client, cfg, source, other_source, crash_after):
    run_release(client, source, *span(source), cfg)
    with pytest.raises(SimulatedCrash):
        run_release(client, other_source, *span(other_source), cfg, fail_after_step=crash_after)

    status = check(client, cfg)
    assert not status["ok"] and "interrupted" in " ".join(status["problems"])
    with pytest.raises(PipelineBusy):  # nobody may write while the switch is uncertain
        run_release(client, other_source, *span(other_source), cfg)

    recover(client, cfg)
    recovered = totals(client, cfg)
    assert check(client, cfg)["ok"]
    # visits table and mart are from the same release: pageviews in mart == pageview hits
    pv_hits = client.query(
        f"SELECT countIf(hit_type='pageview') FROM `{cfg.database}`.hits"
    ).result_rows[0][0]
    assert recovered[1] == pv_hits
    # and recovery is idempotent
    assert recover(client, cfg) is None
    assert totals(client, cfg) == recovered


def test_lock_is_exclusive(client, cfg):
    from release_pipeline.release import ensure_schema
    ensure_schema(client, cfg.database)
    with PipelineLock(client, cfg.database):
        with pytest.raises(PipelineBusy):
            with PipelineLock(client, cfg.database):
                pass


def test_reconcile_detects_short_export_and_repair_fixes_it(client, cfg, source):
    for d in source.days()[-2:]:
        source.dropped_hits[d] = 3
    run_release(client, source, *span(source), cfg)
    rows = reconcile(client, source, *span(source), cfg)
    bad = [date.fromisoformat(r["date"]) for r in rows if not r["ok"]]
    assert len(bad) == 2
    repair(client, source, bad, cfg)
    assert all(r["ok"] for r in reconcile(client, source, *span(source), cfg))


def test_health_flags_stale_publish(client, cfg, source):
    run_release(client, source, *span(source), cfg)
    assert check(client, cfg)["ok"]
    res = check(client, cfg, extra_age_hours=30)
    assert not res["ok"] and "limit 26 h" in res["problems"][0]


def test_bot_token_is_redacted():
    token = "123456789" + ":" + "FAKE" + "x" * 31  # built at runtime: not a real token
    assert token not in redact(f"HTTPError for https://api.telegram.org/bot{token}/sendMessage", token)
    assert token not in redact(f"leaked {token} somewhere")
