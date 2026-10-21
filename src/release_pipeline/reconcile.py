"""Compare the published data with an EXTERNAL source (the Reporting API stand-in).

A test that joins the mart to raw cannot see rows that never reached raw. This does.
"""
from datetime import date, timedelta

from .config import Config
from .release import run_release
from .sql import ident


def reconcile(client, source, d_from: date, d_to: date, cfg: Config | None = None) -> list[dict]:
    cfg = cfg or Config()
    db = ident(cfg.database)
    rows = []
    d = d_from
    while d <= d_to:
        want = source.report_totals(d)
        got_v = client.query(
            f"SELECT count() FROM {db}.visits WHERE date = {{d:Date}}", parameters={"d": d}
        ).result_rows[0][0]
        got_pv = client.query(
            f"SELECT countIf(hit_type = 'pageview') FROM {db}.hits WHERE date = {{d:Date}}", parameters={"d": d}
        ).result_rows[0][0]
        rows.append({
            "date": str(d), "visits_source": want["visits"], "visits_published": got_v,
            "pageviews_source": want["pageviews"], "pageviews_published": got_pv,
            "ok": want["visits"] == got_v and want["pageviews"] == got_pv,
        })
        d += timedelta(days=1)
    return rows


class _CompleteSource:
    """Same source, but a full export: used to repair days that came back short."""

    def __init__(self, source):
        self._s = source

    def fetch(self, a, b):
        return self._s.fetch_complete(a, b)


def repair(client, source, bad_days: list[date], cfg: Config | None = None) -> str | None:
    """Reload the affected range as a normal release, so the repair passes the same gate."""
    if not bad_days:
        return None
    return run_release(client, _CompleteSource(source), min(bad_days), max(bad_days), cfg)
