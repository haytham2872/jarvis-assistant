"""A02: durable run records, so an interrupted turn is recoverable rather than invisible.

The roadmap's shutdown/restart requirement: *"At shutdown ... record interrupted/unknown
states ... On restart, reconcile interrupted operations before resuming safe work"*, and
its acceptance *"shutdown records recoverable interrupted work"*.

Before this, a turn existed only as an in-flight coroutine. If the process died
mid-turn — a crash, a `taskkill` that outran the graceful path, a host reboot — there was
no record that the turn had ever been admitted. Any tool side effects it had already
performed stayed done, with nothing pointing at them.

A run is opened when a turn starts and closed when it finishes. Anything still open at
startup was, by definition, killed mid-flight: `reconcile_interrupted()` marks those
`interrupted` so the fact is durable and inspectable, rather than being silently rolled
forward or retried. **Nothing is automatically re-run** — the roadmap is explicit that an
uncertain side effect must not be blindly repeated.

Additive and non-fatal throughout: every function swallows its errors, and a failure here
can never stop a turn.
"""
from __future__ import annotations

import logging
import time
import uuid

from backend.storage import db

log = logging.getLogger(__name__)

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"

_PREVIEW_CHARS = 200        # enough to recognise a run; not a transcript store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    session_id   TEXT,
    channel      TEXT NOT NULL,
    state        TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    user_preview TEXT NOT NULL DEFAULT '',
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs (state, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_channel ON runs (channel, started_at DESC);
"""


def _ensure() -> object | None:
    """Connection with the runs table present.

    The table is created here rather than in db.py's migration list because this slice
    landed after schema v1 shipped, and editing a shipped migration in place would let a
    fresh database and an upgraded one disagree. `CREATE TABLE IF NOT EXISTS` is
    idempotent and costs one cheap statement on the first call per connection.
    """
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: runs table unavailable (%s)", exc)
        return None


def begin_run(channel: str, user_text: str, session_id: str | None = None,
              *, run_id: str | None = None) -> str | None:
    """Open a run. Returns its id, or None when storage is unavailable."""
    conn = _ensure()
    if conn is None:
        return None
    run_id = run_id or str(uuid.uuid4())
    try:
        with conn:
            conn.execute(
                "INSERT INTO runs (run_id, session_id, channel, state, started_at, "
                "user_preview) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, session_id, channel, RUNNING, time.time(),
                 (user_text or "")[:_PREVIEW_CHARS]),
            )
        return run_id
    except Exception as exc:
        log.debug("storage: begin_run failed (%s)", exc)
        return None


def finish_run(run_id: str | None, state: str = COMPLETED,
               session_id: str | None = None, error: str | None = None) -> bool:
    """Close a run. A no-op for a None id, so callers need no branch."""
    if not run_id:
        return False
    conn = _ensure()
    if conn is None:
        return False
    try:
        with conn:
            if session_id:
                conn.execute(
                    "UPDATE runs SET state = ?, ended_at = ?, error = ?, session_id = ? "
                    "WHERE run_id = ?",
                    (state, time.time(), error, session_id, run_id))
            else:
                conn.execute(
                    "UPDATE runs SET state = ?, ended_at = ?, error = ? WHERE run_id = ?",
                    (state, time.time(), error, run_id))
        return True
    except Exception as exc:
        log.debug("storage: finish_run failed (%s)", exc)
        return False


def open_runs(channels=None) -> list[dict]:
    """Runs still marked `running`, optionally limited to certain channels.

    A06: called at startup *before* `reconcile_interrupted()`, so the caller can see
    exactly which work the previous process was in the middle of. Anything still
    `running` at startup was cut off — the process that owned it is gone.
    """
    conn = _ensure()
    if conn is None:
        return []
    try:
        if channels:
            marks = ",".join("?" for _ in channels)
            rows = conn.execute(
                f"SELECT run_id, channel, user_preview, started_at FROM runs "
                f"WHERE state = ? AND channel IN ({marks}) ORDER BY started_at",
                (RUNNING, *channels)).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, channel, user_preview, started_at FROM runs "
                "WHERE state = ? ORDER BY started_at", (RUNNING,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("storage: open_runs failed (%s)", exc)
        return []


def reconcile_interrupted() -> int:
    """Mark still-open runs as interrupted. Call once at startup, before admitting work.

    Anything left `running` when the process starts cannot be running — the process that
    owned it is gone. Marking rather than deleting keeps the fact that a turn was cut off,
    which is what makes its side effects investigable. Deliberately does NOT retry
    anything: the roadmap requires an uncertain remote action to stay uncertain rather
    than be blindly repeated.
    """
    conn = _ensure()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE runs SET state = ?, ended_at = COALESCE(ended_at, ?) "
                "WHERE state = ?", (INTERRUPTED, time.time(), RUNNING))
            count = cur.rowcount or 0
        if count:
            log.warning(
                "storage: %d run(s) were interrupted by a previous shutdown -- marked "
                "for inspection, not retried", count)
        return count
    except Exception as exc:
        log.debug("storage: reconcile_interrupted failed (%s)", exc)
        return 0


def interrupted_runs(limit: int = 20) -> list[dict]:
    """Recent interrupted runs, newest first -- for status reporting."""
    conn = _ensure()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM runs WHERE state = ? ORDER BY started_at DESC LIMIT ?",
            (INTERRUPTED, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_run(run_id: str) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def counts_since(since: float, channels=None) -> dict[str, int]:
    """Runs started at or after `since`, by state (completed / failed / interrupted /
    running). Read-only, for /activity."""
    conn = _ensure()
    if conn is None:
        return {}
    try:
        sql = "SELECT state, COUNT(*) AS n FROM runs WHERE started_at >= ?"
        args: list = [float(since)]
        if channels:
            sql += " AND channel IN (" + ",".join("?" for _ in channels) + ")"
            args += list(channels)
        return {row["state"]: int(row["n"])
                for row in conn.execute(sql + " GROUP BY state", args).fetchall()}
    except Exception:
        return {}
