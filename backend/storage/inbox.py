"""Durable conversation ingress (M3, docs/archive/GOALS-ROADMAP.md G3).

A message that arrives while JARVIS is busy is inserted here before anyone tells the owner it
was received. From here it goes one of two ways, and nowhere else:

* it joins the running turn of its own conversation (`target_run_id` is that run), drained
  at the tool loop's safe point; or
* it becomes its own turn once JARVIS is idle, answered with its own reply.

States move forward only:
  pending -> claimed -> processed            (answered)
  pending -> claimed -> failed               (the turn produced nothing; he is told)
  pending -> claimed -> interrupted          (the process died while it was claimed;
                                              reported, never rerun on a guess)
  pending -> cancelled                       (/new abandoned the conversation)

`message_key` is the Discord message id where one exists, so a redelivered message is one
row. The row id orders messages and is the watermark a clarifying question waits beyond.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

PENDING, CLAIMED, PROCESSED, FAILED, INTERRUPTED, CANCELLED = (
    "pending", "claimed", "processed", "failed", "interrupted", "cancelled")


def _conn():
    return db.connect()


def _row(r) -> dict:
    d = dict(r)
    try:
        d["context"] = json.loads(d.get("context_json") or "{}")
    except ValueError:
        d["context"] = {}
    return d


def insert(*, conversation_id: str, text: str, message_key: str | None = None,
           target_run_id: str | None = None, target_goal_id: str | None = None,
           reply_to: str | None = None, context: dict | None = None) -> int:
    """Insert one message; returns its row id (> 0), the existing row's id for a duplicate
    key, or 0 when it could not be stored -- in which case nobody may acknowledge it."""
    conn = _conn()
    if conn is None:
        return 0
    key = message_key or f"local:{uuid.uuid4().hex}"
    now, day = lifelog.stamp()
    ctx = dict(context or {})
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversation_inbox (message_key, conversation_id,"
                " source_message_id, target_run_id, target_goal_id, reply_to_message_id, text,"
                " context_json, state, received_at, local_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, conversation_id, ctx.get("message_id"), target_run_id, target_goal_id,
                 reply_to, text, json.dumps(ctx, default=str), PENDING, now, day))
            r = conn.execute("SELECT rowid FROM conversation_inbox WHERE message_key=?",
                             (key,)).fetchone()
        return int(r[0]) if r else 0
    except Exception as exc:
        log.warning("inbox: message not stored (%s)", exc)
        return 0


def get(message_key: str) -> dict | None:
    """One message by its key, whatever its state (None when unknown or unreadable)."""
    conn = _conn()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT rowid AS seq, * FROM conversation_inbox WHERE message_key=?",
                         (message_key,)).fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def claim_for_run(conversation_id: str, run_id: str | None, after: int = 0) -> list[dict]:
    """Claim, oldest first, the pending messages of this conversation aimed at this run (or,
    with no run, at none). Atomic: two drains can never take the same message."""
    conn = _conn()
    if conn is None:
        return []
    target = "target_run_id IS NULL" if run_id is None else "target_run_id=?"
    args = [conversation_id] + ([] if run_id is None else [run_id]) + [after]
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT rowid AS seq, * FROM conversation_inbox WHERE conversation_id=? AND"
                f" {target} AND state=? AND rowid>? ORDER BY rowid",
                args[:-1] + [PENDING, after]).fetchall()
            now = time.time()
            for r in rows:
                conn.execute("UPDATE conversation_inbox SET state=?, claimed_at=? WHERE"
                             " message_key=? AND state=?", (CLAIMED, now, r["message_key"], PENDING))
        return [_row(r) for r in rows]
    except Exception as exc:
        log.warning("inbox: claim failed (%s)", exc)
        return []


def claim_newer(conversation_id: str, after: int) -> list[dict]:
    """Claim and close every pending message of this conversation newer than `after`,
    whatever run it was aimed at: a reply to a question belongs to the question."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT rowid AS seq, * FROM conversation_inbox WHERE"
                                " conversation_id=? AND state=? AND rowid>? ORDER BY rowid",
                                (conversation_id, PENDING, after)).fetchall()
            now = time.time()
            for r in rows:
                conn.execute("UPDATE conversation_inbox SET state=?, claimed_at=?,"
                             " processed_at=? WHERE message_key=? AND state=?",
                             (PROCESSED, now, now, r["message_key"], PENDING))
        return [_row(r) for r in rows]
    except Exception as exc:
        log.warning("inbox: claim failed (%s)", exc)
        return []


