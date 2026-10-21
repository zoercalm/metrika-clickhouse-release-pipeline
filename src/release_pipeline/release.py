"""Whole-release publishing.

New data never lands on the tables a dashboard reads. A release is:

    stage -> full build in a separate database -> dbt build + tests -> journal -> switch

The switch is NOT one transaction (three EXCHANGE TABLES statements), so every step is
journaled and `recover` compares table UUIDs and performs only the missing exchanges.
The lock is a plain table: whoever created it owns it, there is no TTL and no lease
stealing. An interrupted publisher keeps the lock until a human or `recover` clears it.
"""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .sql import HITS_DDL, TABLES, VISITS_DDL, ident, table_uuid

DBT_DIR = Path(os.getenv("RP_DBT_DIR", Path(__file__).resolve().parents[2] / "dbt"))
LOCK_TABLE = "etl_pipeline_lock"


class PipelineBusy(RuntimeError):
    """Another writer holds the lock."""


class ReleaseRejected(RuntimeError):
    """The dbt build or its tests failed. Production was not touched."""


class PublicationUncertain(RuntimeError):
    """The switch was interrupted. Run `rp recover`; do not retry blindly."""


class SimulatedCrash(RuntimeError):
    """Raised by tests and demos to interrupt a switch half-way."""


# ---------------------------------------------------------------- schema ----

def ensure_schema(client, db: str) -> None:
    client.command(f"CREATE DATABASE IF NOT EXISTS {ident(db)} ENGINE = Atomic")
    client.command(
        f"CREATE TABLE IF NOT EXISTS {ident(db)}.release_journal "
        "(release_id String, ts DateTime64(3), event String, detail String) "
        "ENGINE = MergeTree ORDER BY (ts, release_id)"
    )
    client.command(
        f"CREATE TABLE IF NOT EXISTS {ident(db)}.release_health "
        "(release_id String, published_at DateTime, visits UInt64, hits UInt64, "
        "max_date Date, status String) ENGINE = MergeTree ORDER BY published_at"
    )


def journal(client, db: str, release_id: str, event: str, detail: dict | None = None) -> None:
    client.insert(
        f"{db}.release_journal",
        [[release_id, datetime.now(timezone.utc).replace(tzinfo=None), event, json.dumps(detail or {})]],
        column_names=["release_id", "ts", "event", "detail"],
    )


def journal_events(client, db: str, release_id: str | None = None):
    where = "WHERE release_id = {r:String}" if release_id else ""
    rows = client.query(
        f"SELECT release_id, event, detail FROM {ident(db)}.release_journal {where} ORDER BY ts",
        parameters={"r": release_id} if release_id else None,
    ).result_rows
    return [(r, e, json.loads(d)) for r, e, d in rows]


# ------------------------------------------------------------------ lock ----

class PipelineLock:
    """Exclusive table name. Creation succeeds for exactly one writer."""

    def __init__(self, client, db: str):
        self.client, self.db = client, db
        self.name = f"{ident(db)}.{ident(LOCK_TABLE)}"
        self.keep = False

    def __enter__(self):
        try:
            self.client.command(f"CREATE TABLE {self.name} (unused UInt8) ENGINE = TinyLog")
        except Exception as exc:
            if "already exists" in str(exc).lower() or "TABLE_ALREADY_EXISTS" in str(exc):
                raise PipelineBusy("another writer holds the lock (or a publish was interrupted)") from exc
            raise
        return self

    def __exit__(self, *exc):
        if not self.keep:
            self.client.command(f"DROP TABLE IF EXISTS {self.name}")
        return False


def lock_exists(client, db: str) -> bool:
    return table_uuid(client, db, LOCK_TABLE) is not None


# ------------------------------------------------------------------- dbt ----

