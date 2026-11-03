"""Phone slice 4: every message JARVIS sends the owner on his own, kept for the app's Inbox.

The brief, reminders, scheduled-check results, watch alerts, research findings, goal results,
the evening check-in and the rest all went to Discord only, and nothing kept them: the phone
app could not show them, and "what did you send me this morning?" had no record to read.

`record()` stores a message before it is sent anywhere (brain/notices.py routes it to Discord,
the phone, or both). `notices.discord` says what became of the Discord copy: NULL = not sent
there (his setting), 1 = sent, 0 = the send failed. A failed message retried with the same
words reuses its row, so a check-in retried every minute while Discord is down is one entry,
not sixty.

Also here, because they are the same feature: where these messages go (`route`), and the
phone's push tokens with whether a test notification was confirmed on the phone.

Additive and non-fatal: every function swallows its errors (record returns None).
"""
from __future__ import annotations

import logging
import time

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

MAX_TEXT = 20_000          # a long brief is a few thousand characters; this is a bound, not a cut
ROUTES = ("discord", "both", "phone")
DEFAULT_ROUTE = "discord"  # what happened before the phone app could hold them
_RETRY_WINDOW_S = 24 * 3600
_REWRITTEN_KINDS = ("brief", "review")   # recomposed on each retry: same kind, new words

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    local_date TEXT NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    key        TEXT UNIQUE,
    discord    INTEGER,
    read_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_notices_ts ON notices (ts DESC);
