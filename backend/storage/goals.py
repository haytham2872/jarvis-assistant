"""Goals (docs/archive/GOALS-ROADMAP.md G1a): canonical records and their transitions.

SQLite is the only authority for a goal: its mandate and criteria, numbered plans,
milestones, bounded sessions, events, notes, captured evidence, versioned files and
budget charges. Schema in `db._MIGRATIONS[2]` (M1).

Contracts this module owns, each tested in `test_goal_storage.py`:

* **A stale session cannot commit.** Every write a session makes (evidence, note, file,
  milestone completion) is conditional on (goal, milestone, generation) and on the goal not
  having ended. A session left over from before a cancel or a re-plan writes nothing.
* **One working session per goal** -- the partial unique index `goal_one_working_session`;
  `claim_session` is the conditional insert that relies on it.
* **Idempotent by op_key.** A note or file write repeated with the same key returns the
  earlier outcome and writes nothing; a file whose content equals the latest revision is a
  no-op. A key reused with a different payload is rejected, never silently overwritten.
* **Recovery marks, never resumes in place.** `recover()` turns sessions left claimed or
  running into `interrupted`, puts their milestones back to ready and holds their charges at
  the full reservation: the work is redone from saved notes and evidence, and its cost was
  real.

Required writes fail closed: a function returns None / a refusal dict when the database is
unavailable, and the runner then does nothing rather than acting without a record.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

# Goal states (the CHECK constraint in M1).
PROPOSED, ACTIVE, WAITING, PAUSED = "proposed", "active", "waiting", "paused"
DONE, PARTIAL, CANCELLED, FAILED = "done", "partial", "cancelled", "failed"
ENDED = frozenset({DONE, PARTIAL, CANCELLED, FAILED})
LIVE = frozenset({ACTIVE, WAITING, PAUSED})

# Milestone states.
READY, WORKING, BLOCKED = "ready", "working", "blocked"
MS_DONE, MS_FAILED, SUPERSEDED = "done", "failed", "superseded"

# Session states.
CLAIMED, RUNNING, COMPLETE, INCOMPLETE, INTERRUPTED, S_FAILED = (
    "claimed", "running", "complete", "incomplete", "interrupted", "failed")

# Disagreements: the findings two sources set against each other.
NOTE_SECTIONS = ("Findings", "Decisions", "Tried and failed", "Open questions", "Next",
                 "Disagreements")
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_TEXT_TYPES = {".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv",
               ".json": "application/json"}
MAX_FILE_CHARS = 60_000          # a goal file is a report, not a dump
MAX_NOTE_CHARS = 2_000


def _conn():
    return db.connect()


def _new(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40].rstrip("-") or "goal"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------------------- goals

def create(*, title: str, words: str, conversation_id: str, criteria: list[str],
           state: str, budget_microusd: int, deadline_at: float | None,
           creation_key: str, actor: str = "owner") -> dict | None:
    """A goal or a proposal (state PROPOSED). Idempotent on `creation_key`: the same key
    returns the record already made, so a repeated tool call does not create a second one."""
    conn = _conn()
    if conn is None:
        return None
    existing = conn.execute("SELECT goal_id FROM goals WHERE creation_key=?",
                            (creation_key,)).fetchone()
    if existing:
        return get(existing["goal_id"])
    goal_id = _new("g")
    now, day = lifelog.stamp()
    try:
        with conn:
            conn.execute(
                "INSERT INTO goals (goal_id, creation_key, title, authorized_words,"
                " conversation_id, state, deadline_at, budget_microusd, folder, created_at,"
                " local_date, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (goal_id, creation_key, lifelog.short(title, 200), words or "",
                 conversation_id or "", state, deadline_at, int(budget_microusd),
                 f"{goal_id}-{_slug(title)}", now, day, now))
            for i, text in enumerate(criteria or [], 1):
                conn.execute(
                    "INSERT INTO goal_criteria (goal_id, criterion_id, text, actor,"
                    " mandate_rev, created_at) VALUES (?,?,?,?,1,?)",
                    (goal_id, f"C{i}", lifelog.short(text, 400), actor, now))
            _event(conn, goal_id, "created", actor, {"state": state}, f"created")
    except Exception as exc:
        log.warning("goals: could not create goal (%s)", exc)
        return None
    return get(goal_id)


def get(goal_id: str) -> dict | None:
    conn = _conn()
    if conn is None or not goal_id:
        return None
    g = _row(conn.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone())
    if g is None:
        return None
    g["criteria"] = [dict(r) for r in conn.execute(
        "SELECT criterion_id, text, actor, active FROM goal_criteria WHERE goal_id=?"
        " AND mandate_rev=? ORDER BY CAST(substr(criterion_id, 2) AS INTEGER)",
        (goal_id, g["mandate_rev"])).fetchall()]
    try:
        g["outcome"] = json.loads(g.get("outcome_json") or "{}")
    except ValueError:
        g["outcome"] = {}
    return g


def active_criteria(goal: dict) -> list[dict]:
    return [c for c in goal.get("criteria", []) if c["active"]]


def list_goals(states=None, limit: int = 20) -> list[dict]:
    """Newest first."""
    conn = _conn()
    if conn is None:
        return []
    where, args = [], []
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args += list(states)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT goal_id FROM goals{clause} ORDER BY created_at DESC LIMIT ?",
                        (*args, limit)).fetchall()
    return [g for g in (get(r["goal_id"]) for r in rows) if g]


def mind_results() -> list[dict]:
    """Strict read of finished goal metadata for the phone's vault map."""
    conn = _conn()
    if conn is None:
        raise RuntimeError("goal storage unavailable")
    return [dict(r) for r in conn.execute(
        "SELECT folder, title, state, created_at, updated_at FROM goals"
        " WHERE state IN (?,?) ORDER BY folder", (DONE, PARTIAL))]


