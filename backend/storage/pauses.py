"""P2: pauses -- the record behind "/pause".

A pause is the owner saying: stop doing things on your own for a while. One row per pause
(`pauses`), and one row per thing that could not happen because of it (`pause_skips`, append
only): a scheduled check whose moment passed, a job that was stopped. The skips are what the
message on `/resume` lists, and they are the raw material for later questions like "how often
did I pause, and what did it cost".

Life-log conventions (`lifelog.py`): a UTC timestamp and a local date on every row, short text,
nothing deleted, and the pause row moves forward once (`ended_at`, `ended_how`) and is never
rewritten. There is at most one active pause: starting another ends the first as `replaced`.

`active()` treats a pause whose `until_at` has passed as over even before anything has marked it
ended, so the gate opens on time whether or not the expiry loop has run yet; `expire_due()` is
what marks it (and lets the loop announce it).
"""
from __future__ import annotations

import logging
import time

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

NUDGE_EVERY_S = 24 * 3600.0     # a pause nobody ends is mentioned once a day

WORK = "work"          # everything JARVIS does on his own; reminders and the brief still arrive
ALL = "all"            # ... and reminders and the morning brief too

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pauses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    local_date TEXT NOT NULL,
    scope      TEXT NOT NULL,
    until_at   REAL,
    reason     TEXT NOT NULL DEFAULT '',
    ended_at   REAL,
    ended_how  TEXT,
    nudged_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_pauses_open ON pauses (ended_at, started_at);

CREATE TABLE IF NOT EXISTS pause_skips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pause_id   INTEGER NOT NULL,
    at         REAL NOT NULL,
    local_date TEXT NOT NULL,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pause_skips_pause ON pause_skips (pause_id, at);
