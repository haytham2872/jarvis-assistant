"""Chunk receipts for replies that must survive a Discord reconnect or restart.

Only a confirmed send marks a chunk sent. A process death or network exception
after an attempt leaves that chunk uncertain, because Discord might have accepted
it even when the caller did not receive the response. Confirmed chunks are never
resent; pending chunks can be resumed safely.
"""
from __future__ import annotations

import logging
import time

from backend.storage import operations

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_parts (
    intent_id    TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    body         TEXT NOT NULL,
    state        TEXT NOT NULL,
    remote_id    TEXT,
    started_at   REAL,
    sent_at      REAL,
    error        TEXT,
    PRIMARY KEY (intent_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_delivery_parts_state
    ON delivery_parts (state, intent_id);
"""


def _conn():
    conn = operations._ensure()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("delivery: receipt table unavailable (%s)", exc)
        return None


def _load(conn, intent_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM delivery_intents WHERE intent_id=?", (intent_id,),
    ).fetchone()
    if row is None:
        return None
    parts = conn.execute(
        "SELECT * FROM delivery_parts WHERE intent_id=? ORDER BY ordinal", (intent_id,),
    ).fetchall()
    return {**dict(row), "parts": [dict(p) for p in parts]}


def get(intent_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    try:
        return _load(conn, intent_id)
    except Exception as exc:
        log.debug("delivery: read failed for %s (%s)", intent_id, exc)
        return None


def prepare(intent_id: str, channel: str, body: str, parts: list[str]) -> dict | None:
    """Persist a reply before sending. Reject a key reused with different content."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn:
            row = _load(conn, intent_id)
            if row is None:
                conn.execute(
                    "INSERT INTO delivery_intents"
                    " (intent_id, channel, state, body, created_at)"
                    " VALUES (?, ?, 'pending', ?, ?)",
                    (intent_id, channel, body, time.time()),
                )
                conn.executemany(
                    "INSERT INTO delivery_parts (intent_id, ordinal, body, state)"
                    " VALUES (?, ?, ?, 'pending')",
                    [(intent_id, i, part) for i, part in enumerate(parts)],
                )
            elif row["channel"] != channel or row["body"] != body:
                log.error("delivery: intent %s reused with different content", intent_id)
                return None
        return _load(conn, intent_id)
    except Exception as exc:
        log.warning("delivery: could not prepare %s (%s)", intent_id, exc)
        return None


def claim_part(intent_id: str, ordinal: int) -> bool:
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE delivery_parts SET state='sending', started_at=?"
                " WHERE intent_id=? AND ordinal=? AND state='pending'",
                (time.time(), intent_id, ordinal),
            )
            if cur.rowcount == 1:
                conn.execute(
                    "UPDATE delivery_intents SET state='sending' WHERE intent_id=?",
                    (intent_id,),
                )
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("delivery: claim failed for %s/%d (%s)", intent_id, ordinal, exc)
        return False


def mark_sent(intent_id: str, ordinal: int, remote_id: str = "") -> bool:
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE delivery_parts SET state='sent', remote_id=?, sent_at=?, error=NULL"
                " WHERE intent_id=? AND ordinal=? AND state='sending'",
                (remote_id, time.time(), intent_id, ordinal),
            )
            if cur.rowcount != 1:
                return False
            remaining = conn.execute(
                "SELECT COUNT(*) FROM delivery_parts WHERE intent_id=? AND state!='sent'",
                (intent_id,),
            ).fetchone()[0]
            if remaining:
                conn.execute(
                    "UPDATE delivery_intents SET state='partial' WHERE intent_id=?",
                    (intent_id,),
                )
            else:
                conn.execute(
                    "UPDATE delivery_intents SET state='sent', sent_at=?, error=NULL"
                    " WHERE intent_id=?",
                    (time.time(), intent_id),
                )
        return True
    except Exception as exc:
        log.warning("delivery: sent receipt failed for %s/%d (%s)", intent_id, ordinal, exc)
        return False


def mark_uncertain(intent_id: str, ordinal: int, error: str) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "UPDATE delivery_parts SET state='uncertain', error=?"
                " WHERE intent_id=? AND ordinal=? AND state='sending'",
                (error[:500], intent_id, ordinal),
            )
            conn.execute(
                "UPDATE delivery_intents SET state='uncertain', error=? WHERE intent_id=?",
                (error[:500], intent_id),
            )
    except Exception as exc:
        log.warning("delivery: uncertainty receipt failed for %s/%d (%s)", intent_id, ordinal, exc)


def reconcile_inflight() -> int:
    """An attempt left open by a dead process is uncertain, never retryable."""
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE delivery_parts SET state='uncertain',"
                " error='Process stopped during send' WHERE state='sending'"
            )
            conn.execute(
                "UPDATE delivery_intents SET state='uncertain',"
                " error='Process stopped during send'"
                " WHERE intent_id IN"
                " (SELECT intent_id FROM delivery_parts WHERE state='uncertain')"
            )
        return cur.rowcount
    except Exception as exc:
        log.warning("delivery: startup reconciliation failed (%s)", exc)
        return 0


def resumable() -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT intent_id FROM delivery_intents"
            " WHERE state IN ('pending', 'partial') ORDER BY created_at",
        ).fetchall()
        return [_load(conn, row["intent_id"]) for row in rows]
    except Exception as exc:
        log.debug("delivery: pending read failed (%s)", exc)
        return []


def counts() -> dict[str, int]:
    conn = _conn()
    if conn is None:
        return {}
    try:
        return {row["state"]: row["n"] for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM delivery_intents GROUP BY state"
        ).fetchall()}
    except Exception:
        return {}
