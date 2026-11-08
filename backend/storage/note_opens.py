"""Which notes the phone opened: one row per note per local day (the first open that day).

Same pattern as mobile_requests.py: the table is created by its module, not by a schema migration.
"""
from __future__ import annotations

import logging

from backend.storage import lifelog, operations

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_opens (
    folder     TEXT NOT NULL,
    name       TEXT NOT NULL,
    local_date TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (folder, name, local_date)
);
"""


def _conn():
    conn = operations._ensure()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("note_opens: schema unavailable (%s)", exc)
        return None


def record(folder: str, name: str, now: float | None = None) -> None:
    conn = _conn()
    if conn is None:
        return
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO note_opens (folder, name, local_date, ts) VALUES (?,?,?,?)",
                         (folder, name, day, ts))
    except Exception as exc:
        log.warning("note_opens: not stored (%s)", exc)


def last_opened(folder: str) -> dict[str, float]:
    conn = _conn()
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT name, MAX(ts) FROM note_opens WHERE folder=? GROUP BY name", (folder,))
        return {name: ts for name, ts in rows}
    except Exception as exc:
        log.warning("note_opens: not read (%s)", exc)
        return {}


def first_opened() -> float | None:
    conn = _conn()
    if conn is None:
        return None
    try:
        return conn.execute("SELECT MIN(ts) FROM note_opens").fetchone()[0]
    except Exception as exc:
        log.warning("note_opens: not read (%s)", exc)
        return None