CREATE TABLE IF NOT EXISTS notice_settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_tokens (
    token        TEXT PRIMARY KEY,
    added_at     REAL NOT NULL,
    confirmed_at REAL,
    last_ok      REAL,
    failures     INTEGER NOT NULL DEFAULT 0
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
        log.debug("storage: notices table unavailable (%s)", exc)
        return None


def _row(r) -> dict:
    return {"id": r[0], "ts": r[1], "kind": r[2], "text": r[3],
            "discord": None if r[4] is None else bool(r[4]), "read": r[5] is not None}


_COLS = "id, ts, kind, text, discord, read_at"


def record(kind: str, text: str, key: str | None = None, *, now: float | None = None) -> int | None:
    """Store one message; returns its id, or None when storage is unavailable.

    The same `key` (a goal's keyed delivery) is one row. A message whose Discord send failed
    and is tried again with the same words, or a brief or weekly review recomposed after a
    failed send, reuses the unsent row instead of adding another."""
    conn = _ensure()
    text = (text or "").strip()[:MAX_TEXT]
    if conn is None or not text:
        return None
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            if key:
                found = conn.execute("SELECT id FROM notices WHERE key = ?", (key,)).fetchone()
                if found:
                    return found[0]
            since = ts - _RETRY_WINDOW_S
            found = conn.execute(
                "SELECT id FROM notices WHERE kind = ? AND text = ? AND discord = 0 AND ts >= ? "
                "ORDER BY id DESC LIMIT 1", (kind, text, since)).fetchone()
            if found is None and kind in _REWRITTEN_KINDS:
                found = conn.execute(
                    "SELECT id FROM notices WHERE kind = ? AND discord = 0 AND local_date = ? "
                    "ORDER BY id DESC LIMIT 1", (kind, day)).fetchone()
                if found:
                    conn.execute("UPDATE notices SET text = ?, ts = ? WHERE id = ?",
                                 (text, ts, found[0]))
            if found:
                return found[0]
            cur = conn.execute(
                "INSERT INTO notices (ts, local_date, kind, text, key) VALUES (?, ?, ?, ?, ?)",
                (ts, day, kind, text, key or None))
            return cur.lastrowid
    except Exception as exc:
        log.debug("storage: notice not recorded (%s)", exc)
        return None


def set_discord(notice_id: int | None, sent: bool) -> None:
    """What became of the Discord copy. A success is never turned back into a failure."""
    conn = _ensure()
    if conn is None or notice_id is None:
        return
    try:
        with conn:
            conn.execute("UPDATE notices SET discord = ? WHERE id = ? AND "
                         "(discord IS NULL OR discord = 0)", (1 if sent else 0, notice_id))
    except Exception as exc:
        log.debug("storage: notice delivery not recorded (%s)", exc)


def get(notice_id: int) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        r = conn.execute(f"SELECT {_COLS} FROM notices WHERE id = ?", (int(notice_id),)).fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def listing(before: int | None = None, limit: int = 50) -> list[dict]:
    """Newest first; `before` pages back through older ones."""
    conn = _ensure()
    if conn is None:
        return []
    limit = max(1, min(int(limit), 200))
    try:
        if before:
            rows = conn.execute(f"SELECT {_COLS} FROM notices WHERE id < ? ORDER BY id DESC "
                                "LIMIT ?", (int(before), limit)).fetchall()
        else:
            rows = conn.execute(f"SELECT {_COLS} FROM notices ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [_row(r) for r in rows]
    except Exception as exc:
        log.debug("storage: notices unreadable (%s)", exc)
        return []


def unread_count() -> int:
    conn = _ensure()
    if conn is None:
        return 0
    try:
        return int(conn.execute("SELECT COUNT(*) FROM notices WHERE read_at IS NULL").fetchone()[0])
    except Exception:
        return 0


def mark_read(up_to: int, *, now: float | None = None) -> int:
    """Everything up to and including `up_to` has been seen. Returns how many changed."""
    conn = _ensure()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute("UPDATE notices SET read_at = ? WHERE id <= ? AND read_at IS NULL",
                               (time.time() if now is None else now, int(up_to)))
            return cur.rowcount
    except Exception as exc:
        log.debug("storage: notices not marked read (%s)", exc)
        return 0


# ------------------------------------------------------------------ where they go

def route() -> str:
    conn = _ensure()
    if conn is None:
        return DEFAULT_ROUTE
    try:
        r = conn.execute("SELECT value FROM notice_settings WHERE name = 'route'").fetchone()
        return r[0] if r and r[0] in ROUTES else DEFAULT_ROUTE
    except Exception:
        return DEFAULT_ROUTE


def set_route(value: str) -> bool:
    if value not in ROUTES:
        return False
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute("INSERT INTO notice_settings (name, value) VALUES ('route', ?) "
                         "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (value,))
        return True
    except Exception as exc:
        log.debug("storage: route not saved (%s)", exc)
        return False


# ------------------------------------------------------------------ push tokens

def add_token(token: str, *, now: float | None = None) -> bool:
    conn = _ensure()
    if conn is None or not token:
        return False
    try:
        with conn:
            conn.execute("INSERT INTO push_tokens (token, added_at) VALUES (?, ?) "
                         "ON CONFLICT(token) DO NOTHING", (token, time.time() if now is None else now))
        return True
    except Exception as exc:
        log.debug("storage: push token not saved (%s)", exc)
        return False


def confirm_token(token: str, *, now: float | None = None) -> bool:
    """The phone showed the test notification: pushes really reach it."""
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute("UPDATE push_tokens SET confirmed_at = ?, failures = 0 WHERE token = ?",
                               (time.time() if now is None else now, token))
            return cur.rowcount > 0
    except Exception:
        return False


def tokens(confirmed_only: bool = True) -> list[str]:
    conn = _ensure()
    if conn is None:
        return []
    try:
        sql = "SELECT token FROM push_tokens"
        if confirmed_only:
            sql += " WHERE confirmed_at IS NOT NULL"
        return [r[0] for r in conn.execute(sql + " ORDER BY added_at DESC").fetchall()]
    except Exception:
        return []


def token_state(token: str) -> str:
    """'confirmed', 'waiting' (registered, test not seen yet) or 'unknown'."""
    conn = _ensure()
    if conn is None:
        return "unknown"
    try:
        r = conn.execute("SELECT confirmed_at FROM push_tokens WHERE token = ?", (token,)).fetchone()
    except Exception:
        return "unknown"
    if r is None:
        return "unknown"
    return "confirmed" if r[0] else "waiting"


def push_result(token: str, ok: bool, *, gone: bool = False, now: float | None = None) -> None:
    """After a push: a token Expo says is no longer registered is removed."""
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn:
            if gone:
                conn.execute("DELETE FROM push_tokens WHERE token = ?", (token,))
            elif ok:
                conn.execute("UPDATE push_tokens SET last_ok = ?, failures = 0 WHERE token = ?",
                             (time.time() if now is None else now, token))
            else:
                conn.execute("UPDATE push_tokens SET failures = failures + 1 WHERE token = ?",
                             (token,))
    except Exception as exc:
        log.debug("storage: push result not recorded (%s)", exc)
