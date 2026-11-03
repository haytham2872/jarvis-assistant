"""Standing watches: conditions JARVIS checks on a cadence and reports when met.

Storage only -- the evaluation loop lives in `backend.brain.watches`. A watch is a
standing definition, not a one-shot job, so it gets its own small table rather than a
row per evaluation in `derivation_jobs`.

The alert is a small state machine on the row, ordered so a crash can never produce a
second DM for the same firing:

    none -> sending   (persisted BEFORE the DM goes out)
    sending -> sent   (the DM was confirmed)
    sending -> parked (the DM failed: mention it in the next conversation turn)
    parked -> none    (the mention was delivered)

A row left in `sending` by a dead process is uncertain -- Discord may have accepted the
message -- so startup reconciliation turns it into a parked mention flagged as possibly
already sent, never a resend. Same rule as `storage/delivery.py`.

Additive and non-fatal: every function swallows its errors and returns a falsey value.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
import time
import uuid

from backend.storage import db

log = logging.getLogger(__name__)

ACTIVE = "active"
FIRED = "fired"
CANCELLED = "cancelled"
EXPIRED = "expired"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    watch_id        TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,
    condition       TEXT NOT NULL,
    method          TEXT NOT NULL,
    query           TEXT NOT NULL,
    every_s         REAL NOT NULL,
    repeat          INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    next_check_at   REAL NOT NULL,
    last_checked_at REAL,
    evidence_hash   TEXT,
    checks_day      TEXT NOT NULL DEFAULT '',
    judged_today    INTEGER NOT NULL DEFAULT 0,
    fire_count      INTEGER NOT NULL DEFAULT 0,
    last_verdict    TEXT NOT NULL DEFAULT '',
    alert_state     TEXT NOT NULL DEFAULT 'none',
    alert_text      TEXT NOT NULL DEFAULT '',
    origin_text     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_watches_due ON watches (state, next_check_at);
"""

_FIELDS = ("watch_id", "subject", "condition", "method", "query", "every_s", "repeat",
           "state", "created_at", "expires_at", "next_check_at", "last_checked_at",
           "evidence_hash", "checks_day", "judged_today", "fire_count", "last_verdict",
           "alert_state", "alert_text", "origin_text")


def _conn():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("watches: table unavailable (%s)", exc)
        return None


def add(*, subject: str, condition: str, method: str, query: str, every_s: float,
        expires_at: float, repeat: bool = False, origin_text: str = "",
        now: float | None = None, connection=None) -> str | None:
    conn = connection if connection is not None else _conn()
    if conn is None:
        return None
    now = time.time() if now is None else now
    watch_id = uuid.uuid4().hex[:8]
    try:
        with nullcontext(conn) if connection is not None else conn:
            conn.execute(
                "INSERT INTO watches (watch_id, subject, condition, method, query, every_s,"
                " repeat, state, created_at, expires_at, next_check_at, origin_text)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (watch_id, subject, condition, method, query, every_s, int(repeat),
                 ACTIVE, now, expires_at, now, origin_text[:500]),
            )
        return watch_id
    except Exception as exc:
        log.warning("watches: add failed (%s)", exc)
        return None


def get(watch_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def list_active() -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM watches WHERE state=? ORDER BY created_at", (ACTIVE,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def fired_since(since: float, *, strict: bool = False) -> list[dict]:
    """Fire-once watches that fired at or after `since`, newest first. A repeating watch's
    `last_checked_at` moves on with every check, so it cannot say when it fired: only a watch
    that finished (state `fired`) is listed. Read-only, for /activity."""
    conn = _conn()
    if conn is None:
        if strict:
            log.warning("watches: fired watches unavailable")
            raise RuntimeError("Watch storage unavailable")
        return []
    try:
        rows = conn.execute("SELECT * FROM watches WHERE state=? AND last_checked_at>=?"
                            " ORDER BY last_checked_at DESC", (FIRED, float(since))).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        if strict:
            log.warning("watches: fired watches could not be read (%s)", exc)
            raise
        return []


def due(now: float, limit: int) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM watches WHERE state=? AND next_check_at<=? AND alert_state!='sending' AND method!='portfolio_close'"
            " ORDER BY next_check_at LIMIT ?", (ACTIVE, now, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def update(watch_id: str, **fields) -> bool:
    unknown = set(fields) - set(_FIELDS)
    if unknown or not fields:
        raise ValueError(f"unknown watch fields: {sorted(unknown)}")
    conn = _conn()
    if conn is None:
        return False
    cols = ", ".join(f"{k}=?" for k in fields)
    try:
        with conn:
            cur = conn.execute(f"UPDATE watches SET {cols} WHERE watch_id=?",
                               (*fields.values(), watch_id))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("watches: update failed for %s (%s)", watch_id, exc)
        return False


def begin_alert(watch_id: str, text: str, *, next_state: str, next_check_at: float,
                verdict: str, connection=None) -> bool:
    """Record the firing BEFORE the DM, atomically: only one caller can win it."""
    conn = connection if connection is not None else _conn()
    if conn is None:
        return False
    try:
        with nullcontext(conn) if connection is not None else conn:
            cur = conn.execute(
                "UPDATE watches SET alert_state='sending', alert_text=?, state=?,"
                " next_check_at=?, last_verdict=?, fire_count=fire_count+1"
                " WHERE watch_id=? AND state=? AND alert_state!='sending'",
                (text, next_state, next_check_at, verdict[:500], watch_id, ACTIVE),
            )
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("watches: could not record firing for %s (%s)", watch_id, exc)
        return False


def finish_alert(watch_id: str, delivered: bool) -> None:
    update(watch_id, alert_state="sent" if delivered else "parked")


def pop_parked() -> list[dict]:
    """Alerts that could not be DMed, cleared as they are handed out (exactly once)."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn:
            rows = conn.execute(
                "SELECT watch_id, alert_text, alert_state FROM watches"
                " WHERE alert_state IN ('parked', 'parked_uncertain')").fetchall()
            conn.execute(
                "UPDATE watches SET alert_state='none'"
                " WHERE alert_state IN ('parked', 'parked_uncertain')")
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("watches: pop_parked failed (%s)", exc)
        return []


def reconcile_inflight() -> int:
    """A firing left in `sending` by a dead process may already have reached Discord."""
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn:
            # G2: a goal-owned watch never sends a DM (its fire is the row itself, read by the
            # goal runner), so a crash mid-fire left nothing to report to the owner.
            try:
                conn.execute(
                    "UPDATE watches SET alert_state='sent' WHERE alert_state='sending' AND"
                    " watch_id IN (SELECT target_id FROM goal_bindings WHERE kind='watch')")
            except Exception:
                pass                                    # no goal tables: nothing goal-owned
            cur = conn.execute(
                "UPDATE watches SET alert_state='parked_uncertain' WHERE alert_state='sending'")
        return cur.rowcount
    except Exception as exc:
        log.warning("watches: startup reconciliation failed (%s)", exc)
        return 0


def expire(now: float) -> int:
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE watches SET state=? WHERE state=? AND expires_at<=?",
                (EXPIRED, ACTIVE, now))
        return cur.rowcount
    except Exception:
        return 0
