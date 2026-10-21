"""A synthetic stand-in for the Logs API and the Reporting API.

Everything is deterministic: a date always yields the same rows, whatever range
is requested, so reloads and reconciliation behave like a stable source.
"""
import random
from datetime import date, datetime, timedelta

DEVICES = ("desktop", "mobile", "tablet")
SOURCES = ("organic", "direct", "referral", "ad", "social")
PATHS = ("/", "/catalog", "/product/1", "/product/2", "/cart", "/contacts", "/blog/a", "/blog/b")


def _day_rows(counter_id: int, day: date, seed: int, visits_per_day: int):
    rng = random.Random(f"{seed}:{counter_id}:{day.isoformat()}")
    base = day.toordinal() * 1_000_000 + counter_id * 10_000
    visits, hits = [], []
    hit_seq = 0

    def add_visit(i, client_id, is_new, subnet, duration, pages, rng_local):
        nonlocal hit_seq
        vid = base + i
        start = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng_local.randint(0, 86399))
        visits.append((
            vid, counter_id, client_id, day, start, is_new,
            rng_local.choice(DEVICES), rng_local.choice(SOURCES), subnet, duration, pages,
        ))
        for p in range(pages):
            hit_seq += 1
            hits.append((
                base + hit_seq, vid, day, start + timedelta(seconds=p * 7),
                "pageview", rng_local.choice(PATHS),
            ))
        if rng_local.random() < 0.3:
            hit_seq += 1
            hits.append((base + hit_seq, vid, day, start + timedelta(seconds=pages * 7 + 1), "event", "/cart"))

    n = 0
    for _ in range(visits_per_day):
        n += 1
        add_visit(
            n, rng.randint(1, 5_000) + counter_id * 100_000, int(rng.random() < 0.55),
            f"10.{rng.randint(0, 40)}.{rng.randint(0, 255)}" if rng.random() > 0.05 else "",
            rng.randint(5, 600), rng.randint(1, 6), rng,
        )
    # One cookie that floods the counter with "new" visits (rule 1 should flag it).
    robot_client = 900_000_000 + counter_id
    for _ in range(6):
        n += 1
        add_visit(n, robot_client, 1, "10.250.1", rng.randint(1, 4), 1, rng)
    # A /24 subnet farm of short single-page new visits (rule 2 should flag it).
    for k in range(24):
        n += 1
        add_visit(n, 800_000_000 + k, 1, "10.251.7", rng.randint(1, 8), 1, rng)
    return visits, hits


class SyntheticSource:
    def __init__(self, counters=(1001,), days_back=14, seed=42, visits_per_day=60, end=None):
        self.counters = tuple(counters)
        self.seed = seed
        self.visits_per_day = visits_per_day
        self.end = end or date(2026, 1, 31)
        self.days_back = days_back
        self.dropped_hits: dict[date, int] = {}  # fault injection for demos and tests

    def days(self):
        return [self.end - timedelta(days=i) for i in range(self.days_back - 1, -1, -1)]

    def fetch(self, date_from: date, date_to: date):
        """Logs API stand-in: rows for the inclusive range. Optionally loses hits."""
        visits, hits = [], []
        d = date_from
        while d <= date_to:
            for c in self.counters:
                v, h = _day_rows(c, d, self.seed, self.visits_per_day)
                drop = self.dropped_hits.get(d, 0)
                if drop:
                    h = h[:-drop]
                visits += v
                hits += h
            d += timedelta(days=1)
        return visits, hits

    def fetch_complete(self, date_from: date, date_to: date):
        saved, self.dropped_hits = self.dropped_hits, {}
        try:
            return self.fetch(date_from, date_to)
        finally:
            self.dropped_hits = saved

    def report_totals(self, day: date):
        """Reporting API stand-in: unsampled daily totals (the external truth)."""
        visits = pageviews = 0
        for c in self.counters:
            v, h = _day_rows(c, day, self.seed, self.visits_per_day)
            visits += len(v)
            pageviews += sum(1 for r in h if r[4] == "pageview")
        return {"visits": visits, "pageviews": pageviews}


class DuplicatingSource:
    """Demo/test: a source that returns a duplicated visit_id, so dbt tests must reject the release."""

    def __init__(self, source):
        self._s = source

    def fetch(self, a, b):
        v, h = self._s.fetch(a, b)
        return v + v[:1], h
