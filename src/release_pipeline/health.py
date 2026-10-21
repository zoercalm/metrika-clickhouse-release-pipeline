"""Independent freshness check: it reads only the health snapshot, not the pipeline."""
from .config import Config
from .release import lock_exists
from .sql import ident


def check(client, cfg: Config | None = None, extra_age_hours: float = 0.0) -> dict:
    """`extra_age_hours` lets tests and demos simulate time passing."""
    cfg = cfg or Config()
    rows = client.query(
        "SELECT release_id, dateDiff('second', published_at, now()) / 3600, visits, max_date "
        f"FROM {ident(cfg.database)}.release_health ORDER BY published_at DESC LIMIT 1"
    ).result_rows
    if not rows:
        return {"ok": False, "problems": ["no release has been published yet"]}
    rid, age_h, visits, max_date = rows[0]
    age_h += extra_age_hours
    problems = []
    if age_h > cfg.max_age_hours:
        problems.append(f"last successful publish was {age_h:.1f} h ago (limit {cfg.max_age_hours} h)")
    if lock_exists(client, cfg.database):
        problems.append("lock is held: a publish may have been interrupted (run `rp recover`)")
    return {"ok": not problems, "release_id": rid, "age_hours": round(age_h, 2),
            "visits": visits, "max_date": str(max_date), "problems": problems}
