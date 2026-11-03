"""Durable identities for bot messages a Discord user may reply to."""
from __future__ import annotations

import logging
import time

from backend.storage import db

log = logging.getLogger(__name__)


def record(message_id: str, owner_id: str, channel_id: str, kind: str,
           body: str, sent_at: float | None = None) -> bool:
    if not message_id or not owner_id or not body:
        return False
    conn = db.connect()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO discord_outgoing "
                "(message_id, owner_id, channel_id, kind, body, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(message_id), str(owner_id), str(channel_id), kind, body,
                 time.time() if sent_at is None else sent_at),
            )
        return True
    except Exception as exc:
        log.warning("Discord message record failed: %s", exc)
        return False


def get(message_id: str, owner_id: str) -> dict | None:
    conn = db.connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM discord_outgoing WHERE message_id=? AND owner_id=?",
            (str(message_id), str(owner_id)),
        ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("Discord message lookup failed: %s", exc)
        return None


def latest_substantive(owner_id: str, *, channel_id: str = "",
                       max_age_s: float = 900,
                       now: float | None = None) -> dict | None:
    """A recent research DM is implicit context only if no reply followed it."""
    conn = db.connect()
    if conn is None:
        return None
    cutoff = (time.time() if now is None else now) - max_age_s
    try:
        where_channel = " AND channel_id=?" if channel_id else ""
        params = (str(owner_id), cutoff, str(channel_id)) if channel_id else (str(owner_id), cutoff)
        row = conn.execute(
            "SELECT * FROM discord_outgoing WHERE owner_id=? "
            "AND kind IN ('research', 'reply') AND sent_at>=?"
            + where_channel + " ORDER BY sent_at DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("Discord recent message lookup failed: %s", exc)
        return None
