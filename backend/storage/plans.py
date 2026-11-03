"""P3: the week plan and everything that happens to it -- the record behind `week_plan`.

Three tables, owned here (life-log conventions, `lifelog.py`):

  plans         one row per plan VERSION of a week: a draft, the applied plan, each change that
                followed. A version's blocks never change after it is written; the row itself only
                moves forward (draft -> applied -> superseded, or draft -> discarded), each
                timestamp set once.
  plan_blocks   one row per block in a version -- a full copy per version, so "what did the plan
                say on Tuesday" is one query and undo has something exact to go back to. `uid` is
                the same across versions for the same block; `start_at`/`end_at` are what the
                version PLANNED and are never rewritten. What the calendar shows, when the owner has
                moved or deleted a block himself, is `live_start`/`live_end` and the status.
  block_events  APPEND-ONLY history of what happened to a block (proposed, created, moved,
                removed, seen_moved, seen_deleted, failed, ...). `plan_blocks.status` is a
                convenience that can always be rebuilt from these. Nothing in the code base may
                UPDATE or DELETE this table (a structural test walks the source).

So planned-against-actual is a query, not a reconstruction: planned is the immutable row, actual
is the event trail. Every row carries a UTC timestamp and a local date.

Nothing here talks to the calendar or decides anything; `brain/weekplan.py` does that and calls
this to remember it. Every function returns None / [] / False instead of raising when storage is
unavailable, like the other stores, so a broken database costs the record and never the turn.
"""
from __future__ import annotations
from backend import owner as _owner

import json
import logging
import time
import uuid
from typing import Iterable

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

# plan states
DRAFT = "draft"
APPLIED = "applied"
SUPERSEDED = "superseded"
DISCARDED = "discarded"

# block statuses (a convenience: rebuildable from block_events)
PLANNED = "planned"          # in a plan, not in the calendar (yet)
SCHEDULED = "scheduled"      # its calendar event exists
FAILED = "failed"            # writing it to the calendar failed
MOVED = "moved"              # The owner moved its event himself; live_start/live_end say where
DELETED = "deleted"          # The owner deleted its event himself

# block_events.event
PROPOSED = "proposed"
CREATED = "created"
EVENT_MOVED = "moved"        # JARVIS moved a block (a change he asked for, or an undo)
REMOVED = "removed"
RESTORED = "restored"
WRITE_FAILED = "failed"
SEEN_MOVED = "seen_moved"
SEEN_DELETED = "seen_deleted"
RESHUFFLE = "reshuffle"      # P4: a new slot was proposed for the block; the reason is what prompted it

# P4: what became of a block, as the owner told it at the evening check-in. Stored as block_events
# named "outcome_<word>" so they can never be confused with the calendar events above ("moved" is
# JARVIS moving a block in the calendar; the outcome "moved" is the owner doing it at another time).
OUTCOMES = ("done", "partly", "skipped", "moved", "unknown")
_OUTCOME_PREFIX = "outcome_"

