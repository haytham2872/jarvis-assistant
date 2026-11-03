"""Price and FX history kept for drawing charts on the phone. Never read for valuation: the
ledger's own tables (storage/portfolio.py) hold every figure. A series is replaced whole when it
is fetched again, so a later split, which changes every adjusted close, can never leave old and
new values mixed."""
from __future__ import annotations

import json
import time

from backend.storage import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS display_series (
 series TEXT NOT NULL, day TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(series, day)
);
CREATE TABLE IF NOT EXISTS display_fetches (
 series TEXT PRIMARY KEY, fetched_at REAL NOT NULL, extra TEXT NOT NULL DEFAULT '{}'
);
"""


def _conn():
    conn = db.connect()
    if conn is None:
        return None
    conn.executescript(_SCHEMA)
    return conn


def save(series: str, rows: list[tuple[str, str]], extra: dict | None = None, *, now: float | None = None) -> bool:
    """Replace one series: (day, value) rows plus what came with them (splits)."""
    conn = _conn()
    if conn is None:
        return False
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM display_series WHERE series=?", (series,))
        conn.executemany("INSERT INTO display_series(series, day, value) VALUES (?,?,?)",
                         [(series, d, v) for d, v in rows])
        conn.execute("INSERT INTO display_fetches(series, fetched_at, extra) VALUES (?,?,?) "
                     "ON CONFLICT(series) DO UPDATE SET fetched_at=excluded.fetched_at, extra=excluded.extra",
                     (series, now if now is not None else time.time(), json.dumps(extra or {})))
    return True


def load(series: str) -> tuple[list[tuple[str, str]], float | None, dict]:
    """(rows by day, when fetched, extra); empty and None when never fetched."""
    conn = _conn()
    if conn is None:
        return [], None, {}
    meta = conn.execute("SELECT fetched_at, extra FROM display_fetches WHERE series=?", (series,)).fetchone()
    if meta is None:
        return [], None, {}
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT day, value FROM display_series WHERE series=? ORDER BY day", (series,))]
    return rows, meta[0], json.loads(meta[1] or "{}")