def run_dbt(build_db: str, cfg: Config) -> tuple[bool, str]:
    env = {
        **os.environ,
        "DBT_SCHEMA": build_db,
        "CH_HOST": cfg.host, "CH_PORT": str(cfg.port),
        "CH_USER": cfg.user, "CH_PASSWORD": cfg.password,
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    exe = Path(sys.executable).parent / "dbt"
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [str(exe), "build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
             "--target-path", tmp, "--log-path", tmp],
            env=env, capture_output=True, text=True,
        )
    out = proc.stdout + proc.stderr
    return proc.returncode == 0, out


# --------------------------------------------------------------- release ----

def _copy_history(client, prod: str, build: str, table: str, d_from: date, d_to: date) -> None:
    """Copy everything outside the reloaded range from production into the build database."""
    if table_uuid(client, prod, table):
        client.command(
            f"INSERT INTO {ident(build)}.{ident(table)} SELECT * FROM {ident(prod)}.{ident(table)} "
            "WHERE date < {a:Date} OR date > {b:Date}",
            parameters={"a": d_from, "b": d_to},
        )


def _drop_db(client, db: str) -> None:
    client.command(f"DROP DATABASE IF EXISTS {ident(db)} SYNC")


def _insert(client, db: str, table: str, rows) -> None:
    if rows:
        client.insert(f"{db}.{table}", rows)


def _finish(client, cfg: Config, release_id: str, build: str, recovered: bool = False) -> None:
    db = cfg.database
    # The server clock stamps the snapshot, so the freshness check never mixes time zones.
    client.command(
        f"INSERT INTO {ident(db)}.release_health "
        f"SELECT {{r:String}}, now(), (SELECT count() FROM {ident(db)}.visits), "
        f"(SELECT count() FROM {ident(db)}.hits), (SELECT max(date) FROM {ident(db)}.visits), 'published'",
        parameters={"r": release_id},
    )
    journal(client, db, release_id, "published", {"recovered": recovered})
    # The build database now holds the previous versions: keep exactly one for rollback.
    for other in {r for r, e, _ in journal_events(client, db) if e == "prepared"} - {release_id}:
        _drop_db(client, "prev_" + other)
    if client.query(
        "SELECT count() FROM system.tables WHERE database = {d:String}", parameters={"d": build}
    ).result_rows[0][0]:
        client.command(f"RENAME DATABASE {ident(build)} TO {ident('prev_' + release_id)}")
    else:
        _drop_db(client, build)  # first release: nothing to roll back to


def run_release(client, source, d_from: date, d_to: date, cfg: Config | None = None,
                *, fail_after_step: int | None = None) -> str:
    cfg = cfg or Config()
    db = cfg.database
    ensure_schema(client, db)
    rid = uuid.uuid4().hex[:12]
    stage, build = f"stage_{rid}", f"build_{rid}"

    with PipelineLock(client, db) as lock:
        journal(client, db, rid, "started", {"from": str(d_from), "to": str(d_to)})
        try:
            # 1. stage the new range
            for name in (stage, build):
                client.command(f"CREATE DATABASE {ident(name)} ENGINE = Atomic")
            client.command(VISITS_DDL.format(db=ident(stage)))
            client.command(HITS_DDL.format(db=ident(stage)))
            visits, hits = source.fetch(d_from, d_to)
            _insert(client, stage, "visits", visits)
            _insert(client, stage, "hits", hits)
            # 2. full build: history outside the range + the staged range
            client.command(VISITS_DDL.format(db=ident(build)))
            client.command(HITS_DDL.format(db=ident(build)))
            for t in ("visits", "hits"):
                _copy_history(client, db, build, t, d_from, d_to)
                client.command(f"INSERT INTO {ident(build)}.{t} SELECT * FROM {ident(stage)}.{t}")
            # 3. dbt build + tests in the build database
            ok, out = run_dbt(build, cfg)
            if not ok:
                journal(client, db, rid, "rejected", {"dbt_tail": out[-1500:]})
                raise ReleaseRejected("dbt build/tests failed; production untouched\n" + out[-1500:])
        except Exception:
            _drop_db(client, stage)
            _drop_db(client, build)
            raise
        _drop_db(client, stage)

        # 4. plan and journal before touching production
        plan = [
            {"table": t, "old_uuid": table_uuid(client, db, t), "new_uuid": table_uuid(client, build, t)}
            for t in TABLES
        ]
        journal(client, db, rid, "prepared", {"plan": plan, "build": build})

        # 5. switch. From here on an interruption means "uncertain": keep the lock.
        lock.keep = True
        for i, step in enumerate(plan, 1):
            _apply_step(client, db, build, step)
            journal(client, db, rid, "exchanged", {"table": step["table"], "step": i})
            if fail_after_step == i:
                raise SimulatedCrash(f"simulated crash after step {i}")
        _finish(client, cfg, rid, build)
        lock.keep = False
    return rid