_SCHEMA = _owner.say("""
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    state         TEXT NOT NULL,
    rules         TEXT NOT NULL,
    created_at    REAL NOT NULL,
    local_date    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    parent_id     INTEGER,
    applied_at    REAL,
    superseded_at REAL,
    undone_by     INTEGER,
    UNIQUE (week_start, version)
);
CREATE INDEX IF NOT EXISTS idx_plans_week ON plans (week_start, version);

CREATE TABLE IF NOT EXISTS plan_blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id    INTEGER NOT NULL,
    uid        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    start_at   REAL NOT NULL,
    end_at     REAL NOT NULL,
    local_date TEXT NOT NULL,
    event_id   TEXT,
    status     TEXT NOT NULL DEFAULT 'planned',
    live_start REAL,
    live_end   REAL
);
CREATE INDEX IF NOT EXISTS idx_plan_blocks_plan ON plan_blocks (plan_id, start_at);
CREATE INDEX IF NOT EXISTS idx_plan_blocks_uid ON plan_blocks (uid);

CREATE TABLE IF NOT EXISTS block_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    block_uid  TEXT NOT NULL,
    plan_id    INTEGER NOT NULL,
    at         REAL NOT NULL,
    local_date TEXT NOT NULL,
    event      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    old_start  REAL,
    old_end    REAL,
    new_start  REAL,
    new_end    REAL,
    reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_block_events_uid ON block_events (block_uid, at);
CREATE INDEX IF NOT EXISTS idx_block_events_day ON block_events (local_date, event);

CREATE TABLE IF NOT EXISTS plan_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    local_date TEXT NOT NULL
);

-- The two ready-made views for the questions that will be asked first (roadmap section 6).
-- v_block_outcomes: one row per block that was ever applied -- what was planned (from the last
-- version that held it) against what happened (the latest outcome event). `outcome` is NULL when
-- nobody was ever asked; 'unknown' when «he» was asked and it stayed unclear.
CREATE VIEW IF NOT EXISTS v_block_outcomes AS
SELECT b.uid AS uid,
       p.week_start AS week_start,
       b.kind AS kind,
       b.local_date AS planned_date,
       b.start_at AS planned_start,
       b.end_at AS planned_end,
       b.status AS calendar_status,
       (SELECT substr(e.event, 9) FROM block_events e
         WHERE e.block_uid = b.uid AND substr(e.event, 1, 8) = 'outcome_'
         ORDER BY e.at DESC, e.id DESC LIMIT 1) AS outcome,
       (SELECT e.reason FROM block_events e
         WHERE e.block_uid = b.uid AND substr(e.event, 1, 8) = 'outcome_'
         ORDER BY e.at DESC, e.id DESC LIMIT 1) AS reason,
       (SELECT COUNT(*) FROM block_events e
         WHERE e.block_uid = b.uid AND e.event IN ('moved', 'seen_moved')) AS times_moved,
       COALESCE((SELECT e.event FROM block_events e
         WHERE e.block_uid = b.uid AND e.event IN ('created', 'restored', 'removed')
         ORDER BY e.at DESC, e.id DESC LIMIT 1) = 'removed', 0) AS removed
FROM plan_blocks b JOIN plans p ON p.id = b.plan_id
WHERE b.id = (SELECT MAX(b2.id) FROM plan_blocks b2 JOIN plans p2 ON p2.id = b2.plan_id
               WHERE b2.uid = b.uid AND p2.state IN ('applied', 'superseded'));

-- v_adherence: per week and kind, how many blocks there were and how each ended. Blocks taken out
-- of the plan are not counted (cancelled is not skipped).
CREATE VIEW IF NOT EXISTS v_adherence AS
SELECT week_start, kind,
       COUNT(*) AS planned,
       SUM(CASE WHEN outcome = 'done' THEN 1 ELSE 0 END) AS done,
       SUM(CASE WHEN outcome = 'partly' THEN 1 ELSE 0 END) AS partly,
       SUM(CASE WHEN outcome = 'skipped' THEN 1 ELSE 0 END) AS skipped,
       SUM(CASE WHEN outcome = 'moved' THEN 1 ELSE 0 END) AS moved,
       SUM(CASE WHEN outcome = 'unknown' THEN 1 ELSE 0 END) AS unknown,
       SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS unreported
FROM v_block_outcomes WHERE removed = 0 GROUP BY week_start, kind;
""")


def _ensure():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: plan tables unavailable (%s)", exc)
        return None