"""


def _ensure():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: pauses tables unavailable (%s)", exc)
        return None


def start(scope: str, until_at: float | None = None, reason: str = "",
          *, now: float | None = None) -> int | None:
    """Begin a pause, ending any active one as `replaced`. Returns its id, or None when it
    could not be recorded (in which case nothing is paused, and the caller must say so)."""
    if scope not in (WORK, ALL):
        raise ValueError(f"unknown pause scope: {scope}")
    conn = _ensure()
    if conn is None:
        return None
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            # A pause still in force is replaced. One whose time had already run out has
            # simply ended (it has only not been swept yet): "replaced" would hide that.
            conn.execute("UPDATE pauses SET ended_at = ?, ended_how = 'replaced' WHERE "
                         "ended_at IS NULL AND (until_at IS NULL OR until_at > ?)", (ts, ts))
            conn.execute("UPDATE pauses SET ended_at = until_at, ended_how = 'expired' WHERE "
                         "ended_at IS NULL AND until_at IS NOT NULL AND until_at <= ?", (ts,))
            cur = conn.execute(
                "INSERT INTO pauses (started_at, local_date, scope, until_at, reason) "
                "VALUES (?, ?, ?, ?, ?)", (ts, day, scope, until_at, lifelog.short(reason)))
        return int(cur.lastrowid)
    except Exception as exc:
        log.warning("storage: could not record pause (%s)", exc)
        return None


def active(*, now: float | None = None) -> dict | None:
    """The pause in force right now, or None. One that has run out counts as over."""
    conn = _ensure()
    if conn is None:
        return None
    ts = time.time() if now is None else now
    try:
        r = conn.execute("SELECT * FROM pauses WHERE ended_at IS NULL AND "
                         "(until_at IS NULL OR until_at > ?) ORDER BY id DESC LIMIT 1",
                         (ts,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def end(how: str, *, now: float | None = None) -> dict | None:
    """End the pause in force (`resume`). Returns its row, or None when none was active."""
    row = active(now=now)
    if row is None:
        return None
    conn = _ensure()
    ts = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute("UPDATE pauses SET ended_at = ?, ended_how = ? "
                               "WHERE id = ? AND ended_at IS NULL", (ts, how, row["id"]))
        return get(row["id"]) if cur.rowcount == 1 else None
    except Exception:
        return None


def expire_due(*, now: float | None = None) -> list[dict]:
    """Mark pauses whose time ran out as ended (`expired`). Returns the rows ended by THIS
    call, so the loop that calls it announces each exactly once."""
    conn = _ensure()
    if conn is None:
        return []
    ts = time.time() if now is None else now
    ended = []
    try:
        due = conn.execute("SELECT id FROM pauses WHERE ended_at IS NULL AND until_at IS NOT NULL "
                           "AND until_at <= ?", (ts,)).fetchall()
        for r in due:
            with conn:
                cur = conn.execute("UPDATE pauses SET ended_at = ?, ended_how = 'expired' "
                                   "WHERE id = ? AND ended_at IS NULL", (ts, r["id"]))
            if cur.rowcount == 1:
                ended.append(get(r["id"]))
    except Exception as exc:
        log.debug("storage: pause expiry failed (%s)", exc)
    return [e for e in ended if e]


def get(pause_id: int) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM pauses WHERE id = ?", (int(pause_id),)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def nudge_due(*, now: float | None = None) -> dict | None:
    """The pause in force, when a whole day has passed since the owner last heard about it
    (its start, or the last nudge). A pause with an end time far off is mentioned too: a
    forgotten one silently skips every scheduled check, and skips are listed only on /resume."""
    row = active(now=now)
    if row is None:
        return None
    ts = time.time() if now is None else now
    return row if ts - (row.get("nudged_at") or row["started_at"]) >= NUDGE_EVERY_S else None


def mark_nudged(pause_id: int, *, now: float | None = None) -> None:
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("UPDATE pauses SET nudged_at = ? WHERE id = ?",
                         (time.time() if now is None else now, int(pause_id)))
    except Exception as exc:
        log.debug("storage: pause nudge not recorded (%s)", exc)


def chain(pause_id: int) -> list[dict]:
    """The pause and every pause it replaced, oldest first.

    `/pause` and then `/pause all` is one stretch of the owner having switched me off, so what
    the first one skipped belongs in the message on `/resume`, and the time counts from the
    first. Any other ending (resumed, expired) closes the chain.
    """
    conn = _ensure()
    if conn is None:
        return []
    try:
        row = conn.execute("SELECT * FROM pauses WHERE id = ?", (int(pause_id),)).fetchone()
        if row is None:
            return []
        rows = [dict(row)]
        while True:
            prev = conn.execute("SELECT * FROM pauses WHERE id < ? ORDER BY id DESC LIMIT 1",
                                (rows[0]["id"],)).fetchone()
            if prev is None or prev["ended_how"] != "replaced":
                return rows
            rows.insert(0, dict(prev))
    except Exception:
        return []


def chain_skips(pause_id: int) -> list[dict]:
    """Everything skipped in the pause's whole stretch, in the order it happened."""
    ids = [r["id"] for r in chain(pause_id)]
    conn = _ensure()
    if conn is None or not ids:
        return []
    try:
        rows = conn.execute("SELECT * FROM pause_skips WHERE pause_id IN (%s) ORDER BY at, id"
                            % ",".join("?" for _ in ids), ids).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def add_skip(pause_id: int, kind: str, label: str, *, now: float | None = None) -> None:
    """Record one thing that did not happen because of the pause. Never raises."""
    conn = _ensure()
    if conn is None or not pause_id:
        return
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            conn.execute("INSERT INTO pause_skips (pause_id, at, local_date, kind, label) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (int(pause_id), ts, day, kind, lifelog.short(label, 200)))
    except Exception as exc:
        log.debug("storage: pause skip not recorded (%s)", exc)


def skips(pause_id: int) -> list[dict]:
    conn = _ensure()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM pause_skips WHERE pause_id = ? ORDER BY at, id",
                            (int(pause_id),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def recent(limit: int = 20) -> list[dict]:
    conn = _ensure()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM pauses ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