def goal_for_folder(folder: str) -> dict | None:
    """Exact canonical folder lookup; no inference from a goal-looking file name."""
    conn = _conn()
    if conn is None:
        raise RuntimeError("goal storage unavailable")
    return _row(conn.execute("SELECT goal_id, folder FROM goals WHERE folder=?",
                             (folder,)).fetchone())


def latest_session(goal_id: str) -> dict | None:
    """Latest session, including a running one, for read-only progress views."""
    conn = _conn()
    if conn is None:
        return None
    r = conn.execute("SELECT * FROM goal_sessions WHERE goal_id=? ORDER BY started_at DESC,"
                     " rowid DESC LIMIT 1", (goal_id,)).fetchone()
    return dict(r) if r else None


def transition(goal_id: str, new_state: str, *, expect, outcome: dict | None = None,
               actor: str = "jarvis", reason: str = "") -> bool:
    """Conditional state change: only from one of `expect`. Ending a goal bumps the
    generation of every unfinished milestone, so no session still out can write."""
    conn = _conn()
    if conn is None:
        return False
    now = time.time()
    marks = ",".join("?" * len(expect))
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE goals SET state=?, updated_at=?,"
                f" ended_at=CASE WHEN ? THEN ? ELSE ended_at END,"
                f" outcome_json=CASE WHEN ? IS NULL THEN outcome_json ELSE ? END"
                f" WHERE goal_id=? AND state IN ({marks})",
                (new_state, now, new_state in ENDED, now,
                 None if outcome is None else 1, json.dumps(outcome or {}),
                 goal_id, *expect))
            if cur.rowcount != 1:
                return False
            if new_state in ENDED:
                conn.execute(
                    "UPDATE goal_milestones SET generation=generation+1, active_session_id=NULL,"
                    " updated_at=? WHERE goal_id=? AND state NOT IN (?, ?)",
                    (now, goal_id, MS_DONE, SUPERSEDED))
                conn.execute("UPDATE goals SET active_session_id=NULL WHERE goal_id=?",
                             (goal_id,))
                conn.execute("UPDATE goal_waits SET state='cancelled', resolved_at=? WHERE"
                             " goal_id=? AND state='open'", (now, goal_id))
            _event(conn, goal_id, f"state:{new_state}", actor,
                   {"reason": reason[:400]}, f"state:{new_state}:{_new('')}")
        return True
    except Exception as exc:
        log.warning("goals: transition %s -> %s failed (%s)", goal_id, new_state, exc)
        return False


def set_budget(goal_id: str, microusd: int, actor: str, words: str) -> bool:
    conn = _conn()
    if conn is None:
        return False
    with conn:
        cur = conn.execute("UPDATE goals SET budget_microusd=?, updated_at=? WHERE goal_id=?"
                           " AND state NOT IN ('done','partial','cancelled','failed')",
                           (int(microusd), time.time(), goal_id))
        if cur.rowcount == 1:
            _event(conn, goal_id, "budget", actor, {"microusd": int(microusd),
                                                    "words": words[:400]},
                   f"budget:{_new('')}")
    return cur.rowcount == 1


# --------------------------------------------------------------------------- events

