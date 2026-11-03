"""Conventions for the life log: what JARVIS records so that, in a couple of months, there
is real data about what was planned, what happened and why.

The tables themselves are owned by one storage module each (`approvals.py`, `pauses.py`,
`plans.py`, ...), each with `CREATE TABLE IF NOT EXISTS` like the existing stores. What they
share is written down here, once, so a row means the same thing in every table:

  * **Facts are appended, never rewritten.** An event table (`block_events`, `checkins`) only
    ever gets new rows; history is never edited to match the present. A "current status"
    column on a parent row is a convenience that can always be rebuilt from the events. A
    *request* table (`approvals`, `pauses`) is one row per request that moves forward through
    its states in place -- each field is set once when that step happens (`decided_at`,
    `executed_at`) and never rewritten afterwards, and nothing is deleted. The structural
    guard that an event table is never UPDATEd or DELETEd from arrives with the first one
    (`block_events`, phase P3).
  * **Every row has a UTC timestamp and a local date.** `ts` is epoch seconds (UTC), and
    `local_date` is the the owner-local calendar day (YYYY-MM-DD). "Per day" and "per week"
    questions then never depend on the server's clock or on time-zone arithmetic done later
    -- the server was on UTC until 30 September 2026, and a clock-change week is 167 or 169
    hours long.
  * **Short text only.** Reasons and replies are the most valuable part of the record, so
    they are kept, but bounded (`short()`); no model transcripts and no full email bodies.
  * **Kept forever and backed up.** The database is copied into every backup
    (`backend/tools/backup.py`). Personal data stays on the server.
  * **Names are plain words**, not codes: actors are `jarvis`, `owner`, `checkin`,
    `system`; states and events are lower-case verbs, so a row reads in a query result.

Deliberately tiny: no ORM, no event bus. A helper here exists only if a table uses it.
"""
from __future__ import annotations
from backend import localtime as _localtime

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = _localtime.ZONE

MAX_TEXT = 400            # a reason, a reply, a preview: enough to understand, never a transcript

ACTORS = ("jarvis", "owner", "checkin", "system")

_WS = re.compile(r"\s+")


def local_date(ts: float | None = None) -> str:
    """The local time calendar day of a timestamp (now by default), as YYYY-MM-DD."""
    moment = time.time() if ts is None else ts
    return datetime.fromtimestamp(moment, tz=LOCAL_TZ).date().isoformat()


def stamp(ts: float | None = None) -> tuple[float, str]:
    """(UTC epoch seconds, local date): the two values every life-log row carries."""
    moment = time.time() if ts is None else float(ts)
    return moment, local_date(moment)


def short(text, limit: int = MAX_TEXT) -> str:
    """One bounded line of free text: whitespace collapsed, cut with an ellipsis."""
    flat = _WS.sub(" ", str(text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[:max(0, limit - 1)].rstrip() + "…"
