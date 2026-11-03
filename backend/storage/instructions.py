"""Standing instructions from the owner: corrections and preferences that must stick.

Kept apart from mem0 facts on purpose. A fact is one of ~1,100 retrieved only when a
later turn happens to be semantically close; an instruction applies to every turn in
its scope, so it is read directly and injected whole.

Rules are never deleted. Retiring or superseding one keeps the row, with the rule that
replaced it, so "why did you stop doing X" always has an answer.

Additive and non-fatal: every function swallows its errors and returns a falsey value.
"""
from __future__ import annotations

import logging
import re
import time

from backend.storage import db

log = logging.getLogger(__name__)

ACTIVE = "active"
RETIRED = "retired"
SUPERSEDED = "superseded"
SCOPES = ("always", "discord", "desk")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS standing_instructions (
    rule_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule            TEXT NOT NULL,
    scope           TEXT NOT NULL,
    state           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    reaffirmed_at   REAL NOT NULL,
    times_restated  INTEGER NOT NULL DEFAULT 0,
    source_quote    TEXT NOT NULL DEFAULT '',
    superseded_by   INTEGER,
    ended_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_instructions_state ON standing_instructions (state, scope);
"""


def _conn():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.warning("instructions: table unavailable (%s)", exc)
        return None


_CONTRACTIONS = re.compile(r"'(re|s|ve|ll|d|m|t)\b")
_STOPWORDS = frozenset(
    "the and for you your are was were with that this what about from into when then than "
    "have has had not don doesn didn isn aren can could would should will just very also "
    "any all each every his him her its our out off too".split())


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", _CONTRACTIONS.sub("", text.lower()))
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Content-word Jaccard. Only a fallback for noticing a literal restatement -- the
    model declares a paraphrased one itself (`restates=`). Never merges rules that merely
    share a topic: "keep replies short" and "keep replies in English" stay two rules."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def active(scope_filter: tuple[str, ...] | None = None) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM standing_instructions WHERE state=? ORDER BY rule_id",
            (ACTIVE,)).fetchall()
        out = [dict(r) for r in rows]
        if scope_filter is not None:
            out = [r for r in out if r["scope"] in scope_filter]
        return out
    except Exception:
        return []


def get(rule_id: int) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM standing_instructions WHERE rule_id=?",
                           (rule_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def add(rule: str, scope: str, source_quote: str = "", now: float | None = None) -> int | None:
    conn = _conn()
    if conn is None:
        return None
    now = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO standing_instructions (rule, scope, state, created_at,"
                " reaffirmed_at, source_quote) VALUES (?,?,?,?,?,?)",
                (rule, scope, ACTIVE, now, now, source_quote[:500]))
        return cur.lastrowid
    except Exception as exc:
        log.warning("instructions: add failed (%s)", exc)
        return None


def restate(rule_id: int, rule: str, source_quote: str = "", now: float | None = None) -> bool:
    """He said it again: the rule was not followed. Count it, keep the newest wording."""
    conn = _conn()
    if conn is None:
        return False
    now = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "UPDATE standing_instructions SET rule=?, reaffirmed_at=?,"
                " times_restated=times_restated+1,"
                " source_quote=CASE WHEN ?='' THEN source_quote ELSE ? END"
                " WHERE rule_id=? AND state=?",
                (rule, now, source_quote[:500], source_quote[:500], rule_id, ACTIVE))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("instructions: restate failed (%s)", exc)
        return False


def end(rule_id: int, state: str, superseded_by: int | None = None,
        now: float | None = None) -> bool:
    conn = _conn()
    if conn is None:
        return False
    now = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "UPDATE standing_instructions SET state=?, superseded_by=?, ended_at=?"
                " WHERE rule_id=? AND state=?",
                (state, superseded_by, now, rule_id, ACTIVE))
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("instructions: end failed (%s)", exc)
        return False