def _event(conn, goal_id: str, kind: str, actor: str, payload: dict, key: str) -> None:
    now, day = lifelog.stamp()
    conn.execute(
        "INSERT OR IGNORE INTO goal_events (event_id, goal_id, event_key, kind, actor,"
        " payload_json, created_at, local_date, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (_new("e"), goal_id, key, kind, actor, json.dumps(payload), now, day, now))


def record_event(goal_id: str, kind: str, payload: dict, key: str,
                 actor: str = "jarvis") -> bool:
    """Append one event, once per `key`. True when it was new (a message keyed on it is
    then due); False when it already existed or could not be written."""
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn:
            before = conn.total_changes
            _event(conn, goal_id, kind, actor, payload, key)
            return conn.total_changes > before
    except Exception as exc:
        log.debug("goals: event not written (%s)", exc)
        return False


def add_steer(goal_id: str, *, text: str, words: str, key: str,
              conversation_id: str | None = None, message_id: str | None = None) -> bool:
    """G3: the owner's steering for a goal, as a canonical event keyed by the message it came
    from, so a redelivered or retried message is applied once. False when it already
    existed, the goal has ended, or nothing could be written."""
    conn = _conn()
    if conn is None:
        return False
    now, day = lifelog.stamp()
    try:
        with conn:
            g = conn.execute("SELECT state FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            if g is None or g["state"] in ENDED or g["state"] == PROPOSED:
                return False
            cur = conn.execute(
                "INSERT OR IGNORE INTO goal_events (event_id, goal_id, event_key, kind, actor,"
                " payload_json, source_message_id, conversation_id, created_at, local_date,"
                " applied_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (_new("e"), goal_id, key, "steer", "owner",
                 json.dumps({"text": text[:1500], "words": words[:1500]}), message_id,
                 conversation_id, now, day, now))
        return cur.rowcount == 1
    except Exception as exc:
        log.debug("goals: steer not written (%s)", exc)
        return False


def events(goal_id: str, kind: str, limit: int = 5) -> list[dict]:
    """The latest events of one kind, oldest first."""
    conn = _conn()
    if conn is None:
        return []
    rows = conn.execute("SELECT * FROM goal_events WHERE goal_id=? AND kind=? ORDER BY"
                        " created_at DESC LIMIT ?", (goal_id, kind, limit)).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["payload"] = json.loads(d["payload_json"] or "{}")
        out.append(d)
    return out


def overnight_since(since: float) -> list[dict]:
    """Goal milestones, questions (even answered ones), and confirmed deliveries.

    Delivery *intents* are not claimed as deliveries.
    """
    try:
        conn = _conn()
        if conn is None:
            raise RuntimeError("Goal storage unavailable")
        from backend.storage import delivery
        if delivery._conn() is None:
            raise RuntimeError("Delivery storage unavailable")
        out = []
        for r in conn.execute(
            "SELECT e.event_id AS id, e.goal_id, g.title, e.created_at AS at, e.payload_json"
            " FROM goal_events e JOIN goals g ON g.goal_id=e.goal_id"
            " WHERE e.kind='milestone_done' AND e.created_at>=?", (since,)):
            out.append({**dict(r), "event": "milestone", "payload": json.loads(r["payload_json"])})
        for r in conn.execute(
            "SELECT w.wait_id AS id, w.goal_id, g.title, w.created_at AS at, w.payload_json"
            " FROM goal_waits w JOIN goals g ON g.goal_id=w.goal_id"
            " WHERE w.kind='question' AND w.created_at>=?", (since,)):
            out.append({**dict(r), "event": "question", "payload": json.loads(r["payload_json"])})
        for r in conn.execute(
            "SELECT d.delivery_id AS id, d.goal_id, g.title, i.sent_at AS at, i.body"
            " FROM goal_deliveries d JOIN goals g ON g.goal_id=d.goal_id"
            " JOIN delivery_intents i ON i.intent_id=d.delivery_id"
            " WHERE i.state='sent' AND i.sent_at>=?", (since,)):
            out.append({**dict(r), "event": "delivered", "payload": {"text": r["body"]}})
        return out
    except Exception as exc:
        log.warning("goals: overnight steps could not be read (%s)", exc)
        raise


def has_event(goal_id: str, key: str) -> bool:
    conn = _conn()
    if conn is None:
        return False
    return conn.execute("SELECT 1 FROM goal_events WHERE goal_id=? AND event_key=?",
                        (goal_id, key)).fetchone() is not None


# ----------------------------------------------------------------------- plans

def save_plan(goal_id: str, milestones: list[dict], *, actor: str, reason: str) -> int | None:
    """A new numbered plan revision. Milestones already done with an unchanged spec are kept;
    changed ones get a new generation (stale sessions on them then write nothing); ones
    dropped from the plan are superseded. Returns the new revision."""
    conn = _conn()
    if conn is None:
        return None
    now, day = lifelog.stamp()
    try:
        with conn:
            g = conn.execute("SELECT plan_rev, state FROM goals WHERE goal_id=?",
                             (goal_id,)).fetchone()
            if g is None or g["state"] in ENDED:
                return None
            rev = g["plan_rev"] + 1
            conn.execute("INSERT INTO goal_plans (goal_id, revision, actor, reason, plan_json,"
                         " created_at, local_date) VALUES (?,?,?,?,?,?,?)",
                         (goal_id, rev, actor, reason[:400], json.dumps(milestones), now, day))
            existing = {r["milestone_id"]: dict(r) for r in conn.execute(
                "SELECT * FROM goal_milestones WHERE goal_id=?", (goal_id,)).fetchall()}
            keep = set()
            for m in milestones:
                mid = m["id"]
                keep.add(mid)
                spec = (m["title"], m["expected"], json.dumps(m.get("checks", [])),
                        json.dumps(m.get("criteria", [])), json.dumps(m.get("depends", [])))
                old = existing.get(mid)
                if old and old["state"] == MS_DONE and spec == (
                        old["title"], old["expected"], old["checks_json"],
                        old["criteria_json"], old["dependencies_json"]):
                    conn.execute("UPDATE goal_milestones SET plan_rev=? WHERE goal_id=? AND"
                                 " milestone_id=?", (rev, goal_id, mid))
                    continue
                gen = (old["generation"] + 1) if old else 1
                conn.execute(
                    "INSERT OR REPLACE INTO goal_milestones (goal_id, milestone_id, plan_rev,"
                    " generation, title, expected, checks_json, criteria_json,"
                    " dependencies_json, state, active_session_id, no_progress,"
                    " accepted_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,0,'{}',?)",
                    (goal_id, mid, rev, gen, *spec, READY, now))
            for mid, old in existing.items():
                if mid not in keep and old["state"] != SUPERSEDED:
                    conn.execute("UPDATE goal_milestones SET state=?, generation=generation+1,"
                                 " active_session_id=NULL, updated_at=? WHERE goal_id=? AND"
                                 " milestone_id=?", (SUPERSEDED, now, goal_id, mid))
            conn.execute("UPDATE goals SET plan_rev=?, updated_at=? WHERE goal_id=?",
                         (rev, now, goal_id))
            _event(conn, goal_id, "plan", actor, {"revision": rev, "reason": reason[:200]},
                   f"plan:{rev}")
        return rev
    except Exception as exc:
        log.warning("goals: plan not saved for %s (%s)", goal_id, exc)
        return None


def plan(goal_id: str) -> list[dict]:
    """The current plan's milestones, in plan order, with their live state."""
    conn = _conn()
    if conn is None:
        return []
    p = conn.execute("SELECT plan_json FROM goal_plans WHERE goal_id=? ORDER BY revision DESC"
                     " LIMIT 1", (goal_id,)).fetchone()
    if p is None:
        return []
    order = [m["id"] for m in json.loads(p["plan_json"])]
    rows = {r["milestone_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM goal_milestones WHERE goal_id=?", (goal_id,)).fetchall()}
    out = []
    for mid in order:
        r = rows.get(mid)
        if r is None:
            continue
        for k in ("checks_json", "criteria_json", "dependencies_json", "accepted_json"):
            try:
                r[k[:-5]] = json.loads(r[k] or ("{}" if k == "accepted_json" else "[]"))
            except ValueError:
                r[k[:-5]] = {} if k == "accepted_json" else []
        out.append(r)
    return out


def ready_milestone(goal_id: str) -> dict | None:
    """The first milestone in plan order that is ready and whose dependencies are done."""
    ms = plan(goal_id)
    done = {m["milestone_id"] for m in ms if m["state"] == MS_DONE}
    for m in ms:
        if m["state"] == READY and all(d in done for d in m["dependencies"]):
            return m
    return None


def _guard_sql() -> str:
    return ("EXISTS (SELECT 1 FROM goal_milestones m JOIN goals g ON g.goal_id=m.goal_id"
            " WHERE m.goal_id=? AND m.milestone_id=? AND m.generation=?"
            " AND g.state NOT IN ('done','partial','cancelled','failed'))")


def guard_ok(goal_id: str, milestone_id: str, generation: int) -> bool:
    conn = _conn()
    if conn is None:
        return False
    return bool(conn.execute(f"SELECT {_guard_sql()}",
                             (goal_id, milestone_id, generation)).fetchone()[0])


# --------------------------------------------------------------------- sessions

def claim_session(goal_id: str, milestone_id: str, generation: int, run_id: str) -> str | None:
    """Claim the one working session for this goal. None when the goal is not active, the
    milestone moved on (another generation, no longer ready) or a session already works."""
    conn = _conn()
    if conn is None:
        return None
    session_id = _new("s")
    now, day = lifelog.stamp()
    try:
        with conn:
            g = conn.execute("SELECT state, plan_rev FROM goals WHERE goal_id=?",
                             (goal_id,)).fetchone()
            if g is None or g["state"] != ACTIVE:
                return None
            cur = conn.execute(
                "UPDATE goal_milestones SET state=?, active_session_id=?, updated_at=?"
                " WHERE goal_id=? AND milestone_id=? AND generation=? AND state=?",
                (WORKING, session_id, now, goal_id, milestone_id, generation, READY))
            if cur.rowcount != 1:
                raise _Refused()
            conn.execute(
                "INSERT INTO goal_sessions (session_id, run_id, goal_id, milestone_id,"
                " generation, plan_rev, state, started_at, local_date) VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (session_id, run_id, goal_id, milestone_id, generation, g["plan_rev"],
                 CLAIMED, now, day))
            conn.execute("UPDATE goals SET active_session_id=?, updated_at=? WHERE goal_id=?",
                         (session_id, now, goal_id))
        return session_id
    except _Refused:
        return None
    except Exception as exc:          # the unique index: a session already works this goal
        log.info("goals: session not claimed for %s (%s)", goal_id, exc)
        return None


