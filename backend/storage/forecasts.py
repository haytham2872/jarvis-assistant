"""Checkable claims JARVIS makes about the future, and how they turned out.

Storage only -- recording, scoring and resolution live in `backend.brain.forecasts`.
Rows are never deleted: a withdrawn or unresolvable claim stays, with its state, so the
record cannot be quietly improved by forgetting the misses.

`raw_p` is JARVIS's gut probability as he first gave it; `stated_p` is what he was told
to state after his own record in that area was taken into account. Both are scored, so
the record shows whether the self-correction actually helps.

Notices (a resolution to tell the owner about) use the same crash-safe state machine as
watches: recorded before the DM, `sending` left by a dead process becomes an uncertain
next-turn mention rather than a second DM.

Additive and non-fatal: every function swallows its errors and returns a falsey value.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
import time

from backend.storage import db

log = logging.getLogger(__name__)

OPEN = "open"
RESOLVED = "resolved"
UNRESOLVABLE = "unresolvable"
WITHDRAWN = "withdrawn"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    claim            TEXT NOT NULL,
    criteria         TEXT NOT NULL,
    domain           TEXT NOT NULL,
    raw_p            REAL NOT NULL,
    stated_p         REAL NOT NULL,
    method           TEXT NOT NULL,
    check_query      TEXT NOT NULL DEFAULT '',
    resolve_by       REAL NOT NULL,
    created_at       REAL NOT NULL,
    source_quote     TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL,
    outcome          INTEGER,
    resolved_at      REAL,
    resolver         TEXT NOT NULL DEFAULT '',
    resolution_quote TEXT NOT NULL DEFAULT '',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_check_at    REAL NOT NULL,
    notice_state     TEXT NOT NULL DEFAULT 'none',
    notice_text      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_forecasts_due ON forecasts (state, next_check_at);
CREATE INDEX IF NOT EXISTS idx_forecasts_domain ON forecasts (domain, state);
"""

_FIELDS = ("claim", "criteria", "domain", "raw_p", "stated_p", "method", "check_query",
           "resolve_by", "created_at", "source_quote", "state", "outcome", "resolved_at",
           "resolver", "resolution_quote", "attempts", "next_check_at", "notice_state",
           "notice_text")


def _conn():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("forecasts: table unavailable (%s)", exc)
        return None


def add(*, claim: str, criteria: str, domain: str, raw_p: float, stated_p: float,
        method: str, check_query: str, resolve_by: float, source_quote: str = "",
        now: float | None = None, connection=None) -> int | None:
    conn = connection if connection is not None else _conn()
    if conn is None:
        return None
    now = time.time() if now is None else now
    try:
        with nullcontext(conn) if connection is not None else conn:
            cur = conn.execute(
                "INSERT INTO forecasts (claim, criteria, domain, raw_p, stated_p, method,"
                " check_query, resolve_by, created_at, source_quote, state, next_check_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim, criteria, domain, raw_p, stated_p, method, check_query, resolve_by,
                 now, source_quote[:500], OPEN, resolve_by))
        return cur.lastrowid
    except Exception as exc:
        log.warning("forecasts: add failed (%s)", exc)
        return None


def get(forecast_id: int) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM forecasts WHERE forecast_id=?",
                           (forecast_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def query(state: str | None = None, domain: str | None = None, *, strict: bool = False) -> list[dict]:
    """Existing callers keep the falsey fallback; views can distinguish unavailable from empty."""
    conn = _conn()
    if conn is None:
        log.warning("forecasts: query unavailable (no storage connection)")
        if strict:
            raise RuntimeError("Forecast storage is unavailable")
        return []
    sql, args = "SELECT * FROM forecasts WHERE 1=1", []
    if state:
        sql += " AND state=?"
        args.append(state)
    if domain:
        sql += " AND domain=?"
        args.append(domain)
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY forecast_id", args).fetchall()]
    except Exception as exc:
        log.warning("forecasts: query failed (%s)", exc)
        if strict:
            raise RuntimeError("Forecast storage could not be read") from exc
        return []


def due(now: float, limit: int) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM forecasts WHERE state=? AND next_check_at<=?"
            " ORDER BY next_check_at LIMIT ?", (OPEN, now, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def update(forecast_id: int, **fields) -> bool:
    unknown = set(fields) - set(_FIELDS)
    if unknown or not fields:
        raise ValueError(f"unknown forecast fields: {sorted(unknown)}")
    conn = _conn()
    if conn is None:
        return False
    cols = ", ".join(f"{k}=?" for k in fields)
    try:
        with conn:
            cur = conn.execute(f"UPDATE forecasts SET {cols} WHERE forecast_id=?",
                               (*fields.values(), forecast_id))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("forecasts: update failed for %s (%s)", forecast_id, exc)
        return False


def resolve(forecast_id: int, *, outcome: int, resolver: str, quote: str, notice: str,
            now: float | None = None) -> bool:
    """Record the verdict and queue its notice in one step. Only an open (or
    automatically unresolvable) forecast can be resolved, so two resolvers racing cannot
    both score it, and a resolved one is never re-scored."""
    conn = _conn()
    if conn is None:
        return False
    now = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "UPDATE forecasts SET state=?, outcome=?, resolved_at=?, resolver=?,"
                " resolution_quote=?, notice_state=?, notice_text=?"
                " WHERE forecast_id=? AND state IN (?, ?)",
                (RESOLVED, int(bool(outcome)), now, resolver, quote[:500],
                 "sending" if notice else "none", notice, forecast_id, OPEN, UNRESOLVABLE))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("forecasts: resolve failed for %s (%s)", forecast_id, exc)
        return False


def finish_notice(forecast_id: int, delivered: bool) -> None:
    update(forecast_id, notice_state="sent" if delivered else "parked")


def pop_parked() -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn:
            rows = conn.execute(
                "SELECT forecast_id, notice_text, notice_state FROM forecasts"
                " WHERE notice_state IN ('parked', 'parked_uncertain')").fetchall()
            conn.execute(
                "UPDATE forecasts SET notice_state='none'"
                " WHERE notice_state IN ('parked', 'parked_uncertain')")
        return [dict(r) for r in rows]
    except Exception:
        return []


def reconcile_inflight() -> int:
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE forecasts SET notice_state='parked_uncertain' WHERE notice_state='sending'")
        return cur.rowcount
    except Exception:
        return 0
