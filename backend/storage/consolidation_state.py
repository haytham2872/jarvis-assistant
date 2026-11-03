"""A05 slice 1: per-output consolidation state, so a partial failure is recoverable.

The roadmap's issue: *"Memory can be partially saved, silently overwritten, prematurely
marked consolidated"*, with the root cause *"consolidation uses a file-exists completion
test"*. Acceptance: *"Fail one consolidation write, restart, and complete only missing
work without duplicate append artifacts; uncovered facts remain pending."*

All three parts were live, and reproduced together:

1. `consolidate_session` wrote its six vault outputs with
   `asyncio.gather(..., return_exceptions=True)` and **never looked at the result** —
   and `vault_write` reports failure by *returning an error string*, not raising, so the
   failure was discarded twice over.
2. Facts were then marked consolidated **unconditionally**, removing them from `recall()`
   even though the vault never received the content they would have been recoverable
   from.
3. Completion was tested by "does a letter file for this session exist" — so a restart
   with a healthy disk skipped everything and never repaired the gap.

Net effect of one transient write failure: that session's portrait update was lost
permanently, along with the raw facts that could have regenerated it.

This module records, per session: the generated **proposal** (saved *before* anything is
applied, so a restart completes the missing work from the same content instead of paying
for a second LLM call that would produce something different), which **outputs** have
landed, and whether the facts have been marked. `mood-patterns.md` is an *append*, which
is exactly why per-output state is needed rather than blanket retry — re-running it would
duplicate the entry, the "duplicate append artifacts" the acceptance criterion names.

Same silent-degradation contract as the rest of storage: every function returns a safe
default when the database is unavailable, and consolidation then behaves as it did
before this existed rather than failing.
"""
from __future__ import annotations

import json
import logging
import time

from backend.storage import db

log = logging.getLogger(__name__)

DONE = "done"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consolidation_runs (
    session_id   TEXT PRIMARY KEY,
    proposal     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    facts_marked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS consolidation_outputs (
    session_id   TEXT NOT NULL,
    output       TEXT NOT NULL,
    state        TEXT NOT NULL,
    detail       TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (session_id, output)
);
"""


def _ensure():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: consolidation tables unavailable (%s)", exc)
        return None


def save_proposal(session_id: str, proposal: dict) -> bool:
    """Persist the generated proposal BEFORE any of it is applied.

    This is what makes a restart able to finish the job: without it, recovery would mean
    a second LLM call, which costs money and — more importantly — would produce
    *different* content, so the letter already on disk and the portrait written on the
    retry would describe the session differently.

    An existing row is kept, never overwritten: the first proposal is the one partially
    applied, and replacing it would strand whatever already landed.
    """
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO consolidation_runs (session_id, proposal, "
                "created_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(proposal), time.time()))
        return True
    except Exception as exc:
        log.debug("storage: save_proposal failed (%s)", exc)
        return False


def load_proposal(session_id: str) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT proposal FROM consolidation_runs WHERE session_id = ?",
            (session_id,)).fetchone()
        return json.loads(row[0]) if row else None
    except Exception as exc:
        log.debug("storage: load_proposal failed (%s)", exc)
        return None


def record_output(session_id: str, output: str, ok: bool,
                  detail: str | None = None) -> None:
    """Record one output's fate. Never raises — a bookkeeping failure must not take
    down a consolidation that is otherwise succeeding."""
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO consolidation_outputs (session_id, output, state, detail, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, output) DO UPDATE SET "
                "state = excluded.state, detail = excluded.detail, "
                "updated_at = excluded.updated_at",
                (session_id, output, DONE if ok else FAILED, (detail or "")[:500],
                 time.time()))
    except Exception as exc:
        log.debug("storage: record_output failed (%s)", exc)


def completed_outputs(session_id: str) -> set[str]:
    """Outputs already safely applied. A retry must skip exactly these."""
    conn = _ensure()
    if conn is None:
        return set()
    try:
        rows = conn.execute(
            "SELECT output FROM consolidation_outputs WHERE session_id = ? AND state = ?",
            (session_id, DONE)).fetchall()
        return {r[0] for r in rows}
    except Exception as exc:
        log.debug("storage: completed_outputs failed (%s)", exc)
        return set()


def failed_outputs(session_id: str) -> dict[str, str]:
    conn = _ensure()
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT output, detail FROM consolidation_outputs WHERE session_id = ? "
            "AND state = ?", (session_id, FAILED)).fetchall()
        return {r[0]: (r[1] or "") for r in rows}
    except Exception as exc:
        log.debug("storage: failed_outputs failed (%s)", exc)
        return {}


def mark_facts_done(session_id: str) -> None:
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "UPDATE consolidation_runs SET facts_marked = 1 WHERE session_id = ?",
                (session_id,))
    except Exception as exc:
        log.debug("storage: mark_facts_done failed (%s)", exc)


def facts_marked(session_id: str) -> bool:
    conn = _ensure()
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT facts_marked FROM consolidation_runs WHERE session_id = ?",
            (session_id,)).fetchone()
        return bool(row and row[0])
    except Exception as exc:
        log.debug("storage: facts_marked failed (%s)", exc)
        return False


def is_fully_done(session_id: str) -> bool:
    """True only when the proposal was applied with nothing outstanding.

    Deliberately requires the facts step too: a run whose vault writes all landed but
    whose fact marking never ran is not finished, and its facts must stay recallable
    until it is.
    """
    return facts_marked(session_id) and not failed_outputs(session_id)