class _Refused(Exception):
    pass


def session(session_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    return _row(conn.execute("SELECT * FROM goal_sessions WHERE session_id=?",
                             (session_id,)).fetchone())


def session_count(goal_id: str, milestone_id: str, generation: int) -> int:
    """Sessions run on one milestone in one generation, whatever their outcome."""
    conn = _conn()
    if conn is None:
        return 0
    return conn.execute("SELECT count(*) FROM goal_sessions WHERE goal_id=? AND milestone_id=?"
                        " AND generation=?", (goal_id, milestone_id, generation)).fetchone()[0]


def mark_running(session_id: str) -> None:
    conn = _conn()
    if conn is not None:
        with conn:
            conn.execute("UPDATE goal_sessions SET state=? WHERE session_id=? AND state=?",
                         (RUNNING, session_id, CLAIMED))


def end_session(session_id: str, state: str, *, ending: dict, fingerprint: str | None,
                output_tokens: int = 0, release: bool = True,
                no_progress: int | None = None, release_to: str = READY) -> bool:
    """Close a session and (by default) put its milestone back to ready -- conditional on the
    milestone still belonging to this session, so a stale session cannot release a newer one."""
    conn = _conn()
    if conn is None:
        return False
    now = time.time()
    with conn:
        s = conn.execute("SELECT * FROM goal_sessions WHERE session_id=?", (session_id,)).fetchone()
        if s is None or s["state"] not in (CLAIMED, RUNNING):
            return False
        conn.execute("UPDATE goal_sessions SET state=?, ended_at=?, ending_json=?,"
                     " progress_fingerprint=?, output_tokens=? WHERE session_id=?",
                     (state, now, json.dumps(ending), fingerprint, int(output_tokens), session_id))
        if release:
            conn.execute(
                "UPDATE goal_milestones SET state=?, active_session_id=NULL, updated_at=?,"
                " no_progress=COALESCE(?, no_progress)"
                " WHERE goal_id=? AND milestone_id=? AND generation=? AND active_session_id=?"
                " AND state=?",
                (release_to, now, no_progress, s["goal_id"], s["milestone_id"], s["generation"],
                 session_id, WORKING))
        conn.execute("UPDATE goals SET active_session_id=NULL WHERE goal_id=? AND"
                     " active_session_id=?", (s["goal_id"], session_id))
    return True


def complete_milestone(goal_id: str, milestone_id: str, generation: int, session_id: str,
                       accepted: dict) -> bool:
    """milestone_done after the checks passed. Conditional on generation and session."""
    conn = _conn()
    if conn is None:
        return False
    with conn:
        cur = conn.execute(
            f"UPDATE goal_milestones SET state=?, accepted_json=?, active_session_id=NULL,"
            f" no_progress=0, updated_at=? WHERE goal_id=? AND milestone_id=? AND generation=?"
            f" AND active_session_id=? AND {_guard_sql()}",
            (MS_DONE, json.dumps(accepted), time.time(), goal_id, milestone_id, generation,
             session_id, goal_id, milestone_id, generation))
        if cur.rowcount == 1:
            _event(conn, goal_id, "milestone_done", "jarvis",
                   {"milestone": milestone_id, "accepted": accepted},
                   f"milestone_done:{milestone_id}:{generation}")
    return cur.rowcount == 1


def recover() -> list[dict]:
    """At startup, before the runner: sessions the previous process left claimed or running
    become interrupted, their milestones go back to ready, and their charges are held at the
    full reservation. Returns the interrupted sessions."""
    conn = _conn()
    if conn is None:
        return []
    now = time.time()
    try:
        with conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM goal_sessions WHERE state IN (?, ?)", (CLAIMED, RUNNING)).fetchall()]
            for s in rows:
                conn.execute("UPDATE goal_sessions SET state=?, ended_at=? WHERE session_id=?",
                             (INTERRUPTED, now, s["session_id"]))
                conn.execute("UPDATE goal_milestones SET state=?, active_session_id=NULL,"
                             " updated_at=? WHERE goal_id=? AND milestone_id=? AND"
                             " active_session_id=? AND state=?",
                             (READY, now, s["goal_id"], s["milestone_id"], s["session_id"], WORKING))
                conn.execute("UPDATE goals SET active_session_id=NULL WHERE goal_id=? AND"
                             " active_session_id=?", (s["goal_id"], s["session_id"]))
                conn.execute("UPDATE goal_charges SET state='held', settled_microusd="
                             "reserved_microusd, settled_at=? WHERE session_id=? AND"
                             " state='reserved'", (now, s["session_id"]))
                _event(conn, s["goal_id"], "session_interrupted", "system",
                       {"session": s["session_id"]}, f"interrupted:{s['session_id']}")
            # A card post the previous process was making when it died: its outcome is
            # unknown, so it is never repeated (G1b).
            conn.execute("UPDATE goals SET card_message_id='uncertain' WHERE"
                         " card_message_id='pending'")
        return rows
    except Exception as exc:
        log.warning("goals: recovery failed (%s)", exc)
        return []


# --------------------------------------------------------------------- evidence

def save_evidence(goal_id: str, milestone_id: str, generation: int, *, session_id: str,
                  run_id: str | None, tool: str, args: dict, body: str,
                  metadata: dict) -> str | None:
    """The complete tool return, saved by code (never a preview). Guarded."""
    conn = _conn()
    if conn is None:
        return None
    evidence_id = _new("ev")
    now, day = lifelog.stamp()
    with conn:
        cur = conn.execute(
            f"INSERT INTO goal_evidence (evidence_id, goal_id, session_id, run_id, tool,"
            f" arguments_json, body, body_hash, metadata_json, captured_at, local_date)"
            f" SELECT ?,?,?,?,?,?,?,?,?,?,? WHERE {_guard_sql()}",
            (evidence_id, goal_id, session_id, run_id, tool,
             json.dumps(args, default=str)[:4000], body or "", _sha(body or ""),
             json.dumps(metadata, default=str), now, day, goal_id, milestone_id, generation))
    return evidence_id if cur.rowcount == 1 else None


def get_evidence(goal_id: str, evidence_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    r = _row(conn.execute("SELECT * FROM goal_evidence WHERE goal_id=? AND evidence_id=?",
                          (goal_id, evidence_id)).fetchone())
    if r:
        r["metadata"] = json.loads(r["metadata_json"] or "{}")
    return r


def evidence_index(goal_id: str) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT evidence_id, tool, arguments_json, length(body) AS chars, metadata_json,"
        " captured_at, local_date FROM goal_evidence WHERE goal_id=? ORDER BY captured_at",
        (goal_id,)).fetchall()]


