"""A02 operation receipts and the tables used by durable jobs and delivery.

**Receipts.** The roadmap's contract: *"Record operation intent before a tool side effect
and its observed result afterward"*, so that *"Jarvis can explain what a job did, what it
saved, what remains uncertain"*. Before this, a tool call left no trace once the turn
ended — `_append_history` keeps only the flat final (user, assistant) pair, and the
tool-call transcript lives in one function's local list and is discarded. A receipt is
the durable answer to "did that write actually happen, and what did it return".

Outcomes are typed rather than guessed from prose: `ok | failed | unknown`. **`unknown`
is the important one** — an interrupted or timed-out external write (an email send, a
calendar insert) may or may not have taken effect remotely, and the roadmap is explicit
that it must be recorded as uncertain rather than blindly retried.

**Jobs and outbox** were originally schema only. Background jobs now use
`backend.storage.jobs`; Discord reply receipts use `backend.storage.delivery`.
Chronos schedules and unkeyed notifications still use their existing paths.

Additive and non-fatal throughout: a receipt that cannot be written must never fail the
tool call it describes.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from backend.storage import db

log = logging.getLogger(__name__)

OK = "ok"
FAILED = "failed"
UNKNOWN = "unknown"

_PREVIEW_CHARS = 500        # bounded evidence, not an artifact store

# The run a tool call belongs to. A ContextVar because the run id is established in
# main.py's turn body while tools execute deep inside engine.py, and asyncio.gather runs
# them concurrently -- a module global would be shared across overlapping work, while a
# ContextVar is inherited correctly by each task spawned from the turn.
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    run_id       TEXT,
    tool         TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    started_at   REAL NOT NULL,
    duration_ms  INTEGER,
    args_preview TEXT NOT NULL DEFAULT '',
    result_preview TEXT NOT NULL DEFAULT '',
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_run ON operations (run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_operations_outcome ON operations (outcome, started_at DESC);

-- A06 background jobs use this table through storage/jobs.py.
CREATE TABLE IF NOT EXISTS derivation_jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON derivation_jobs (state, created_at);

-- A07 Discord replies use this table through storage/delivery.py.
CREATE TABLE IF NOT EXISTS delivery_intents (
    intent_id   TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    state       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    sent_at     REAL,
    receipt     TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_delivery_state ON delivery_intents (state, created_at);
"""


def _ensure():
    """Connection with the A02 slice-3 tables present.

    Created here rather than appended to `db._MIGRATIONS` for the same reason as `runs`:
    schema v1 has shipped, and editing a released migration in place would let a fresh
    database and an upgraded one disagree.
    """
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: operations tables unavailable (%s)", exc)
        return None


def _preview(value) -> str:
    try:
        return str(value)[:_PREVIEW_CHARS]
    except Exception:
        return "<unrepresentable>"


def record_operation(
    *,
    tool: str,
    outcome: str,
    started_at: float,
    args=None,
    result=None,
    error: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Write one operation receipt. Returns its id, or None when unavailable.

    Called after the tool has run, with `started_at` captured before it, so the duration
    is real. Never raises — a receipt must not be able to fail the operation it records.
    """
    conn = _ensure()
    if conn is None:
        return None
    operation_id = str(uuid.uuid4())
    try:
        with conn:
            conn.execute(
                "INSERT INTO operations (operation_id, run_id, tool, outcome, started_at,"
                " duration_ms, args_preview, result_preview, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (operation_id, run_id if run_id is not None else current_run_id.get(),
                 tool, outcome, started_at,
                 int(max(0.0, time.time() - started_at) * 1000),
                 _preview(args) if args is not None else "",
                 _preview(result) if result is not None else "",
                 error),
            )
        return operation_id
    except Exception as exc:
        log.debug("storage: operation receipt for %s not written (%s)", tool, exc)
        return None


def operations_for_run(run_id: str) -> list[dict]:
    conn = _ensure()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM operations WHERE run_id = ? ORDER BY started_at", (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def uncertain_operations(limit: int = 50) -> list[dict]:
    """Operations whose remote effect could not be established.

    These are the ones a human (or a later reconciliation pass) must look at: the
    roadmap forbids resolving them by blind retry.
    """
    conn = _ensure()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM operations WHERE outcome = ? ORDER BY started_at DESC LIMIT ?",
            (UNKNOWN, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def counts() -> dict[str, int]:
    conn = _ensure()
    if conn is None:
        return {}
    out: dict[str, int] = {}
    for table in ("operations", "derivation_jobs", "delivery_intents"):
        try:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:
            pass
    return out


def outcome_counts(since: float) -> dict[str, int]:
    """Tool operations started at or after `since`, by outcome (ok / failed / unknown).

    Read-only, for /activity: what was done in the last day and how it went.
    """
    conn = _ensure()
    if conn is None:
        return {}
    try:
        return {row["outcome"]: int(row["n"]) for row in conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM operations WHERE started_at >= ? GROUP BY outcome",
            (float(since),)).fetchall()}
    except Exception:
        return {}
