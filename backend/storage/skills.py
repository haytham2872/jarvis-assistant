"""Skills: when each one was opened, and every version it has had.

The skills themselves are Markdown files in jarvis-vault/skills/ (backend/brain/skills.py),
so the owner can read and edit them in Obsidian. What does not belong in the file lives here:

  skill_uses        one row each time a skill is opened -- by JARVIS (`model`), by the owner's
                    /skill command (`command`) or by a scheduled routine (`routine`). This
                    replaces the counters that used to sit in each file's frontmatter, which
                    were rewritten on every turn and, measured 4 October 2026, counted 216
                    "uses" of three skills that had mostly been pasted into unrelated turns.
  skill_revisions   the full text of a skill before each change (save over it, retire,
                    migrate), so any change can be taken back. A skill is a few KB; keeping
                    every version is the point, which is why this table holds whole files
                    rather than the short text other life-log tables keep.

Life-log conventions (lifelog.py): UTC timestamp plus local date on every row, rows are
appended and never edited. Every function swallows its errors and returns a falsey value.
"""
from __future__ import annotations

import logging

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

VIA = ("model", "command", "routine")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_uses (
    use_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    skill       TEXT NOT NULL,
    via         TEXT NOT NULL,
    run_id      TEXT,
    channel     TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL,
    local_date  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_uses_skill ON skill_uses (skill, ts);
CREATE TABLE IF NOT EXISTS skill_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill       TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    ts          REAL NOT NULL,
    local_date  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_revisions_skill ON skill_revisions (skill, ts);
"""

_ready: dict[int, object] = {}


def _conn():
    conn = db.connect()
    if conn is None:
        return None
    if _ready.get(id(conn)) is conn:
        return conn
    try:
        conn.executescript(_SCHEMA)
    except Exception as exc:
        log.warning("skills store: tables unavailable (%s)", exc)
        return None
    _ready[id(conn)] = conn
    return conn


def record_use(skill: str, via: str, *, run_id: str | None = None, channel: str = "") -> bool:
    if via not in VIA:
        raise ValueError(f"via must be one of {VIA}")
    conn = _conn()
    if conn is None:
        return False
    ts, day = lifelog.stamp()
    try:
        with conn:
            conn.execute(
                "INSERT INTO skill_uses (skill, via, run_id, channel, ts, local_date) "
                "VALUES (?, ?, ?, ?, ?, ?)", (skill, via, run_id, channel or "", ts, day))
        return True
    except Exception as exc:
        log.warning("skills store: could not record a use of %s (%s)", skill, exc)
        return False


def use_stats() -> dict[str, dict]:
    """{skill: {"uses": n, "last": ts}} over every recorded use."""
    conn = _conn()
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT skill, COUNT(*), MAX(ts) FROM skill_uses GROUP BY skill").fetchall()
    except Exception as exc:
        log.warning("skills store: could not read uses (%s)", exc)
        return {}
    return {r[0]: {"uses": int(r[1]), "last": float(r[2])} for r in rows}


def uses_between(start_ts: float, end_ts: float) -> dict[str, int]:
    """{skill: openings} in [start_ts, end_ts)."""
    conn = _conn()
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT skill, COUNT(*) FROM skill_uses WHERE ts >= ? AND ts < ? "
                            "GROUP BY skill", (float(start_ts), float(end_ts))).fetchall()
    except Exception as exc:
        log.warning("skills store: could not count uses (%s)", exc)
        return {}
    return {r[0]: int(r[1]) for r in rows}


def revisions_between(start_ts: float, end_ts: float) -> list[dict]:
    """Every revision in [start_ts, end_ts), without the stored text."""
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT revision_id, skill, action, actor, reason, ts FROM skill_revisions "
                            "WHERE ts >= ? AND ts < ? ORDER BY revision_id", (float(start_ts), float(end_ts))).fetchall()
    except Exception as exc:
        log.warning("skills store: could not read revisions (%s)", exc)
        return []
    return [{"revision_id": r[0], "skill": r[1], "action": r[2], "actor": r[3], "reason": r[4],
             "ts": r[5]} for r in rows]


def add_revision(skill: str, action: str, content: str, *, actor: str = "jarvis",
                 reason: str = "") -> int | None:
    """Keep `content` -- the skill as it was before this change. Returns the revision id."""
    conn = _conn()
    if conn is None:
        return None
    ts, day = lifelog.stamp()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO skill_revisions (skill, action, actor, reason, content, ts, local_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (skill, action, actor, lifelog.short(reason), content, ts, day))
        return int(cur.lastrowid)
    except Exception as exc:
        log.warning("skills store: could not keep a revision of %s (%s)", skill, exc)
        return None


def recent_revisions(since_ts: float, limit: int = 50) -> list[dict]:
    """Every skill's revisions since `since_ts`, newest first (for an undo with no name)."""
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT revision_id, skill, action, actor, reason, content, ts FROM skill_revisions "
            "WHERE ts >= ? ORDER BY revision_id DESC LIMIT ?", (float(since_ts), int(limit))).fetchall()
    except Exception as exc:
        log.warning("skills store: could not read recent revisions (%s)", exc)
        return []
    return [{"revision_id": r[0], "skill": r[1], "action": r[2], "actor": r[3], "reason": r[4],
             "content": r[5], "ts": r[6]} for r in rows]


def revisions(skill: str, limit: int = 20) -> list[dict]:
    """Newest first."""
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT revision_id, action, actor, reason, content, ts FROM skill_revisions "
            "WHERE skill = ? ORDER BY revision_id DESC LIMIT ?", (skill, int(limit))).fetchall()
    except Exception as exc:
        log.warning("skills store: could not read revisions of %s (%s)", skill, exc)
        return []
    return [{"revision_id": r[0], "action": r[1], "actor": r[2], "reason": r[3],
             "content": r[4], "ts": r[5]} for r in rows]
