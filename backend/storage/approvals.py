"""P1: durable approval requests -- the record behind "ask me first".

Before this, "ask before sending email" was advice in the system prompt. Nothing in the
code enforced it, and a background job, a night task or a scheduled task could send email
with nobody watching. Now an action that reaches another person (or changes the owner's own
data) does not run unless he asked for that exact thing in the current message; otherwise
the gateway stores it here and asks him, on his phone, with one tap.

**One row is one exact action.** The tool and its arguments are stored as they would have
been run, so approving runs *those* arguments once -- not a re-generated version of them.
Approval covers one action; it grants nothing ongoing.

**States move forward, once, by compare-and-set.**

    pending --approve--> executing --> executed | failed | uncertain
       |--deny----------> denied
       `--24 hours-----> expired

`claim()` is the only way to start an execution and it is a single conditional UPDATE, so
two taps on the same button, a text reply racing a tap, or an approval arriving as the row
expires can run the action at most once. `executing` is written *before* the tool runs: if
the process dies mid-send, startup turns it into `uncertain` and tells the owner, because an
email that may or may not have gone out must not be sent again on a guess (the A02 rule).

Life-log conventions (`lifelog.py`): a UTC timestamp and a local date, short text, nothing
deleted. Additive and non-fatal like the other stores: a storage failure returns None and the
caller treats it as "could not record", which the gateway turns into a refusal (an action that
needs approval and cannot be recorded does not run).
"""
from __future__ import annotations

import json
import logging
import time

from backend.storage import db, lifelog

log = logging.getLogger(__name__)

PENDING = "pending"
EXECUTING = "executing"
EXECUTED = "executed"
FAILED = "failed"
UNCERTAIN = "uncertain"
DENIED = "denied"
EXPIRED = "expired"

