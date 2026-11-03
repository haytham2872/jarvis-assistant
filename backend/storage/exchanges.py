"""A02: canonical exchange and session records.

The roadmap's finding this addresses: *"Full exchange preservation depends on indexing --
low-significance thread records retain a heuristic summary; full semantic exchange chunks
are written asynchronously. Index failure can lose the reconstructable original
exchange."*

Concretely, `threads.Thread` stores `user`/`assistant` **only when the exchange scored
high significance**; otherwise it keeps a ~150-char heuristic summary and the real wording
survives only in `jarvis_semantic`, written fire-and-forget. Two independent async writes
had to succeed for an ordinary exchange to remain reconstructable.

Here the full text is stored **always**, synchronously, regardless of significance.
Significance is kept as a ranking signal, never a storage gate.

Every function degrades to a falsey result rather than raising: this store is additive
and must never be able to fail a turn that would otherwise have worked.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from backend.storage import db

log = logging.getLogger(__name__)


def record_exchange(
    *,
    exchange_id: str,
    session_id: str,
    channel: str,
    user_text: str,
    assistant_text: str,
    significance: float,
    high: bool,
    summary: str = "",
    timestamp: float | None = None,
) -> bool:
    """Persist one completed exchange. Idempotent on `exchange_id`.

    Idempotency matters because the caller mirrors an existing Qdrant write: a retry, or
    a future resumed derivation, must not create a duplicate. `INSERT OR REPLACE` also
    makes re-running a backfill safe.
    """
    conn = db.connect()
    if conn is None:
        return False
    try:
        with conn:                      # one short transaction; commits or rolls back
            conn.execute(
                """INSERT OR REPLACE INTO exchanges
                   (id, session_id, channel, timestamp, user_text, assistant_text,
                    significance, high, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (exchange_id, session_id, channel, timestamp or time.time(),
                 user_text, assistant_text, float(significance), int(bool(high)), summary),
            )
            conn.execute(
                """INSERT INTO sessions (session_id, channel, started_at, last_activity_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       last_activity_at = excluded.last_activity_at""",
                (session_id, channel, timestamp or time.time(), timestamp or time.time()),
            )
        return True
    except Exception as exc:
        log.warning("storage: exchange %s not recorded (%s)", exchange_id, exc)
        return False


def backfill_missing(rows) -> int:
    """Insert exchanges that are not already stored. Returns how many were added.

    A02's deferred read-path swap needs the canonical store to actually hold the
    history before it can be consulted first: the mirror only began filling when
    `record_thread` started writing it, so on an existing install SQLite is empty while
    Qdrant holds every past exchange.

    **Insert-if-absent, never replace.** `record_exchange` uses `INSERT OR REPLACE`,
    which is right for a live write but wrong here: a row backfilled from Qdrant is
    strictly *poorer* than a live-recorded one (a low-significance Qdrant thread keeps
    only a ~150-char summary, never the full text), so replacing would destroy the
    better copy. Re-running this is therefore safe and idempotent.

    Each row is `{id, session_id, channel, timestamp, user, assistant, significance,
    high, summary}` as `threads` yields it.
    """
    conn = db.connect()
    if conn is None:
        return 0
    added = 0
    try:
        with conn:
            for row in rows or []:
                exchange_id = str(row.get("id") or "").strip()
                if not exchange_id:
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO exchanges
                       (id, session_id, channel, timestamp, user_text, assistant_text,
                        significance, high, summary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (exchange_id,
                     row.get("session_id") or "",
                     row.get("channel") or "local",
                     float(row.get("timestamp") or 0.0),
                     row.get("user") or "",
                     row.get("assistant") or "",
                     float(row.get("significance") or 0.0),
                     int(bool(row.get("high"))),
                     row.get("summary") or ""),
                )
                added += cur.rowcount or 0
                conn.execute(
                    """INSERT INTO sessions (session_id, channel, started_at,
                                             last_activity_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                           last_activity_at = MAX(last_activity_at, excluded.last_activity_at),
                           started_at       = MIN(started_at, excluded.started_at)""",
                    (row.get("session_id") or "", row.get("channel") or "local",
                     float(row.get("timestamp") or 0.0), float(row.get("timestamp") or 0.0)),
                )
    except Exception as exc:
        log.warning("storage: exchange backfill stopped early (%s)", exc)
    if added:
        log.info("storage: backfilled %d exchange(s) from the thread history", added)
    return added


