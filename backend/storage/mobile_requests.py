"""What became of each message the phone sent (docs/archive/MOBILE-APP-PLAN.md, M1).

The conversation inbox (inbox.py) already holds the message durably and its state while it
waits or runs. This table adds only what the phone needs on top: the request id the phone
generated (so a retry after a lost acknowledgement finds the original instead of running
the request twice), a hash of the text (so the same id with different text is refused),
the run that answered it, and the final answer as it was delivered.

Same pattern as delivery.py: the table is created by its module, not by a schema migration.
"""
from __future__ import annotations

import hashlib
import logging
import time

from backend.storage import operations

log = logging.getLogger(__name__)

ANSWERED, FAILED, INTERRUPTED, CANCELLED = "answered", "failed", "interrupted", "cancelled"
NEW, SAME, CONFLICT, UNSTORED = "new", "same", "conflict", "unstored"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_requests (
    request_id   TEXT PRIMARY KEY,
    text_sha     TEXT NOT NULL,
    input_kind   TEXT NOT NULL,
    received_at  REAL NOT NULL,
    run_id       TEXT,
    answer       TEXT,
    outcome      TEXT,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_mobile_requests_received ON mobile_requests (received_at);
"""


def _conn():
    conn = operations._ensure()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("mobile_requests: schema unavailable (%s)", exc)
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def record(request_id: str, text: str, input_kind: str) -> str:
    """NEW for a first sight, SAME for a retry of the same message, CONFLICT for the same id
    with different text, UNSTORED when storage is unavailable (nothing may be acknowledged)."""
    conn = _conn()
    if conn is None:
        return UNSTORED
    sha = _sha(text)
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO mobile_requests (request_id, text_sha, input_kind, received_at)"
                " VALUES (?,?,?,?)", (request_id, sha, input_kind, time.time()))
            if cur.rowcount:
                return NEW
            row = conn.execute("SELECT text_sha FROM mobile_requests WHERE request_id=?",
                               (request_id,)).fetchone()
        return SAME if row and row[0] == sha else CONFLICT
    except Exception as exc:
        log.warning("mobile_requests: not stored (%s)", exc)
        return UNSTORED


def get(request_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    row = conn.execute("SELECT * FROM mobile_requests WHERE request_id=?", (request_id,)).fetchone()
    return dict(row) if row else None


def bind_run(request_id: str, run_id: str) -> None:
    conn = _conn()
    if conn is None:
        return
    with conn:
        conn.execute("UPDATE mobile_requests SET run_id=? WHERE request_id=? AND outcome IS NULL",
                     (run_id, request_id))


def finish(request_id: str, outcome: str, answer: str | None = None) -> bool:
    """Record the final outcome once; a second finish never overwrites the first."""
    conn = _conn()
    if conn is None:
        return False
    with conn:
        cur = conn.execute(
            "UPDATE mobile_requests SET outcome=?, answer=?, finished_at=? WHERE request_id=?"
            " AND outcome IS NULL", (outcome, answer, time.time(), request_id))
    return bool(cur.rowcount)


def recent(limit: int = 50) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = conn.execute("SELECT * FROM mobile_requests ORDER BY received_at DESC LIMIT ?",
                        (limit,)).fetchall()
    return [dict(r) for r in rows]


def forget(request_id: str) -> None:
    """Drop a request that was never acknowledged (its inbox insert failed), so a retry of
    the same id is treated as new rather than as a duplicate of nothing."""
    conn = _conn()
    if conn is None:
        return
    with conn:
        conn.execute("DELETE FROM mobile_requests WHERE request_id=? AND outcome IS NULL", (request_id,))
