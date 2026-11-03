"""Person-profile bookkeeping: who the owner said no to, and which questions were suggested.

The profiles themselves are vault Markdown (backend/brain/people.py). This keeps the two
small operational facts that must survive restarts but are not part of any profile:

* **Declines.** When the owner says he does not want a profile for someone, JARVIS never
  offers again. Keyed on the normalised name; creating the profile anyway clears it.
* **Suggested questions.** Which gap question was put in front of the model for which
  person, and when, so the same question is not suggested every time the person comes
  up. A suggestion is not proof it was asked -- the model may judge the moment wrong --
  so this only spaces suggestions out; it never marks a section as covered.

Additive and non-fatal: every function swallows its errors and returns a falsey value.
"""
from __future__ import annotations

import logging
import re
import time

from backend.storage import db

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_declines (
    name_key     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    declined_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_suggestions (
    person_key    TEXT NOT NULL,
    section       TEXT NOT NULL,
    suggested_at  REAL NOT NULL,
    PRIMARY KEY (person_key, section)
);
"""


def _conn():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("people store: tables unavailable (%s)", exc)
        return None


def key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def decline(name: str, now: float | None = None) -> bool:
    conn = _conn()
    if conn is None or not key(name):
        return False
    try:
        conn.execute("INSERT OR REPLACE INTO profile_declines VALUES (?, ?, ?)",
                     (key(name), " ".join(name.split()), now or time.time()))
        conn.commit()
        return True
    except Exception as exc:
        log.warning("people store: decline failed (%s)", exc)
        return False


def clear_decline(name: str) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        conn.execute("DELETE FROM profile_declines WHERE name_key = ?", (key(name),))
        conn.commit()
    except Exception as exc:
        log.warning("people store: clear_decline failed (%s)", exc)


def declined() -> list[str]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT name FROM profile_declines ORDER BY declined_at").fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def record_suggestion(person: str, section: str, now: float | None = None) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        conn.execute("INSERT OR REPLACE INTO profile_suggestions VALUES (?, ?, ?)",
                     (key(person), section, now or time.time()))
        conn.commit()
    except Exception as exc:
        log.warning("people store: record_suggestion failed (%s)", exc)


def suggestions(person: str) -> dict[str, float]:
    """{section: last suggested at} for one person."""
    conn = _conn()
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT section, suggested_at FROM profile_suggestions "
                            "WHERE person_key = ?", (key(person),)).fetchall()
        return {r[0]: float(r[1]) for r in rows}
    except Exception:
        return {}
