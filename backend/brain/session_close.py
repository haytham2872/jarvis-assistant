"""backend/brain/session_close.py — close a quiet session instead of waiting for the next message.

A session ends after `SESSION_GAP_THRESHOLD` (2h) of silence on its channel, but nothing
used to act at that moment: the old session was consolidated only when the next message
arrived (`chat()` sees `ended_session_id`), on `/new`, or at a graceful shutdown. On the
always-on server, where restarts are rare, a conversation at 10pm was consolidated at
9am the next day, and a quiet week left its letter, portrait update and profile edits
waiting a week.

This loop closes a session once it has been quiet for the gap, so all of that lands about
two hours after the last message. Nothing else changes: the next message still starts a
new session by the same rule, and `consolidate_session` already skips a session it has
finished, so that turn does not redo it.

Sessions come from SQLite's `sessions` table (every exchange updates it), so a restart
does not lose track of an open one. Only sessions active in the last `_LOOKBACK_S` are
considered, one per cycle, and each is marked ended after one attempt: a years-old
backlog, or a session whose consolidation keeps failing, must never turn into a burst of
LLM calls. The next-message path stays as the retry for a failure.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_CHECK_S = 300.0                 # how often to look
_LOOKBACK_S = 48 * 3600.0        # older open sessions are left to the next-message path
_EXCLUDE = ("canary",)           # the memory canary's own channel is not a conversation

_busy_check: Optional[Callable[[], bool]] = None


def set_busy_check(fn: Callable[[], bool]) -> None:
    global _busy_check
    _busy_check = fn


def due(now: float | None = None) -> list[dict]:
    """Open sessions that have been quiet for the gap, oldest first. Never raises."""
    from backend.brain.threads import SESSION_GAP_THRESHOLD
    from backend.storage import exchanges
    now = time.time() if now is None else now
    try:
        return exchanges.quiet_open_sessions(now - SESSION_GAP_THRESHOLD, now - _LOOKBACK_S,
                                             _EXCLUDE)
    except Exception:
        return []


async def close_one(now: float | None = None) -> str | None:
    """Consolidate and end the oldest quiet session. Returns its id, or None."""
    if _busy_check is not None and _busy_check():
        return None          # a live turn owns the pipeline; the next cycle will do it
    sessions = due(now)
    if not sessions:
        return None
    sid = sessions[0]["session_id"]
    from backend.brain.consolidation import consolidate_session
    from backend.storage import exchanges
    try:
        log.info("session_close: %s quiet since %s on %s -- consolidating", sid[:8],
                 time.strftime("%H:%M", time.localtime(sessions[0]["last_activity_at"])),
                 sessions[0]["channel"])
        await consolidate_session(sid)
    finally:
        exchanges.end_session(sid)       # one attempt, success or not -- see module doc
    return sid


async def run_loop() -> None:
    from backend.brain import pacing
    await pacing.run_periodic(name="session_close", check_interval_s=_CHECK_S,
                              on_cycle=close_one)
