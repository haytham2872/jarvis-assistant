"""P4: the daily loop that keeps the plan true.

Once a minute, four things -- each cheap when there is nothing to do:

  * **Close finished days.** A check-in whose day is over (04:00 the next morning) is finished off:
    blocks he never answered for are recorded as unknown. Bookkeeping, so never held by /pause.
  * **The evening check-in** (default 21:30, `/plan checkin HH:MM`): one message listing the day's
    blocks, sent once a day, only when there is a plan with blocks that day. A reminder-class
    thing he asked for, so only `/pause all` holds it.
  * **The 30-minute look** at his calendar between 07:00 and 23:00 (read-only) for a meeting that
    now overlaps one of the plan's blocks; each clash gets a proposed move (`reshuffle.py`). Work the
    plan does on its own, so a plain `/pause` holds it.
  * **The weekly review** on Sunday evening (default 19:00): what was planned against what happened,
    and next week drawn from the same rules (`weeklyreview.py`). Held by `/pause` too.

The Google token is never created from here: with no token file the calendar steps are skipped, so
an unattended loop can never open an interactive consent flow.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from backend.brain import notices as _notices
from backend.brain import pacing, pause, planday, reshuffle, weeklyreview
from backend.brain import planner as P
from backend.storage import checkins, plans

log = logging.getLogger(__name__)

_CHECK_INTERVAL_S = 60.0
_SCAN_EVERY_S = 30 * 60
_SCAN_FROM_HOUR = 7
_SCAN_TO_HOUR = 23

_notifier: Callable[[str], Awaitable[bool]] | None = None
_unsent_review: dict = {}                 # {"week": iso, "text": str}: composed, Discord did not take it


def set_notifier(fn) -> None:
    """async fn(text) -> bool: how a check-in, a review or a notice reaches him."""
    global _notifier
    _notifier = fn


def set_busy_check(fn) -> None:
    """Accepted for the shared wiring (bootstrap.wire_busy_check); the loop's steps are DMs and
    calendar reads, which do not contend with a conversation."""


def _calendar_ready() -> bool:
    """Is there already a Google token? Without one a calendar call would open a browser."""
    try:
        from backend.tools.calendar import TOKEN_PATH
        return TOKEN_PATH.exists()
    except Exception:
        return False


async def _notify(text: str) -> bool:
    if _notifier is None:
        return False
    try:
        return bool(await _notifier(text))
    except Exception as exc:
        log.debug("planloop: message not delivered (%s)", exc)
        return False


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

async def _close_days(now: datetime) -> None:
    today = P.local(now).date().isoformat()
    for row in checkins.unclosed(before_date=today):
        if planday.closable(row, now):
            n = planday.close_day(row, now)
            log.info("planloop: closed the check-in of %s (%d block(s) unanswered -> unknown)",
                     row["local_date"], n)


def _note_held(key: str, value: str, kind: str, label: str) -> None:
    """Tell /resume about something a pause held back, once."""
    if plans.get_setting(key) != value:
        plans.set_setting(key, value)
        pause.record_skip(kind, label)


async def _maybe_checkin(now: datetime) -> None:
    enabled, when = planday.checkin_config()
    if not enabled:
        return
    local = P.local(now)
    day_iso = local.date().isoformat()
    if f"{local:%H:%M}" < when:
        return
    row = checkins.get(day_iso)
    if row and row["sent_at"]:
        return
    payload = planday.checkin_payload(local.date())
    if payload is None:
        return
    text, uids = payload
    if pause.is_paused("all"):
        _note_held("checkin_held_for", day_iso, "checkin_held", f"evening check-in of {day_iso}")
        return
    if row is None and not checkins.claim(day_iso, uids, now=now.timestamp()):
        return
    with _notices.labelled("checkin"):
        delivered = await _notify(text)
    if delivered:
        checkins.mark_sent(day_iso, now=now.timestamp())
        log.info("planloop: evening check-in sent for %s", day_iso)
    else:
        log.debug("planloop: evening check-in for %s not delivered; will retry", day_iso)


async def _maybe_scan(now: datetime) -> None:
    local = P.local(now)
    if not (_SCAN_FROM_HOUR <= local.hour < _SCAN_TO_HOUR):
        return
    last = plans.get_setting("last_scan_at", 0.0)
    if now.timestamp() - float(last or 0.0) < _SCAN_EVERY_S:
        return
    if not _calendar_ready():
        return
    plans.set_setting("last_scan_at", now.timestamp())          # first, so a failing look waits a full interval
    loop = asyncio.get_event_loop()
    try:
        moves = await loop.run_in_executor(None, reshuffle.scan, now)
    except Exception as exc:
        log.warning("planloop: calendar look failed (%s)", exc)
        return
    if moves:
        said = await reshuffle.handle(moves, now, _notify)
        log.info("planloop: %d clash(es) found, %d message(s) sent", len(moves), len(said))


async def _maybe_review(now: datetime) -> None:
    enabled, when = planday.review_config()
    if not enabled:
        return
    local = P.local(now)
    if local.weekday() != 6 or f"{local:%H:%M}" < when:
        return
    week_iso = P.monday_of(local.date()).isoformat()
    if plans.get_setting("last_review_week") == week_iso:
        return
    if _unsent_review.get("week") == week_iso:                   # composed already; only the send failed
        with _notices.labelled("review"):
            delivered = await _notify(_unsent_review["text"])
        if delivered:
            plans.set_setting("last_review_week", week_iso)
            _unsent_review.clear()
        return
    loop = asyncio.get_event_loop()
    ready = _calendar_ready()
    try:
        text = await loop.run_in_executor(None, weeklyreview.compose, now, ready)
    except Exception as exc:
        log.warning("planloop: weekly review failed (%s)", exc)
        plans.set_setting("last_review_week", week_iso)          # one attempt a week
        return
    if text is None:                                             # nothing was planned this week
        plans.set_setting("last_review_week", week_iso)
        return
    with _notices.labelled("review"):
        delivered = await _notify(text)
    if delivered:
        plans.set_setting("last_review_week", week_iso)
        log.info("planloop: weekly review sent for %s", week_iso)
    else:
        _unsent_review.update({"week": week_iso, "text": text})


async def _step(name: str, fn, now: datetime) -> None:
    try:
        await fn(now)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("planloop: %s failed (%s)", name, exc)


async def _cycle(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    await _step("closing days", _close_days, now)               # bookkeeping: never held
    await _step("check-in", _maybe_checkin, now)                # held by /pause all (it says so, once)
    if not pause.is_paused("work"):
        await _step("calendar look", _maybe_scan, now)
        await _step("weekly review", _maybe_review, now)
    else:
        _note_review_held(now)


def _note_review_held(now: datetime) -> None:
    enabled, when = planday.review_config()
    local = P.local(now)
    if enabled and local.weekday() == 6 and f"{local:%H:%M}" >= when:
        week_iso = P.monday_of(local.date()).isoformat()
        if plans.get_setting("last_review_week") != week_iso:
            _note_held("review_held_for", week_iso, "review_held", f"weekly review of {week_iso}")


async def run_loop() -> None:
    """Started from main.py's lifespan."""
    enabled, when = planday.checkin_config()
    log.info("planloop: check-in %s, weekly review Sundays", "at " + when if enabled else "off")
    await pacing.run_periodic(name="planloop", check_interval_s=_CHECK_INTERVAL_S, on_cycle=_cycle)
