"""Durable admission and recovery for detached background tasks.

Queued jobs are safe to start after a restart. A job that reached ``running`` may
already have performed external actions, so recovery marks it interrupted and
never executes it again automatically.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from backend.storage import operations

log = logging.getLogger(__name__)


def _conn():
    return operations._ensure()


def enqueue(payload: dict) -> str | None:
    conn = _conn()
    if conn is None:
        return None
    job_id = str(uuid.uuid4())
    now = time.time()
    try:
        with conn:
            conn.execute(
                "INSERT INTO derivation_jobs"
                " (job_id, kind, state, payload, created_at, updated_at)"
                " VALUES (?, 'background', 'queued', ?, ?, ?)",
                (job_id, json.dumps(payload), now, now),
            )
        return job_id
    except Exception as exc:
        log.warning("jobs: could not queue task (%s)", exc)
        return None


def claim(job_id: str) -> bool | None:
    """Atomically claim one queued job. None means storage became unavailable."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn:
            cur = conn.execute(
                "UPDATE derivation_jobs SET state='running', attempts=attempts+1,"
                " updated_at=? WHERE job_id=? AND state='queued'",
                (time.time(), job_id),
            )
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("jobs: claim failed for %s (%s)", job_id, exc)
        return None


def finish(job_id: str | None, state: str, payload: dict, error: str = "") -> bool:
    if not job_id:
        return False
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE derivation_jobs SET state=?, payload=?, error=?, updated_at=?"
                " WHERE job_id=? AND state='running'",
                (state, json.dumps(payload), error[:500] or None, time.time(), job_id),
            )
        return cur.rowcount == 1
    except Exception as exc:
        log.warning("jobs: finish failed for %s (%s)", job_id, exc)
        return False


def mark_announced(job_id: str | None) -> None:
    if not job_id:
        return
    conn = _conn()
    if conn is None:
        return
    try:
        with conn:
            row = conn.execute(
                "SELECT payload FROM derivation_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload"])
            payload["announced"] = True
            conn.execute(
                "UPDATE derivation_jobs SET payload=?, updated_at=? WHERE job_id=?",
                (json.dumps(payload), time.time(), job_id),
            )
    except Exception as exc:
        log.debug("jobs: announce mark failed for %s (%s)", job_id, exc)


def recover() -> list[dict]:
    """Mark old running jobs interrupted; never hide queued work behind a limit."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn:
            conn.execute(
                "UPDATE derivation_jobs SET state='interrupted', updated_at=?,"
                " error='Process stopped during execution; side effects may have landed'"
                " WHERE kind='background' AND state='running'",
                (time.time(),),
            )
        rows = conn.execute(
            "SELECT job_id, state, payload, created_at, updated_at FROM derivation_jobs"
            " WHERE kind='background' AND state IN ('queued', 'interrupted')"
            " UNION ALL SELECT job_id, state, payload, created_at, updated_at"
            " FROM (SELECT job_id, state, payload, created_at, updated_at"
            " FROM derivation_jobs WHERE kind='background'"
            " AND state NOT IN ('queued', 'interrupted')"
            " ORDER BY created_at DESC LIMIT 100)"
            " ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            try:
                out.append({**dict(row), "payload": json.loads(row["payload"])})
            except (ValueError, TypeError):
                log.warning("jobs: invalid payload for %s; leaving it for inspection", row["job_id"])
        return out
    except Exception as exc:
        log.warning("jobs: recovery failed (%s)", exc)
        return []


def exists(job_id: str | None) -> bool:
    if not job_id:
        return False
    conn = _conn()
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM derivation_jobs WHERE job_id=?", (job_id,),
        ).fetchone() is not None
    except Exception:
        return False


def set_payload_field(job_id: str | None, key: str, value) -> bool:
    """G2: write one field of a job's durable payload now (a goal child's remote run id)."""
    if not job_id:
        return False
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn:
            row = conn.execute("SELECT payload FROM derivation_jobs WHERE job_id=?",
                               (job_id,)).fetchone()
            if row is None:
                return False
            payload = json.loads(row["payload"])
            payload[key] = value
            conn.execute("UPDATE derivation_jobs SET payload=?, updated_at=? WHERE job_id=?",
                         (json.dumps(payload), time.time(), job_id))
        return True
    except Exception as exc:
        log.debug("jobs: payload field %s not written for %s (%s)", key, job_id, exc)
        return False


def research_launched_today(local_date: str) -> int:
    """web_research jobs created on this local day (TinyFish allows five)."""
    conn = _conn()
    if conn is None:
        return 0
    from backend.storage import lifelog
    n = 0
    for row in conn.execute("SELECT payload, created_at FROM derivation_jobs WHERE kind='background'"
                            " AND created_at > ?", (time.time() - 2 * 86400,)).fetchall():
        try:
            p = json.loads(row["payload"])
        except ValueError:
            continue
        # A goal's research job is counted by its goal effect (counted before it is sent).
        if p.get("kind") == "web_research" and not p.get("goal_id") \
                and lifelog.local_date(row["created_at"]) == local_date:
            n += 1
    return n


def has_occurrence(key: str | None) -> bool:
    """G0 / N1: whether a scheduled run with this chronos occurrence key was already
    queued. False when unknown: chronos then tries to start it, and a second copy needs
    a crash in the instant between two local writes."""
    if not key:
        return False
    conn = _conn()
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM derivation_jobs WHERE kind='background'"
            " AND json_extract(payload, '$.occurrence') = ? LIMIT 1", (key,),
        ).fetchone() is not None
    except Exception as exc:
        log.debug("jobs: occurrence lookup failed (%s)", exc)
        return False


def get(job_id: str) -> dict | None:
    """Read one durable job without the recovery path's state transitions."""
    conn = _conn()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM derivation_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def finished_since(since: float) -> list[dict]:
    """Completed durable work, including jobs started before the requested window.

    Read-only, with no recovery transitions. Failure raises rather than pretending quiet.
    """
    try:
        conn = _conn()
        if conn is None:
            raise RuntimeError("Job storage unavailable")
        rows = conn.execute(
            "SELECT job_id, state, payload, created_at, updated_at FROM derivation_jobs"
            " WHERE kind='background' AND state NOT IN ('queued','running')"
            " AND COALESCE(json_extract(payload, '$.finished'), updated_at)>=?",
            (since,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
    except Exception as exc:
        log.warning("jobs: completed jobs could not be read (%s)", exc)
        raise


def recent(since: float, limit: int = 50) -> list[dict]:
    """Background jobs created since `since`, newest first, with their payload decoded.
    Read-only; the phone app lists the ones it started from these."""
    conn = _conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT job_id, state, payload, created_at, updated_at FROM derivation_jobs"
            " WHERE kind='background' AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, int(limit))).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append({"job_id": r["job_id"], "state": r["state"], "created_at": r["created_at"],
                    "updated_at": r["updated_at"], "payload": payload})
    return out