def new_uid() -> str:
    """A block's identity, the same in every version that contains it."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def create_version(week_start: str, rules: dict, blocks: Iterable[dict], *, created_by: str,
                   note: str = "", parent_id: int | None = None,
                   now: float | None = None) -> int | None:
    """Write a new DRAFT version of the week, with all its blocks, in one transaction.

    Every block is `{kind, start, end}` (epoch seconds) plus optionally `uid` (carried over
    from an earlier version; a new block gets a fresh one), `event_id` and `status`. Unapplied
    drafts older than this one are discarded: there is one current proposal per week. Blocks
    that are new to the plan (no earlier `proposed` event for their uid) get a `proposed`
    event. Returns the plan id, or None when it could not be recorded.
    """
    if created_by not in lifelog.ACTORS:
        raise ValueError(f"unknown actor: {created_by}")
    conn = _ensure()
    if conn is None:
        return None
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM plans WHERE week_start = ?",
                               (week_start,)).fetchone()
            version = int(row["v"]) + 1
            conn.execute("UPDATE plans SET state = ? WHERE week_start = ? AND state = ?",
                         (DISCARDED, week_start, DRAFT))
            cur = conn.execute(
                "INSERT INTO plans (week_start, version, state, rules, created_at, local_date, "
                "created_by, note, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (week_start, version, DRAFT, json.dumps(rules, sort_keys=True), ts, day, created_by,
                 lifelog.short(note), parent_id))
            plan_id = int(cur.lastrowid)
            for b in blocks:
                uid = b.get("uid") or new_uid()
                start, end = float(b["start"]), float(b["end"])
                conn.execute(
                    "INSERT INTO plan_blocks (plan_id, uid, kind, start_at, end_at, local_date, "
                    "event_id, status, live_start, live_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (plan_id, uid, str(b["kind"]), start, end, lifelog.local_date(start),
                     b.get("event_id"), b.get("status") or PLANNED, b.get("live_start"),
                     b.get("live_end")))
                seen = conn.execute("SELECT 1 FROM block_events WHERE block_uid = ? AND event = ? "
                                    "LIMIT 1", (uid, PROPOSED)).fetchone()
                if not seen:
                    conn.execute(
                        "INSERT INTO block_events (block_uid, plan_id, at, local_date, event, actor, "
                        "new_start, new_end, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (uid, plan_id, ts, day, PROPOSED, created_by, start, end, lifelog.short(note)))
        return plan_id
    except Exception as exc:
        log.warning("storage: could not record plan (%s)", exc)
        return None


def get(plan_id: int) -> dict | None:
    """One version with its blocks (`blocks`, in time order), or None."""
    conn = _ensure()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if r is None:
            return None
        plan = dict(r)
        plan["rules"] = _loads(plan["rules"])
        plan["blocks"] = blocks_of(plan_id)
        return plan
    except Exception:
        return None


def blocks_of(plan_id: int) -> list[dict]:
    conn = _ensure()
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM plan_blocks WHERE plan_id = ? ORDER BY start_at, kind, id", (plan_id,))]
    except Exception:
        return []


def _loads(text) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def versions(week_start: str) -> list[dict]:
    """Every version of the week, newest first, without blocks."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        out = []
        for r in conn.execute("SELECT * FROM plans WHERE week_start = ? ORDER BY version DESC",
                              (week_start,)):
            row = dict(r)
            row["rules"] = _loads(row["rules"])
            out.append(row)
        return out
    except Exception:
        return []


def latest(week_start: str, state: str | None = None) -> dict | None:
    """The newest version of the week (in `state`, when given), with its blocks."""
    for row in versions(week_start):
        if state is None or row["state"] == state:
            return get(row["id"])
    return None


def applied(week_start: str) -> dict | None:
    return latest(week_start, APPLIED)


def draft(week_start: str) -> dict | None:
    return latest(week_start, DRAFT)


def current(week_start: str) -> dict | None:
    """The plan the week stands on: the applied version, else the proposal being discussed."""
    return applied(week_start) or draft(week_start)


def latest_live(week_start: str) -> dict | None:
    """The version being worked on: the newest that is still alive (a draft or the applied
    plan). A proposal made on top of an applied plan is newer, so it wins; applying it retires
    the plan it replaced."""
    for row in versions(week_start):
        if row["state"] in (DRAFT, APPLIED):
            return get(row["id"])
    return None


def newest_week(since_week: str = "") -> str | None:
    """The Monday (YYYY-MM-DD) of the week whose plan was written most recently, among weeks from
    `since_week` on: "the plan we are talking about" when he does not name a week."""
    conn = _ensure()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT week_start FROM plans WHERE week_start >= ? AND state IN (?, ?) "
                           "ORDER BY created_at DESC, id DESC LIMIT 1",
                           (since_week or "", DRAFT, APPLIED)).fetchone()
        return row["week_start"] if row else None
    except Exception:
        return None


def newest_draft(since_week: str = "", since_ts: float = 0.0) -> dict | None:
    """The proposal made most recently that is still waiting (not applied, replaced or discarded),
    with its blocks, among weeks from `since_week` on and made at or after `since_ts`."""
    conn = _ensure()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT id FROM plans WHERE week_start >= ? AND state = ? AND created_at >= ? "
                           "ORDER BY created_at DESC, id DESC LIMIT 1",
                           (since_week or "", DRAFT, since_ts)).fetchone()
        return get(row["id"]) if row else None
    except Exception:
        return None


