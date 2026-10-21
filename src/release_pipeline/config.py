import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str = os.getenv("CH_HOST", "localhost")
    port: int = int(os.getenv("CH_PORT", "18123"))
    user: str = os.getenv("CH_USER", "demo")
    password: str = os.getenv("CH_PASSWORD", "demo")
    database: str = os.getenv("CH_DATABASE", "analytics")  # the "public" database
    max_age_hours: int = int(os.getenv("HEALTH_MAX_AGE_HOURS", "26"))


def connect(cfg: Config | None = None):
    import clickhouse_connect

    cfg = cfg or Config()
    return clickhouse_connect.get_client(
        host=cfg.host, port=cfg.port, username=cfg.user, password=cfg.password
    )
