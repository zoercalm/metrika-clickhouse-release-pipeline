"""Small helpers shared by the pipeline modules."""
import re

TABLES = ("visits", "hits", "visits_hits_flat")  # the mart is switched last

VISITS_DDL = """
CREATE TABLE {db}.visits (
    visit_id UInt64, counter_id UInt32, client_id UInt64, date Date,
    start_ts DateTime, is_new UInt8, device String, traffic_source String,
    ip_subnet String, duration_s UInt32, page_views UInt16
) ENGINE = MergeTree ORDER BY (date, visit_id)
"""

HITS_DDL = """
CREATE TABLE {db}.hits (
    hit_id UInt64, visit_id UInt64, date Date, ts DateTime,
    hit_type String, url_path String
) ENGINE = MergeTree ORDER BY (date, hit_id)
"""


def ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
        raise ValueError(f"invalid identifier: {value!r}")
    return f"`{value}`"


def table_uuid(client, db: str, table: str):
    rows = client.query(
        "SELECT toString(uuid) FROM system.tables WHERE database = {d:String} AND name = {t:String}",
        parameters={"d": db, "t": table},
    ).result_rows
    return rows[0][0] if rows else None
