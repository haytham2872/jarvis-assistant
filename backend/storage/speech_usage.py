"""Characters sent to Google's voice each local month (docs/archive/MOBILE-APP-PLAN.md, M3).

Chirp 3 HD is free up to 1,000,000 characters a month, then $30 per million (Google's
pricing page, 14 October 2026). The phone's spoken answers stop asking for it before the
free allowance ends; the app then reads with the iPhone's own voice.

Characters are reserved before the call and given back if it fails, in one conditional
UPDATE, so two clips synthesised at once cannot both slip past the limit. Same pattern as
mobile_requests.py: the table is created by its module.
"""
from __future__ import annotations

import logging
from datetime import datetime

from backend.storage import lifelog, operations

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS speech_usage (
    month  TEXT PRIMARY KEY,      -- local month, '2026-10'
    chars  INTEGER NOT NULL
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
        log.warning("speech_usage: schema unavailable (%s)", exc)
        return None


def month(now: datetime | None = None) -> str:
    return (now or datetime.now(lifelog.LOCAL_TZ)).astimezone(lifelog.LOCAL_TZ).strftime("%Y-%m")


def reserve(chars: int, limit: int, now: datetime | None = None) -> bool:
    """Count `chars` against this month if that stays within `limit`. False when it would
    not, and False when storage is unavailable: an uncounted call could go past the free
    allowance unseen, so the phone's own voice reads instead."""
    conn = _conn()
    if conn is None:
        return False
    key = month(now)
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO speech_usage (month, chars) VALUES (?, 0)", (key,))
            cur = conn.execute(
                "UPDATE speech_usage SET chars = chars + ? WHERE month = ? AND chars + ? <= ?",
                (chars, key, chars, limit))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("speech_usage: reserve failed (%s)", exc)
        return False


def refund(chars: int, now: datetime | None = None) -> None:
    """Give back a reservation whose call failed (Google does not bill a failed request)."""
    conn = _conn()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("UPDATE speech_usage SET chars = MAX(0, chars - ?) WHERE month = ?",
                         (chars, month(now)))
    except Exception as exc:
        log.warning("speech_usage: refund failed (%s)", exc)


def used(now: datetime | None = None) -> int:
    conn = _conn()
    if conn is None:
        return 0
    try:
        row = conn.execute("SELECT chars FROM speech_usage WHERE month = ?", (month(now),)).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
