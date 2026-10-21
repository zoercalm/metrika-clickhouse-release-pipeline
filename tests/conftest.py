import uuid

import pytest

from release_pipeline.config import Config, connect
from release_pipeline.release import journal_events
from release_pipeline.source import SyntheticSource


@pytest.fixture(scope="session")
def client():
    try:
        c = connect()
        c.command("SELECT 1")
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"ClickHouse is not reachable: {exc}")
    return c


@pytest.fixture
def cfg(client):
    c = Config(database="t_" + uuid.uuid4().hex[:8])
    yield c
    try:
        rids = {r for r, _, _ in journal_events(client, c.database)}
    except Exception:
        rids = set()
    for r in rids:
        for prefix in ("prev_", "build_", "stage_"):
            client.command(f"DROP DATABASE IF EXISTS `{prefix}{r}` SYNC")
    client.command(f"DROP DATABASE IF EXISTS `{c.database}` SYNC")


@pytest.fixture
def source():
    return SyntheticSource(days_back=5, visits_per_day=20)


@pytest.fixture
def other_source():
    return SyntheticSource(days_back=5, visits_per_day=20, seed=7)


def totals(client, cfg):
    return client.query(
        f"SELECT count(), sum(pageviews), sum(client_id) FROM `{cfg.database}`.visits_hits_flat"
    ).result_rows[0]