# ------------------------------------------------------------------------ notes

def _replay(conn, table: str, goal_id: str, op_key: str, payload_hash: str):
    """An earlier write under this key: its row, or a refusal when the payload differs."""
    r = conn.execute(f"SELECT * FROM {table} WHERE goal_id=? AND op_key=?",
                     (goal_id, op_key)).fetchone()
    if r is None:
        return None
    r = dict(r)
    stored = r.get("content_hash") if table == "goal_files" else _sha(
        json.dumps([r["section"], r["body"], r["removed"]]))
    if table == "goal_notes" and stored != payload_hash:
        return {"refused": "this op_key was already used for a different note"}
    if table == "goal_files" and stored != payload_hash:
        return {"refused": "this op_key was already used for different file content"}
    return r


def save_note(goal_id: str, milestone_id: str, generation: int, *, op: str, op_key: str,
              section: str = "", text: str = "", entry_id: str = "",
              expected_revision: int | None = None, evidence_ids=None) -> dict:
    """add / revise / remove one note entry. Returns {"entry_id","revision","replayed"?} or
    {"refused": reason}."""
    conn = _conn()
    if conn is None:
        return {"refused": "storage unavailable"}
    if op == "add" and section not in NOTE_SECTIONS:
        return {"refused": f"section must be one of {', '.join(NOTE_SECTIONS)}"}
    text = (text or "").strip()
    if op in ("add", "revise") and not text:
        return {"refused": "a note needs text"}
    if len(text) > MAX_NOTE_CHARS:
        return {"refused": f"a note is at most {MAX_NOTE_CHARS} characters; split it"}
    refs = [e for e in (evidence_ids or []) if isinstance(e, str)]
    now, day = lifelog.stamp()
    with conn:
        if not conn.execute(f"SELECT {_guard_sql()}", (goal_id, milestone_id, generation)).fetchone()[0]:
            return {"refused": "this session is no longer current (the goal or milestone moved on)"}
        for e in refs:
            if not conn.execute("SELECT 1 FROM goal_evidence WHERE goal_id=? AND evidence_id=?",
                                (goal_id, e)).fetchone():
                return {"refused": f"evidence {e} does not belong to this goal"}
        if op == "add":
            payload = _sha(json.dumps([section, text, 0]))
        else:
            cur_entry = conn.execute(
                "SELECT * FROM goal_notes WHERE goal_id=? AND entry_id=? ORDER BY revision DESC"
                " LIMIT 1", (goal_id, entry_id)).fetchone()
            if cur_entry is None:
                return {"refused": f"no note {entry_id}"}
            section = cur_entry["section"]
            payload = _sha(json.dumps([section, text if op == "revise" else cur_entry["body"],
                                       1 if op == "remove" else 0]))
        prior = _replay(conn, "goal_notes", goal_id, op_key, payload)
        if prior is not None:
            if "refused" in prior:
                return prior
            return {"entry_id": prior["entry_id"], "revision": prior["revision"], "replayed": True}
        if op == "add":
            entry_id, revision, body, removed = _new("n"), 1, text, 0
        else:
            if expected_revision is not None and cur_entry["revision"] != expected_revision:
                return {"refused": f"note {entry_id} is at revision {cur_entry['revision']}, "
                                   f"not {expected_revision}; read it again"}
            revision = cur_entry["revision"] + 1
            body = text if op == "revise" else cur_entry["body"]
            removed = 1 if op == "remove" else 0
        conn.execute("INSERT INTO goal_notes (goal_id, entry_id, revision, section, body,"
                     " source_refs_json, removed, op_key, created_at, local_date)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (goal_id, entry_id, revision, section, body, json.dumps(refs), removed,
                      op_key, now, day))
    return {"entry_id": entry_id, "revision": revision}


