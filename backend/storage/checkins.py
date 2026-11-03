"""P4: the evening check-in record -- one row per local day.

The check-in is one short message listing the day's planned blocks; the owner answers in his own
words and each block gets an outcome (`plans.record_outcome`). This table is the record of the
conversation around it: when it was sent, what he replied, what was made of it, whether a follow-up
question was asked, when the day was closed. Life-log conventions (`lifelog.py`): every row carries
a UTC time and its local date (the primary key); each field is set once when that step happens and
never rewritten (a later reply is appended to `reply`, bounded); nothing is deleted.

    claim      the day's row is created before the message goes out, so two loop cycles or a restart
               cannot send it twice; `mark_sent` records that Discord took it.
    reply      his words (short) and what was made of them, per block.
    follow_up  the one clarifying question asked about the day, set once.
    close      the day is over: blocks nobody answered for are recorded as unknown, not guessed.

Every function returns None / False / [] instead of raising when storage is unavailable.
"""
from __future__ import annotations

import json
import logging

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

_MAX_REPLY = 800                  # his replies over a day, joined (each one is short already)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkins (
    local_date  TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    sent_at     REAL,
    replied_at  REAL,
    reply       TEXT NOT NULL DEFAULT '',
    parsed      TEXT NOT NULL DEFAULT '[]',
    follow_up   TEXT NOT NULL DEFAULT '',
    followup_at REAL,
    closed_at   REAL,
    block_uids  TEXT NOT NULL DEFAULT '[]'
);
"""


def _ensure():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: checkins unavailable (%s)", exc)
        return None


def _row(r) -> dict:
    out = dict(r)
    for key in ("parsed", "block_uids"):
        try:
            out[key] = json.loads(out.get(key) or "[]")
        except Exception:
            out[key] = []
    return out


def claim(local_date: str, block_uids: list[str], *, now: float | None = None) -> bool:
    """Create the day's row. True only for the call that created it."""
    conn = _ensure()
    if conn is None:
        return False
    ts = lifelog.stamp(now)[0]
    try:
        with conn:
            cur = conn.execute("INSERT OR IGNORE INTO checkins (local_date, created_at, block_uids) "
                               "VALUES (?, ?, ?)", (local_date, ts, json.dumps(list(block_uids))))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("storage: could not record check-in (%s)", exc)
        return False


def get(local_date: str) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM checkins WHERE local_date = ?", (local_date,)).fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def mark_sent(local_date: str, *, now: float | None = None) -> bool:
    """Discord took the message. Set once."""
    conn = _ensure()
    if conn is None:
        return False
    ts = lifelog.stamp(now)[0]
    try:
        with conn:
            cur = conn.execute("UPDATE checkins SET sent_at = ? WHERE local_date = ? AND sent_at IS NULL",
                               (ts, local_date))
        return cur.rowcount == 1
    except Exception:
        return False


def record_reply(local_date: str, said: str, parsed: list[dict], *, now: float | None = None) -> bool:
    """What he said and what was made of it. `replied_at` is set by the first reply; later replies
    (the answer to a follow-up) are appended, bounded; `parsed` accumulates."""
    conn = _ensure()
    if conn is None:
        return False
    ts = lifelog.stamp(now)[0]
    try:
        with conn:
            r = conn.execute("SELECT reply, parsed FROM checkins WHERE local_date = ?", (local_date,)).fetchone()
            if r is None:
                return False
            try:
                so_far = json.loads(r["parsed"] or "[]")
            except Exception:
                so_far = []
            said = lifelog.short(said)
            joined = (r["reply"] + (" | " if r["reply"] and said else "") + said)[:_MAX_REPLY]
            conn.execute("UPDATE checkins SET reply = ?, parsed = ?, "
                         "replied_at = COALESCE(replied_at, ?) WHERE local_date = ?",
                         (joined, json.dumps(so_far + list(parsed)), ts, local_date))
        return True
    except Exception as exc:
        log.warning("storage: could not record check-in reply (%s)", exc)
        return False


def note_followup(local_date: str, question: str, *, now: float | None = None) -> bool:
    """One clarifying question per day. True only for the call that set it."""
    conn = _ensure()
    if conn is None:
        return False
    ts = lifelog.stamp(now)[0]
    try:
        with conn:
            cur = conn.execute("UPDATE checkins SET follow_up = ?, followup_at = ? "
                               "WHERE local_date = ? AND followup_at IS NULL",
                               (lifelog.short(question), ts, local_date))
        return cur.rowcount == 1
    except Exception:
        return False


def close(local_date: str, *, now: float | None = None) -> bool:
    """The day is over. Set once."""
    conn = _ensure()
    if conn is None:
        return False
    ts = lifelog.stamp(now)[0]
    try:
        with conn:
            cur = conn.execute("UPDATE checkins SET closed_at = ? WHERE local_date = ? AND closed_at IS NULL",
                               (ts, local_date))
        return cur.rowcount == 1
    except Exception:
        return False


def waiting(*, since_ts: float = 0.0) -> dict | None:
    """The newest check-in that was sent and is still open (not closed), sent at or after `since_ts`."""
    conn = _ensure()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM checkins WHERE sent_at IS NOT NULL AND closed_at IS NULL "
                         "AND sent_at >= ? ORDER BY sent_at DESC LIMIT 1", (since_ts,)).fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def unclosed(*, before_date: str) -> list[dict]:
    """Check-ins of days before `before_date` that were never closed, oldest first: sent ones, and
    ones claimed but never delivered (both must be finished off so nothing stays open)."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM checkins WHERE closed_at IS NULL AND local_date < ? ORDER BY local_date",
            (before_date,))]
    except Exception:
        return []


def recent(limit: int = 30) -> list[dict]:
    """The latest check-ins, newest first."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        return [_row(r) for r in conn.execute("SELECT * FROM checkins ORDER BY local_date DESC LIMIT ?",
                                              (int(limit),))]
    except Exception:
        return []