def _apply_step(client, db: str, build: str, step: dict) -> None:
    t = step["table"]
    if step["old_uuid"] is None:
        client.command(f"RENAME TABLE {ident(build)}.{ident(t)} TO {ident(db)}.{ident(t)}")
    else:
        client.command(f"EXCHANGE TABLES {ident(db)}.{ident(t)} AND {ident(build)}.{ident(t)}")


# -------------------------------------------------------------- recovery ----

def _open_release(client, db: str):
    done, plans = set(), {}
    for rid, event, detail in journal_events(client, db):
        if event == "prepared":
            plans[rid] = detail
        elif event in ("published", "rejected", "rolled_back", "aborted"):
            done.add(rid)
    pending = [r for r in plans if r not in done]
    return (pending[-1], plans[pending[-1]]) if pending else (None, None)


def recover(client, cfg: Config | None = None, *, writer_stopped: bool = False) -> str | None:
    """Finish an interrupted switch by comparing UUIDs. Never re-exchanges a finished step."""
    cfg = cfg or Config()
    db = cfg.database
    rid, detail = _open_release(client, db)
    if rid is None:
        if lock_exists(client, db):
            raise PipelineBusy("lock held without a recovery plan: inspect the writer; do not clear automatically")
        return None
    if not writer_stopped:
        raise PipelineBusy("stop the writer first, then explicitly confirm writer_stopped for recovery")
    build = detail["build"]
    for step in detail["plan"]:
        current = table_uuid(client, db, step["table"])
        if current == step["new_uuid"]:
            continue  # already switched
        if current != step["old_uuid"]:
            raise PublicationUncertain(
                f"{step['table']}: UUID is neither the old nor the new one; manual inspection needed"
            )
        _apply_step(client, db, build, step)
        journal(client, db, rid, "exchanged", {"table": step["table"], "recovered": True})
    _finish(client, cfg, rid, build, recovered=True)
    client.command(f"DROP TABLE IF EXISTS {ident(db)}.{ident(LOCK_TABLE)}")
    return rid


def rollback(client, cfg: Config | None = None) -> str:
    """Switch back to the previous release (kept in the prev_* database)."""
    cfg = cfg or Config()
    db = cfg.database
    published = [(r, d) for r, e, d in journal_events(client, db) if e == "prepared"]
    done = {r for r, e, _ in journal_events(client, db) if e == "published"}
    candidates = [(r, d) for r, d in published if r in done]
    if not candidates:
        raise RuntimeError("no published release to roll back")
    rid, detail = candidates[-1]
    prev = f"prev_{rid}"
    if not client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}", parameters={"n": prev}
    ).result_rows[0][0]:
        raise RuntimeError("no previous version kept (first release or already rolled back)")
    with PipelineLock(client, db):
        for step in detail["plan"]:
            if step["old_uuid"] is None:
                raise RuntimeError("the first release has nothing to roll back to")
        for step in detail["plan"]:
            if table_uuid(client, db, step["table"]) == step["new_uuid"]:
                client.command(f"EXCHANGE TABLES {ident(db)}.{ident(step['table'])} AND {ident(prev)}.{ident(step['table'])}")
        journal(client, db, rid, "rolled_back", {})
    return rid