EXPIRY_S = 24 * 3600.0          # an unanswered request never runs late

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    local_date  TEXT NOT NULL,
    context     TEXT NOT NULL,
    run_id      TEXT,
    tool        TEXT NOT NULL,
    action      TEXT NOT NULL DEFAULT '',
    arguments   TEXT NOT NULL,
    preview     TEXT NOT NULL,
    risk        TEXT NOT NULL,
    why         TEXT NOT NULL,
    state       TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    decision    TEXT,
    decided_at  REAL,
    decided_how TEXT,
    executed_at REAL,
    outcome     TEXT NOT NULL DEFAULT '',
    message_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals (state, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_day ON approvals (local_date);
"""


def _ensure():
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: approvals table unavailable (%s)", exc)
        return None


def _row(r) -> dict:
    d = dict(r)
    try:
        d["arguments"] = json.loads(d["arguments"])
    except (TypeError, ValueError):
        d["arguments"] = {}
    return d


def create(*, context: str, tool: str, arguments: dict, preview: str, risk: str, why: str,
           action: str = "", run_id: str | None = None, ttl_s: float = EXPIRY_S,
           now: float | None = None) -> int | None:
    """Store a request. Returns its id, or None when it could not be recorded."""
    conn = _ensure()
    if conn is None:
        return None
    ts, day = lifelog.stamp(now)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO approvals (created_at, local_date, context, run_id, tool, action, "
                "arguments, preview, risk, why, state, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, day, context, run_id, tool, action,
                 json.dumps(arguments, ensure_ascii=False, default=str),
                 lifelog.short(preview, 600), risk, lifelog.short(why), PENDING, ts + ttl_s))
        return int(cur.lastrowid)
    except Exception as exc:
        log.warning("storage: could not record approval request (%s)", exc)
        return None


def get(approval_id: int) -> dict | None:
    conn = _ensure()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM approvals WHERE id = ?", (int(approval_id),)).fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def claim(approval_id: int, how: str, *, now: float | None = None) -> dict | None:
    """pending -> executing, exactly once. Returns the row to run, or None when it is not
    pending any more (already decided, denied, expired, or unknown)."""
    conn = _ensure()
    if conn is None:
        return None
    ts = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "UPDATE approvals SET state = ?, decision = 'approved', decided_at = ?, "
                "decided_how = ? WHERE id = ? AND state = ? AND expires_at > ?",
                (EXECUTING, ts, how, int(approval_id), PENDING, ts))
        if cur.rowcount != 1:
            return None
        return get(approval_id)
    except Exception as exc:
        log.warning("storage: could not claim approval %s (%s)", approval_id, exc)
        return None


def deny(approval_id: int, how: str, *, now: float | None = None) -> dict | None:
    """pending -> denied. Returns the row, or None when it was not pending."""
    conn = _ensure()
    if conn is None:
        return None
    ts = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "UPDATE approvals SET state = ?, decision = 'denied', decided_at = ?, "
                "decided_how = ? WHERE id = ? AND state = ?",
                (DENIED, ts, how, int(approval_id), PENDING))
        return get(approval_id) if cur.rowcount == 1 else None
    except Exception:
        return None


def finish(approval_id: int, state: str, outcome: str, *, now: float | None = None) -> bool:
    """executing -> executed | failed | uncertain, once."""
    if state not in (EXECUTED, FAILED, UNCERTAIN):
        raise ValueError(f"not a finishing state: {state}")
    conn = _ensure()
    if conn is None:
        return False
    ts = time.time() if now is None else now
    try:
        with conn:
            cur = conn.execute(
                "UPDATE approvals SET state = ?, executed_at = ?, outcome = ? "
                "WHERE id = ? AND state = ?",
                (state, ts, lifelog.short(outcome), int(approval_id), EXECUTING))
        return cur.rowcount == 1
    except Exception:
        return False


def expire_due(*, now: float | None = None) -> list[dict]:
    """pending past its expiry -> expired. Returns the rows expired by THIS call."""
    conn = _ensure()
    if conn is None:
        return []
    ts = time.time() if now is None else now
    expired: list[dict] = []
    try:
        due = conn.execute("SELECT id FROM approvals WHERE state = ? AND expires_at <= ?",
                           (PENDING, ts)).fetchall()
        for r in due:
            with conn:
                cur = conn.execute(
                    "UPDATE approvals SET state = ?, decision = 'expired', decided_at = ?, "
                    "decided_how = 'expiry' WHERE id = ? AND state = ?",
                    (EXPIRED, ts, r["id"], PENDING))
            if cur.rowcount == 1:
                row = get(r["id"])
                if row:
                    expired.append(row)
    except Exception as exc:
        log.debug("storage: approval expiry sweep failed (%s)", exc)
    return expired


def reconcile_interrupted() -> list[dict]:
    """At startup: anything still `executing` was cut off mid-run. Whether the action
    landed cannot be known, so it becomes `uncertain` -- never retried."""
    conn = _ensure()
    if conn is None:
        return []
    ts = time.time()
    rows: list[dict] = []
    try:
        due = conn.execute("SELECT id FROM approvals WHERE state = ?", (EXECUTING,)).fetchall()
        for r in due:
            with conn:
                cur = conn.execute(
                    "UPDATE approvals SET state = ?, executed_at = ?, outcome = ? "
                    "WHERE id = ? AND state = ?",
                    (UNCERTAIN, ts, "interrupted while running; it may or may not have gone through",
                     r["id"], EXECUTING))
            if cur.rowcount == 1:
                row = get(r["id"])
                if row:
                    rows.append(row)
    except Exception as exc:
        log.debug("storage: approval reconcile failed (%s)", exc)
    return rows


def attach_message(approval_id: int, message_id: str) -> None:
    conn = _ensure()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("UPDATE approvals SET message_id = ? WHERE id = ?",
                         (str(message_id), int(approval_id)))
    except Exception:
        pass


def pending(*, now: float | None = None) -> list[dict]:
    """Requests still waiting on the owner, oldest first."""
    conn = _ensure()
    if conn is None:
        return []
    ts = time.time() if now is None else now
    try:
        rows = conn.execute("SELECT * FROM approvals WHERE state = ? AND expires_at > ? "
                            "ORDER BY created_at, id", (PENDING, ts)).fetchall()
        return [_row(r) for r in rows]
    except Exception:
        return []


def recent(limit: int = 20) -> list[dict]:
    conn = _ensure()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
        return [_row(r) for r in rows]
    except Exception:
        return []


def reason_not_pending(approval_id: int) -> str:
    """Plain words for why a tap or a reply did not act on a request."""
    row = get(approval_id)
    if row is None:
        return f"I have no request #{approval_id}."
    state = row["state"]
    if state == EXPIRED or (state == PENDING and row["expires_at"] <= time.time()):
        return f"Request #{approval_id} expired after 24 hours, so it did not run."
    if state == DENIED:
        return f"Request #{approval_id} was already denied."
    if state in (EXECUTING, EXECUTED, FAILED, UNCERTAIN):
        return f"Request #{approval_id} was already approved and handled ({state})."
    return f"Request #{approval_id} is {state}."