def get_exchange(exchange_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def exchanges_for_session(session_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Chronological full-text exchanges for one session."""
    conn = db.connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM exchanges WHERE session_id = ? ORDER BY timestamp LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def exchanges_in_range(start_ts: float, end_ts: float,
                       limit: int = 5000) -> list[dict[str, Any]]:
    """Chronological exchanges with start_ts <= timestamp < end_ts.

    Served by `idx_exchanges_channel_ts`; the Qdrant equivalent fetches every record and
    filters in process, measured at 4,671ms against 3.7ms here.
    """
    conn = db.connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM exchanges WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp LIMIT ?",
            (float(start_ts), float(end_ts), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def recent_exchanges(limit: int = 10, channel: str | None = None) -> list[dict[str, Any]]:
    """Newest-first exchanges, optionally scoped to one channel.

    Served by `idx_exchanges_channel_ts`, i.e. an index seek. The Qdrant equivalent
    (`threads._scroll_recent_sync`) fetches every record and sorts in process, which is
    the roadmap's "recent-thread queries fetch/sort all records" measurement finding.
    """
    conn = db.connect()
    if conn is None:
        return []
    try:
        if channel is None:
            rows = conn.execute(
                "SELECT * FROM exchanges ORDER BY timestamp DESC LIMIT ?", (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM exchanges WHERE channel = ? ORDER BY timestamp DESC LIMIT ?",
                (channel, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def sessions_for_channel(channel: str, limit: int = 30) -> list[dict[str, Any]]:
    """One row per session on `channel`, newest activity first: when it started and last
    moved, how many exchanges, and its first message (for a chat list)."""
    conn = db.connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT session_id, MIN(timestamp) AS started_at, MAX(timestamp) AS last_at,"
            " COUNT(*) AS exchanges,"
            " (SELECT user_text FROM exchanges e2 WHERE e2.session_id = e.session_id"
            "  ORDER BY timestamp LIMIT 1) AS first_user_text"
            " FROM exchanges e WHERE channel = ? GROUP BY session_id"
            " ORDER BY last_at DESC LIMIT ?", (channel, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def last_activity(channel: str) -> float | None:
    """Timestamp of the newest exchange on a channel, or None.

    One indexed lookup. This is what a future session-boundary check can use instead of
    scanning history -- not wired into `assign_session` yet; that swap belongs to the
    slice that makes SQLite authoritative.
    """
    conn = db.connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT timestamp FROM exchanges WHERE channel = ? "
            "ORDER BY timestamp DESC LIMIT 1", (channel,),
        ).fetchone()
        return float(row["timestamp"]) if row else None
    except Exception:
        return None


def quiet_open_sessions(quiet_before: float, active_after: float,
                        exclude_channels: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Sessions not yet ended whose last exchange falls in [active_after, quiet_before),
    oldest first: the ones a quiet-session close should consolidate."""
    conn = db.connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT session_id, channel, last_activity_at FROM sessions "
            "WHERE ended_at IS NULL AND last_activity_at < ? AND last_activity_at >= ? "
            "ORDER BY last_activity_at", (quiet_before, active_after),
        ).fetchall()
        return [dict(r) for r in rows if r["channel"] not in exclude_channels]
    except Exception:
        return []


def end_session(session_id: str, when: float | None = None) -> bool:
    conn = db.connect()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE session_id = ?",
                         (when or time.time(), session_id))
        return True
    except Exception:
        return False


def claim_ingress(message_id: str, channel: str, session_id: str | None = None) -> bool:
    """True when this message is new; False when it has already been admitted.

    The roadmap's ingress rule: "Deduplicate incoming Discord message IDs" so a gateway
    redelivery cannot run the same request twice. Uses the PRIMARY KEY conflict rather
    than SELECT-then-INSERT, so two concurrent admissions of the same id cannot both
    win -- a check-then-act would race.
    """
    conn = db.connect()
    if conn is None:
        return True        # cannot dedup -> admit, never silently drop a real message
    try:
        with conn:
            conn.execute(
                "INSERT INTO ingress (message_id, channel, received_at, session_id) "
                "VALUES (?, ?, ?, ?)",
                (message_id, channel, time.time(), session_id),
            )
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as exc:
        log.warning("storage: ingress claim failed for %s (%s)", message_id, exc)
        return True


def counts() -> dict[str, int]:
    """Row counts, for status reporting and the mirroring health check."""
    conn = db.connect()
    if conn is None:
        return {}
    out: dict[str, int] = {}
    for table in ("exchanges", "sessions", "ingress"):
        try:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:
            pass
    return out
