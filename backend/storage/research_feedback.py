"""The owner's thumbs up/down on research findings (phone Inbox), kept for the research loop.

Same pattern as note_opens.py: the table is created by its module, not by a schema migration.
A finding is a notice of kind "research" whose key is "research:<subject>@<unix seconds>".
"""
from __future__ import annotations

import logging

from backend.storage import lifelog, operations

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_feedback (
    notice_id  INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,
    vote       TEXT NOT NULL CHECK (vote IN ('up','down')),
    ts         REAL NOT NULL,
    local_date TEXT NOT NULL
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
        log.warning("research_feedback: schema unavailable (%s)", exc)
        return None


def _subject(key) -> str | None:
    if not isinstance(key, str) or not key.startswith("research:") or "@" not in key:
        return None
    subject = key[len("research:"):].rsplit("@", 1)[0].strip()
    return subject or None


def record(notice_id: int, vote: str, now: float | None = None) -> dict | None:
    """Store (latest wins) or, for "none", remove the owner's vote on a research finding."""
    if vote not in ("up", "down", "none"):
        return None
    try:
        conn = _conn()
        if conn is None:
            return None
        row = conn.execute("SELECT kind, key FROM notices WHERE id=?", (int(notice_id),)).fetchone()
        subject = _subject(row[1]) if row and row[0] == "research" else None
        if subject is None:
            return None
        ts, day = lifelog.stamp(now)
        with conn:
            if vote == "none":
                conn.execute("DELETE FROM research_feedback WHERE notice_id=?", (int(notice_id),))
            else:
                conn.execute("INSERT OR REPLACE INTO research_feedback (notice_id, subject, vote, ts, local_date) "
                             "VALUES (?,?,?,?,?)", (int(notice_id), subject, vote, ts, day))
        return {"notice_id": int(notice_id), "subject": subject, "vote": vote}
    except Exception as exc:
        log.warning("research_feedback: not stored (%s)", exc)
        return None


def votes(since_ts: float) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT notice_id, subject, vote, ts FROM research_feedback WHERE ts >= ? "
                            "ORDER BY ts", (since_ts,))
        return [{"notice_id": i, "subject": s, "vote": v, "ts": t} for i, s, v, t in rows]
    except Exception as exc:
        log.warning("research_feedback: not read (%s)", exc)
        return []


def vote_for(notice_ids) -> dict[int, str]:
    ids = [int(i) for i in notice_ids]
    conn = _conn() if ids else None
    if conn is None:
        return {}
    try:
        marks = ",".join("?" * len(ids))
        return {i: v for i, v in conn.execute(
            f"SELECT notice_id, vote FROM research_feedback WHERE notice_id IN ({marks})", ids)}
    except Exception as exc:
        log.warning("research_feedback: not read (%s)", exc)
        return {}