def notes(goal_id: str) -> list[dict]:
    """Current entries (latest revision of each, removed ones left out), oldest first."""
    conn = _conn()
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT n.* FROM goal_notes n JOIN (SELECT entry_id, max(revision) AS r FROM goal_notes"
        " WHERE goal_id=? GROUP BY entry_id) l ON l.entry_id=n.entry_id AND l.r=n.revision"
        " WHERE n.goal_id=? AND n.removed=0 ORDER BY n.created_at", (goal_id, goal_id)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_ids"] = json.loads(d["source_refs_json"] or "[]")
        out.append(d)
    return out


# ------------------------------------------------------------------------ files

def valid_name(name: str) -> str | None:
    """A refusal reason, or None. A plain logical name with a text extension: no paths."""
    n = (name or "").strip()
    if not _NAME_OK.match(n) or ".." in n or "/" in n or "\\" in n:
        # Live (G1a, 2026-10-05): told only "no path", the model retried "notes/x.md" and spent
        # its session's steps. The refusal names the name to use.
        flat = re.sub(r"[^A-Za-z0-9 ._-]", "", re.split(r"[\\/]", n)[-1]).lstrip(".")
        hint = f" -- use {flat!r}" if flat and _NAME_OK.match(flat) else ""
        return f"a goal file has a plain name with no folders{hint}"
    if not any(n.lower().endswith(ext) for ext in _TEXT_TYPES):
        return f"a file must end in one of {', '.join(_TEXT_TYPES)}"
    return None


def save_file(goal_id: str, milestone_id: str, generation: int, *, name: str, content: str,
              expected_revision: int | None, op_key: str, source_refs=None) -> dict:
    """A new revision of a goal file. {"revision","replayed"|"unchanged"?} or {"refused"}."""
    bad = valid_name(name)
    if bad:
        return {"refused": bad}
    if len(content or "") > MAX_FILE_CHARS:
        return {"refused": f"a goal file is at most {MAX_FILE_CHARS:,} characters"}
    conn = _conn()
    if conn is None:
        return {"refused": "storage unavailable"}
    digest = _sha(content or "")
    now, day = lifelog.stamp()
    with conn:
        if not conn.execute(f"SELECT {_guard_sql()}", (goal_id, milestone_id, generation)).fetchone()[0]:
            return {"refused": "this session is no longer current (the goal or milestone moved on)"}
        prior = _replay(conn, "goal_files", goal_id, op_key, digest)
        if prior is not None:
            if "refused" in prior:
                return prior
            return {"revision": prior["revision"], "replayed": True}
        latest = conn.execute("SELECT revision, content_hash FROM goal_files WHERE goal_id=? AND"
                              " name=? ORDER BY revision DESC LIMIT 1", (goal_id, name)).fetchone()
        current = latest["revision"] if latest else None
        if current != expected_revision:
            return {"refused": (f"{name} is at revision {current}; read it first" if current
                                else f"{name} does not exist yet: write it with expected_revision null")}
        if latest and latest["content_hash"] == digest:
            return {"revision": current, "unchanged": True}
        rev = (current or 0) + 1
        ext = next(e for e in _TEXT_TYPES if name.lower().endswith(e))
        refs = [r for r in (source_refs or []) if isinstance(r, str)]
        g = conn.execute("SELECT folder FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        conn.execute("INSERT INTO goal_files (goal_id, name, revision, relative_path, mime_type,"
                     " body, byte_length, content_hash, source_refs_json, op_key, created_at,"
                     " local_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (goal_id, name, rev, f"goals/{g['folder']}/{name}", _TEXT_TYPES[ext],
                      content or "", len((content or "").encode("utf-8")), digest,
                      json.dumps(refs), op_key, now, day))
    return {"revision": rev}


def files(goal_id: str) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT f.name, f.revision, f.byte_length, f.created_at FROM goal_files f JOIN"
        " (SELECT name, max(revision) AS r FROM goal_files WHERE goal_id=? GROUP BY name) l"
        " ON l.name=f.name AND l.r=f.revision WHERE f.goal_id=? ORDER BY f.name",
        (goal_id, goal_id)).fetchall()]


