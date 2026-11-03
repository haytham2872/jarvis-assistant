"""backend/brain/history_refill.py -- the live history survives a restart.

main.py keeps each channel's last 40 turns in memory only, so every restart (a deploy, a
crash, systemd restarting the service) emptied it. Measured on the server over 30 days
(scripts/measure_memory_baseline.py, 2026-10-11): 46 backend processes, about 1.5 restarts a
day; 38 of 255 Discord turns started with an empty history, and 12 of those came less than
two hours after the previous message -- a restart had cut a conversation that was still
going, and his next message arrived with nothing before it. The first turn after a restart
had no same-session recall either, because the current session id lives in memory too.
Hermes Agent reloads a gateway conversation from SQLite after a restart
(gateway/session_transcript.py); this does the same from the canonical exchange store,
which already holds every turn's exact text (A02).

What is restored: the run of exchanges ending with the newest one on the channel, walking
back while consecutive exchanges are less than the session gap apart, never past the last
/new, at most the history window. By time, not by session id: the shutdown hook writes a
sleep marker, which makes the next message start a new session, so the session id changes
at every restart even in the middle of a conversation. The text is the stored text, the same
bytes the history held, so the provider's prompt cache can still match.

/new clears the history on purpose; mark_new() records when, in a small state file, so a
restart afterwards never brings back the conversation he ended.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from backend.brain import pacing

log = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent.parent / ".jarvis_history.json"


def conversation_to_restore(rows: list[dict], now: float, gap_s: float,
                            new_at: float | None, max_turns: int) -> list[dict]:
    """The exchanges to put back, oldest first. `rows` are the channel's newest exchanges,
    newest first. Stops at the first gap of `gap_s` or more (counting from `now` for the
    newest), at the last /new, at an exchange without full text (an old backfilled row
    keeps only a summary), and after `max_turns`."""
    out: list[dict] = []
    later = now
    for r in rows:
        ts = float(r.get("timestamp") or 0.0)
        if later - ts >= gap_s:
            break
        if new_at is not None and ts <= new_at:
            break
        if not (r.get("user_text") and r.get("assistant_text")):
            break
        out.append(r)
        later = ts
        if len(out) >= max_turns:
            break
    out.reverse()
    return out


def mark_new(channel: str, when: float | None = None) -> None:
    """Record that /new ended the conversation on `channel`. Never raises."""
    stamp = time.time() if when is None else when

    def mutate(state: dict) -> None:
        state.setdefault("new_at", {})[channel] = stamp
    pacing.update_state(_STATE_FILE, mutate, log_prefix="history_refill")


def restore(channel: str, max_turns: int, now: float | None = None) -> list[dict]:
    """The exchanges to refill `channel`'s live history with, oldest first. [] on any
    failure: an empty history is what a restart gave before this existed."""
    try:
        from backend.brain.threads import SESSION_GAP_THRESHOLD
        from backend.storage import exchanges
        rows = exchanges.recent_exchanges(limit=max_turns, channel=channel)
        new_at = pacing.load_state(_STATE_FILE).get("new_at", {}).get(channel)
        return conversation_to_restore(
            rows, time.time() if now is None else now, SESSION_GAP_THRESHOLD,
            float(new_at) if new_at is not None else None, max_turns)
    except Exception as exc:
        log.warning("history_refill: %s history not restored (%s)", channel, exc)
        return []