def claim_next() -> dict | None:
    """Claim the oldest pending message of any conversation, for its own turn."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT rowid AS seq, * FROM conversation_inbox WHERE state=?"
                             " ORDER BY rowid LIMIT 1", (PENDING,)).fetchone()
            if r is None:
                return None
            conn.execute("UPDATE conversation_inbox SET state=?, claimed_at=? WHERE"
                         " message_key=? AND state=?", (CLAIMED, time.time(), r["message_key"], PENDING))
        return _row(r)
    except Exception as exc:
        log.warning("inbox: claim failed (%s)", exc)
        return None


def finish(message_key: str, state: str) -> bool:
    """claimed -> processed / failed. Only a claimed message moves; never backwards."""
    conn = _conn()
    if conn is None:
        return False
    with conn:
        cur = conn.execute("UPDATE conversation_inbox SET state=?, processed_at=? WHERE"
                           " message_key=? AND state=?", (state, time.time(), message_key, CLAIMED))
    return cur.rowcount == 1


def finish_run(run_id: str, state: str) -> int:
    """Every message absorbed into run `run_id` takes the run's outcome: processed when it
    completed, interrupted when it was cut off or failed (reported, not rerun)."""
    conn = _conn()
    if conn is None or not run_id:
        return 0
    with conn:
        cur = conn.execute("UPDATE conversation_inbox SET state=?, processed_at=? WHERE"
                           " target_run_id=? AND state=?", (state, time.time(), run_id, CLAIMED))
    return cur.rowcount


def pending(conversation_id: str | None = None) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    sql, args = "SELECT rowid AS seq, * FROM conversation_inbox WHERE state=?", [PENDING]
    if conversation_id:
        sql, args = sql + " AND conversation_id=?", args + [conversation_id]
    return [_row(r) for r in conn.execute(sql + " ORDER BY rowid", args).fetchall()]


def latest_seq(conversation_id: str) -> int:
    conn = _conn()
    if conn is None:
        return 0
    r = conn.execute("SELECT MAX(rowid) FROM conversation_inbox WHERE conversation_id=?",
                     (conversation_id,)).fetchone()
    return int(r[0] or 0)


def cancel_pending(conversation_ids: list[str]) -> int:
    conn = _conn()
    if conn is None or not conversation_ids:
        return 0
    marks = ",".join("?" * len(conversation_ids))
    with conn:
        cur = conn.execute(f"UPDATE conversation_inbox SET state=?, processed_at=? WHERE"
                           f" state=? AND conversation_id IN ({marks})",
                           (CANCELLED, time.time(), PENDING, *conversation_ids))
    return cur.rowcount


def recover() -> list[dict]:
    """At startup: a message still claimed belonged to a turn the dead process was running.
    It may have been half used, so it is marked interrupted and returned for the cut-off
    notice -- never rerun. Pending messages stay pending and are admitted when idle."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn:
            rows = conn.execute("SELECT rowid AS seq, * FROM conversation_inbox WHERE state=?"
                                " ORDER BY rowid", (CLAIMED,)).fetchall()
            conn.execute("UPDATE conversation_inbox SET state=?, processed_at=? WHERE state=?",
                         (INTERRUPTED, time.time(), CLAIMED))
        return [_row(r) for r in rows]
    except Exception as exc:
        log.warning("inbox: recovery failed (%s)", exc)
        return []


def all_rows() -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    return [_row(r) for r in conn.execute(
        "SELECT rowid AS seq, * FROM conversation_inbox ORDER BY rowid").fetchall()]