def read_file(goal_id: str, name: str, revision: int | None = None) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    if revision:
        r = conn.execute("SELECT * FROM goal_files WHERE goal_id=? AND name=? AND revision=?",
                         (goal_id, name, revision)).fetchone()
    else:
        r = conn.execute("SELECT * FROM goal_files WHERE goal_id=? AND name=? ORDER BY revision"
                         " DESC LIMIT 1", (goal_id, name)).fetchone()
    return _row(r)


# ---------------------------------------------------------------------- charges

def insert_charge(goal_id: str, purpose: str, reserved_microusd: int,
                  session_id: str | None = None, conn=None) -> str | None:
    """Insert one reservation inside the caller's transaction (goal_budget.reserve)."""
    charge_id = _new("c")
    now, day = lifelog.stamp()
    conn.execute("INSERT INTO goal_charges (charge_id, goal_id, session_id, purpose,"
                 " reserved_microusd, state, created_at, local_date) VALUES (?,?,?,?,?,?,?,?)",
                 (charge_id, goal_id, session_id, purpose, int(reserved_microusd), "reserved",
                  now, day))
    return charge_id


def charges(goal_id: str) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    return [dict(r) for r in conn.execute("SELECT * FROM goal_charges WHERE goal_id=? ORDER BY"
                                          " created_at", (goal_id,)).fetchall()]


# ------------------------------------------------------------------------- card

def card_id(goal_id: str) -> str | None:
    conn = _conn()
    if conn is None:
        return None
    r = conn.execute("SELECT card_message_id FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
    return r["card_message_id"] if r else None


def set_card(goal_id: str, value: str | None, *, expect) -> bool:
    """Conditional: only from the value `expect` (None = no card yet), so two refreshes can
    never both decide to post. Values: a Discord message id, "pending" (a post in flight,
    recorded before sending) or "uncertain" (a post whose outcome is unknown: never repeated)."""
    conn = _conn()
    if conn is None:
        return False
    with conn:
        if expect is None:
            cur = conn.execute("UPDATE goals SET card_message_id=? WHERE goal_id=? AND"
                               " card_message_id IS NULL", (value, goal_id))
        else:
            cur = conn.execute("UPDATE goals SET card_message_id=? WHERE goal_id=? AND"
                               " card_message_id=?", (value, goal_id, expect))
    return cur.rowcount == 1


# =========================================================================== G2

WAITING_MS = "waiting"                 # a milestone with an open wait (time, event, job, approval)
OPEN, RESOLVED, W_CANCELLED = "open", "resolved", "cancelled"


def set_milestone(goal_id: str, milestone_id: str, generation: int, state: str, *,
                  expect: tuple) -> bool:
    """Conditional milestone state change (waiting/blocked <-> ready), on its generation."""
    conn = _conn()
    if conn is None:
        return False
    marks = ",".join("?" * len(expect))
    with conn:
        cur = conn.execute(
            f"UPDATE goal_milestones SET state=?, updated_at=? WHERE goal_id=? AND"
            f" milestone_id=? AND generation=? AND state IN ({marks})",
            (state, time.time(), goal_id, milestone_id, generation, *expect))
    return cur.rowcount == 1


# ------------------------------------------------------------------------- waits

def add_wait(goal_id: str, milestone_id: str, generation: int, *, kind: str, key: str,
             payload: dict, due_at: float | None = None, expires_at: float | None = None) -> dict:
    """Register a wait once per (goal, key). The same key returns the wait already there;
    the same key with a different payload is refused. Guarded by generation."""
    conn = _conn()
    if conn is None:
        return {"refused": "storage unavailable"}
    body = json.dumps(payload, sort_keys=True, default=str)
    now, day = lifelog.stamp()
    with conn:
        prior = conn.execute("SELECT * FROM goal_waits WHERE goal_id=? AND correlation_key=?",
                             (goal_id, key)).fetchone()
        if prior is not None:
            if prior["payload_json"] != body:
                return {"refused": "this op_key was already used for a different wait"}
            return {**dict(prior), "replayed": True}
        if not conn.execute(f"SELECT {_guard_sql()}", (goal_id, milestone_id, generation)).fetchone()[0]:
            return {"refused": "this session is no longer current"}
        wait_id = _new("w")
        conn.execute("INSERT INTO goal_waits (wait_id, goal_id, milestone_id, generation, kind,"
                     " correlation_key, state, payload_json, due_at, expires_at, created_at,"
                     " local_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (wait_id, goal_id, milestone_id, generation, kind, key, OPEN, body,
                      due_at, expires_at, now, day))
    return dict(conn.execute("SELECT * FROM goal_waits WHERE wait_id=?", (wait_id,)).fetchone())