def weeks_with_applied_plans(since_week: str = "") -> list[str]:
    """Mondays (YYYY-MM-DD) that have an applied plan, oldest first, from `since_week` on."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        return [r["week_start"] for r in conn.execute(
            "SELECT DISTINCT week_start FROM plans WHERE state = ? AND week_start >= ? "
            "ORDER BY week_start", (APPLIED, since_week or ""))]
    except Exception:
        return []


def mark_applied(plan_id: int, *, now: float | None = None) -> bool:
    """This version is what the calendar now shows: the earlier applied version of the week is
    superseded and any other unapplied draft is discarded."""
    conn = _ensure()
    if conn is None:
        return False
    ts = time.time() if now is None else now
    try:
        with conn:
            row = conn.execute("SELECT week_start, state FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if row is None or row["state"] not in (DRAFT, APPLIED):
                return False
            conn.execute("UPDATE plans SET state = ?, superseded_at = ? WHERE week_start = ? AND "
                         "state = ? AND id != ?", (SUPERSEDED, ts, row["week_start"], APPLIED, plan_id))
            conn.execute("UPDATE plans SET state = ? WHERE week_start = ? AND state = ? AND id != ?",
                         (DISCARDED, row["week_start"], DRAFT, plan_id))
            conn.execute("UPDATE plans SET state = ?, applied_at = COALESCE(applied_at, ?) "
                         "WHERE id = ?", (APPLIED, ts, plan_id))
        return True
    except Exception as exc:
        log.warning("storage: could not mark plan %s applied (%s)", plan_id, exc)
        return False


def mark_undone(plan_id: int, by_plan_id: int) -> bool:
    """Version `plan_id` has been undone by `by_plan_id`, so the next undo goes one step further
    back. Set once."""
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute("UPDATE plans SET undone_by = ? WHERE id = ? AND undone_by IS NULL",
                               (by_plan_id, plan_id))
        return cur.rowcount == 1
    except Exception:
        return False


def discard(plan_id: int) -> bool:
    """Throw a draft away (a proposal the owner did not want)."""
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute("UPDATE plans SET state = ? WHERE id = ? AND state = ?",
                               (DISCARDED, plan_id, DRAFT))
        return cur.rowcount == 1
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

_BLOCK_FIELDS = {"event_id", "status", "live_start", "live_end"}


def set_block(plan_id: int, uid: str, **fields) -> bool:
    """Update the parts of a block that change after it is planned: its calendar event, its
    status, and where the calendar shows it when the owner moved it. What was PLANNED
    (`start_at`/`end_at`) can never be changed here."""
    bad = set(fields) - _BLOCK_FIELDS
    if bad:
        raise ValueError(f"a planned block's {', '.join(sorted(bad))} cannot be changed")
    if not fields:
        return True
    conn = _ensure()
    if conn is None:
        return False
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with conn:
            cur = conn.execute(f"UPDATE plan_blocks SET {sets} WHERE plan_id = ? AND uid = ?",
                               (*fields.values(), plan_id, uid))
        return cur.rowcount >= 1
    except Exception as exc:
        log.warning("storage: could not update block %s (%s)", uid, exc)
        return False


def add_event(block_uid: str, plan_id: int, event: str, actor: str, *,
              old: tuple[float | None, float | None] = (None, None),
              new: tuple[float | None, float | None] = (None, None),
              reason: str = "", now: float | None = None) -> int | None:
    """Append one thing that happened to a block. Never raises."""
    if actor not in lifelog.ACTORS:
        raise ValueError(f"unknown actor: {actor}")
    conn = _ensure()
    if conn is None:
        return None
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO block_events (block_uid, plan_id, at, local_date, event, actor, "
                "old_start, old_end, new_start, new_end, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (block_uid, plan_id, ts, day, event, actor, old[0], old[1], new[0], new[1],
                 lifelog.short(reason)))
        return int(cur.lastrowid)
    except Exception as exc:
        log.warning("storage: could not record block event (%s)", exc)
        return None


def events(*, block_uid: str | None = None, plan_id: int | None = None,
           week_start: str | None = None) -> list[dict]:
    """Block events, oldest first, filtered by block, plan version or week."""
    conn = _ensure()
    if conn is None:
        return []
    where, args = [], []
    if block_uid:
        where.append("e.block_uid = ?")
        args.append(block_uid)
    if plan_id:
        where.append("e.plan_id = ?")
        args.append(plan_id)
    if week_start:
        where.append("e.plan_id IN (SELECT id FROM plans WHERE week_start = ?)")
        args.append(week_start)
    sql = "SELECT e.* FROM block_events e" + (" WHERE " + " AND ".join(where) if where else "")
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY e.at, e.id", args)]
    except Exception:
        return []


def generation(uid: str) -> int:
    """How many calendar events this block has had so far. The next event's id is derived from
    it, so a retry of a write that may or may not have landed reuses the same id (a duplicate
    is impossible), while writing the block again after it was deleted gets a new one (Google
    keeps a deleted event's id)."""
    conn = _ensure()
    if conn is None:
        return 0
    try:
        return int(conn.execute("SELECT COUNT(*) AS n FROM block_events WHERE block_uid = ? AND "
                                "event IN (?, ?)", (uid, CREATED, RESTORED)).fetchone()["n"])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# P4: what became of a block, settings, and the two views
# ---------------------------------------------------------------------------

def record_outcome(uid: str, plan_id: int, outcome: str, actor: str, *, reason: str = "",
                   now: float | None = None) -> int | None:
    """Append what became of a block (one of OUTCOMES), as a fact: the latest one is the answer,
    earlier ones stay. Never raises."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    return add_event(uid, plan_id, _OUTCOME_PREFIX + outcome, actor, reason=reason, now=now)


def outcomes(uids: Iterable[str]) -> dict[str, dict]:
    """The latest outcome of each block that has one: {uid: {outcome, reason, at, actor}}."""
    wanted = list(dict.fromkeys(uids))
    conn = _ensure()
    if conn is None or not wanted:
        return {}
    out: dict[str, dict] = {}
    try:
        for i in range(0, len(wanted), 400):
            chunk = wanted[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                    "SELECT block_uid, event, reason, at, actor FROM block_events "
                    f"WHERE substr(event, 1, 8) = ? AND block_uid IN ({marks}) ORDER BY at, id",
                    (_OUTCOME_PREFIX, *chunk)):
                out[r["block_uid"]] = {"outcome": r["event"][len(_OUTCOME_PREFIX):], "reason": r["reason"],
                                       "at": r["at"], "actor": r["actor"]}
    except Exception as exc:
        log.debug("storage: could not read outcomes (%s)", exc)
        return {}
    return out


def get_setting(key: str, default=None):
    """A stored setting (any JSON value), or `default`."""
    conn = _ensure()
    if conn is None:
        return default
    try:
        row = conn.execute("SELECT value FROM plan_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
    except Exception:
        return default


def set_setting(key: str, value, *, now: float | None = None) -> bool:
    """Store a setting (settings are configuration, not history: the newest value replaces the
    old one)."""
    conn = _ensure()
    if conn is None:
        return False
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            conn.execute("INSERT INTO plan_settings (key, value, updated_at, local_date) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                         "updated_at = excluded.updated_at, local_date = excluded.local_date",
                         (key, json.dumps(value, sort_keys=True), ts, day))
        return True
    except Exception as exc:
        log.warning("storage: could not save setting %s (%s)", key, exc)
        return False


def block_outcomes(*, week_start: str | None = None, since_date: str | None = None) -> list[dict]:
    """The `v_block_outcomes` view: planned against what happened, one row per block ever applied,
    oldest first. Optionally one week, or from a local date on."""
    conn = _ensure()
    if conn is None:
        return []
    where, args = [], []
    if week_start:
        where.append("week_start = ?")
        args.append(week_start)
    if since_date:
        where.append("planned_date >= ?")
        args.append(since_date)
    sql = "SELECT * FROM v_block_outcomes" + (" WHERE " + " AND ".join(where) if where else "")
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY planned_start, kind", args)]
    except Exception:
        return []


def adherence(*, since_week: str = "", until_week: str = "") -> list[dict]:
    """The `v_adherence` view: per week and kind, how many blocks and how each ended, oldest
    week first."""
    conn = _ensure()
    if conn is None:
        return []
    where, args = [], []
    if since_week:
        where.append("week_start >= ?")
        args.append(since_week)
    if until_week:
        where.append("week_start <= ?")
        args.append(until_week)
    sql = "SELECT * FROM v_adherence" + (" WHERE " + " AND ".join(where) if where else "")
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY week_start, kind", args)]
    except Exception:
        return []


def slips(*, since_date: str) -> list[dict]:
    """Every time a block was reported skipped, from a local date on, oldest first:
    {uid, kind, date, reason}. Read from the events (the facts), not the view, because a skipped
    block that was then rescheduled has a newer outcome but the skip still happened."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT e.block_uid AS uid, e.local_date AS date, e.reason AS reason, "
            "(SELECT b.kind FROM plan_blocks b WHERE b.uid = e.block_uid ORDER BY b.id DESC LIMIT 1) AS kind "
            "FROM block_events e WHERE e.event = ? AND e.local_date >= ? ORDER BY e.at, e.id",
            (_OUTCOME_PREFIX + "skipped", since_date))]
    except Exception:
        return []