def get_wait(wait_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    r = _row(conn.execute("SELECT * FROM goal_waits WHERE wait_id=?", (wait_id,)).fetchone())
    if r:
        r["payload"] = json.loads(r["payload_json"] or "{}")
    return r


def waits(goal_id: str | None = None, state: str | None = OPEN) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    sql, args = "SELECT * FROM goal_waits WHERE 1=1", []
    if goal_id:
        sql, args = sql + " AND goal_id=?", args + [goal_id]
    if state:
        sql, args = sql + " AND state=?", args + [state]
    out = []
    for r in conn.execute(sql + " ORDER BY created_at", args).fetchall():
        d = dict(r)
        d["payload"] = json.loads(d["payload_json"] or "{}")
        out.append(d)
    return out


def resolve_wait(wait_id: str, outcome: dict, state: str = RESOLVED) -> bool:
    """open -> resolved/cancelled, exactly once."""
    conn = _conn()
    if conn is None:
        return False
    with conn:
        cur = conn.execute("UPDATE goal_waits SET state=?, resolved_at=?, outcome_json=? WHERE"
                           " wait_id=? AND state=?",
                           (state, time.time(), json.dumps(outcome, default=str), wait_id, OPEN))
    return cur.rowcount == 1


def resolved_waits(goal_id: str, milestone_id: str, since: float = 0.0) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    out = []
    for r in conn.execute("SELECT * FROM goal_waits WHERE goal_id=? AND milestone_id=? AND"
                          " state=? AND resolved_at>=? ORDER BY resolved_at",
                          (goal_id, milestone_id, RESOLVED, since)).fetchall():
        d = dict(r)
        d["payload"] = json.loads(d["payload_json"] or "{}")
        d["outcome"] = json.loads(d["outcome_json"] or "{}")
        out.append(d)
    return out


# ----------------------------------------------------------------------- effects

def effect_begin(goal_id: str, milestone_id: str, generation: int, *, kind: str,
                 logical_key: str, request: dict, quota_meter: str | None = None,
                 quota_units: int = 0) -> tuple[dict | None, str]:
    """Record a cross-store action BEFORE it is attempted. ("created" | "existing" |
    "conflict" | "refused"). An existing effect under the same key is returned whatever its
    state -- a launch whose acceptance was lost is never launched again."""
    conn = _conn()
    if conn is None:
        return None, "refused"
    body = json.dumps(request, sort_keys=True, default=str)
    now, day = lifelog.stamp()
    with conn:
        prior = conn.execute("SELECT * FROM goal_effects WHERE goal_id=? AND logical_key=?",
                             (goal_id, logical_key)).fetchone()
        if prior is not None:
            return dict(prior), ("existing" if prior["request_json"] == body else "conflict")
        if not conn.execute(f"SELECT {_guard_sql()}", (goal_id, milestone_id, generation)).fetchone()[0]:
            return None, "refused"
        effect_id = _new("x")
        conn.execute("INSERT INTO goal_effects (effect_id, goal_id, milestone_id, generation,"
                     " kind, logical_key, request_json, state, quota_meter, quota_units,"
                     " created_at, local_date, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (effect_id, goal_id, milestone_id, generation, kind, logical_key, body,
                      "starting", quota_meter, int(quota_units), now, day, now))
    return dict(conn.execute("SELECT * FROM goal_effects WHERE effect_id=?",
                             (effect_id,)).fetchone()), "created"


def effect_update(effect_id: str, state: str, *, target_id: str | None = None,
                  result: dict | None = None) -> None:
    conn = _conn()
    if conn is None:
        return
    with conn:
        conn.execute("UPDATE goal_effects SET state=?, target_id=COALESCE(?, target_id),"
                     " result_json=COALESCE(?, result_json), updated_at=? WHERE effect_id=?",
                     (state, target_id, None if result is None else json.dumps(result, default=str),
                      time.time(), effect_id))


def effects(goal_id: str, kind: str | None = None) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    sql, args = "SELECT * FROM goal_effects WHERE goal_id=?", [goal_id]
    if kind:
        sql, args = sql + " AND kind=?", args + [kind]
    return [dict(r) for r in conn.execute(sql + " ORDER BY created_at", args).fetchall()]


def quota_used_today(meter: str) -> int:
    """Units of a quota counted by goal effects today, uncertain launches included."""
    conn = _conn()
    if conn is None:
        return 0
    return int(conn.execute("SELECT COALESCE(SUM(quota_units), 0) FROM goal_effects WHERE"
                            " quota_meter=? AND local_date=?",
                            (meter, lifelog.stamp()[1])).fetchone()[0])


# ---------------------------------------------------------------------- bindings

def bind(kind: str, target_id: str, goal_id: str, milestone_id: str, generation: int,
         effect_id: str, wait_id: str | None = None) -> bool:
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO goal_bindings (kind, target_id, goal_id,"
                         " milestone_id, generation, effect_id, wait_id, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (kind, str(target_id), goal_id, milestone_id, generation, effect_id,
                          wait_id, time.time()))
        return True
    except Exception as exc:
        log.warning("goals: binding %s %s not written (%s)", kind, target_id, exc)
        return False


def binding(kind: str, target_id: str) -> dict | None:
    conn = _conn()
    if conn is None:
        return None
    return _row(conn.execute("SELECT * FROM goal_bindings WHERE kind=? AND target_id=? AND active=1",
                             (kind, str(target_id))).fetchone())


def bindings(goal_id: str, kind: str | None = None) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    sql, args = "SELECT * FROM goal_bindings WHERE goal_id=? AND active=1", [goal_id]
    if kind:
        sql, args = sql + " AND kind=?", args + [kind]
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def unbind(kind: str, target_id: str) -> None:
    conn = _conn()
    if conn is not None:
        with conn:
            conn.execute("UPDATE goal_bindings SET active=0 WHERE kind=? AND target_id=?",
                         (kind, str(target_id)))


# --------------------------------------------------------------------- deliveries

def link_delivery(goal_id: str, event_key: str, purpose: str, delivery_id: str) -> None:
    conn = _conn()
    if conn is not None:
        with conn:
            conn.execute("INSERT OR IGNORE INTO goal_deliveries (goal_id, event_id, purpose,"
                         " delivery_id, created_at) VALUES (?,?,?,?,?)",
                         (goal_id, event_key, purpose, delivery_id, time.time()))


def sent(goal_id: str) -> list[dict]:
    """What the owner was actually sent for this goal: only deliveries whose every part is
    confirmed (delivery_intents state 'sent'). Never what the model says it sent."""
    conn = _conn()
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT d.purpose, d.delivery_id, i.sent_at, i.body FROM goal_deliveries d JOIN"
            " delivery_intents i ON i.intent_id=d.delivery_id WHERE d.goal_id=? AND"
            " i.state='sent' ORDER BY i.sent_at", (goal_id,)).fetchall()]
    except Exception:
        return []
