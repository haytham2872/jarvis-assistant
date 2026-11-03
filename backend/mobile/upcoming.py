"""Phone slice 4: what the phone should notify about by itself, and when.

Expo Go cannot be relied on to receive a server's push (Expo rules it out on Android and says
nothing either way on iOS), but it can schedule notifications itself. Many of JARVIS's own
messages have a known time: reminders, the morning brief, scheduled checks, the evening
check-in, the Sunday review. The app reads this list when it opens and schedules them; each
notification opens the screen where the message will be (the Inbox, or Today for the check-in).

Read from the stores that own each thing (chronos's schedule, the plan's settings and blocks),
never from a copy. What a pause holds is left out: a `/pause` skips scheduled checks and the
review, `/pause all` also holds reminders, the brief and the check-in (pause.py's split).

A reminder's notification carries its own words. Anything composed when it is sent (the
brief, a check's answer, the review) gets a line saying it is ready, a few minutes after it is
due, so the message is in the Inbox by the time he taps it.
"""
from __future__ import annotations
from backend import localtime as _localtime

import datetime as dt
import logging
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LOCAL_TZ = _localtime.ZONE
HORIZON_DAYS = 7
MAX_ITEMS = 60             # iOS keeps at most 64 pending local notifications per app
BRIEF_DELAY_S = 180        # the brief is composed at its time and takes a little while
CHECK_DELAY_S = 120        # a scheduled check's answer: p50 7 s, max 66 s on the server
REVIEW_DELAY_S = 120
_BODY = 178


def _short(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _BODY else flat[:_BODY - 1].rstrip() + "…"


def _at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=LOCAL_TZ)


def _held(pause: dict | None, at: float, scope: str) -> bool:
    """Is something of `scope` ("work" or "all") due at `at` held by the pause in force?"""
    if not pause:
        return False
    if pause.get("until_at") and at >= float(pause["until_at"]):
        return False
    return scope == "work" or pause.get("scope") == "all"


def _reminders(now: dt.datetime, end: dt.datetime, agenda: dict) -> list[dict]:
    out = []
    for r in agenda.get("reminders", []):
        due = r["due"]
        while due <= end:
            if due > now:
                out.append({"id": f"reminder:{due.isoformat()}:{r['message'][:40]}",
                            "kind": "reminder", "at": due, "title": "Reminder",
                            "body": _short(r["message"]), "screen": "inbox", "scope": "all"})
            if r.get("repeat") != "daily":
                break
            due = due + dt.timedelta(days=1)
    return out


def _brief(now: dt.datetime, end: dt.datetime, agenda: dict) -> list[dict]:
    brief = agenda.get("brief") or {}
    if not brief.get("enabled"):
        return []
    out = []
    day = now.date()
    while day <= end.date():
        due = _at(day, brief.get("time") or "08:00")
        already = day.isoformat() == brief.get("last_date")
        if due > now and not already and due <= end:
            out.append({"id": f"brief:{day.isoformat()}", "kind": "brief",
                        "at": due + dt.timedelta(seconds=BRIEF_DELAY_S), "title": "Morning brief",
                        "body": "Your morning brief is ready.", "screen": "inbox", "scope": "all"})
        day += dt.timedelta(days=1)
    return out


def _checks(now: dt.datetime, end: dt.datetime, agenda: dict) -> list[dict]:
    from backend.brain import chronos
    out = []
    for t in agenda.get("tasks", []):
        stop = chronos._until_date(t.get("until", ""))
        due = t["due"]
        while due is not None and due <= end and (stop is None or due.date() <= stop):
            if due > now:
                out.append({"id": f"check:{due.isoformat()}:{t['task'][:40]}", "kind": "check",
                            "at": due + dt.timedelta(seconds=CHECK_DELAY_S),
                            "title": "Scheduled check", "body": _short(t["task"]),
                            "screen": "inbox", "scope": "work"})
            due = chronos.next_task_occurrence(due, t["repeat"], due)
    return out


def _checkins(now: dt.datetime, end: dt.datetime) -> list[dict]:
    from backend.brain import planday
    from backend.storage import checkins
    enabled, when = planday.checkin_config()
    if not enabled:
        return []
    out = []
    day = now.date()
    while day <= end.date():
        due = _at(day, when)
        if now < due <= end:
            if day == now.date():
                row = checkins.get(day.isoformat())
                wanted = planday.checkin_payload(day) is not None and not (row and row.get("sent_at"))
            else:
                wanted = bool(planday.live_blocks_on(day))
            if wanted:
                out.append({"id": f"checkin:{day.isoformat()}", "kind": "checkin", "at": due,
                            "title": "Evening check-in", "body": "How did today's plan go?",
                            "screen": "today", "scope": "all"})
        day += dt.timedelta(days=1)
    return out


def _review(now: dt.datetime, end: dt.datetime) -> list[dict]:
    from backend.brain import planday
    from backend.brain import planner as P
    from backend.storage import plans
    enabled, when = planday.review_config()
    if not enabled:
        return []
    out = []
    day = now.date()
    while day <= end.date():
        if day.weekday() == 6:
            week = P.monday_of(day).isoformat()
            due = _at(day, when)
            if (now < due <= end and plans.applied(week) is not None
                    and plans.get_setting("last_review_week") != week):
                out.append({"id": f"review:{week}", "kind": "review",
                            "at": due + dt.timedelta(seconds=REVIEW_DELAY_S),
                            "title": "Weekly review", "body": "Your week, counted, and next week drawn.",
                            "screen": "inbox", "scope": "work"})
        day += dt.timedelta(days=1)
    return out


def items(now: dt.datetime | None = None) -> list[dict]:
    """The next week's notifications, soonest first, at most MAX_ITEMS. Each part degrades
    alone: one store failing leaves the rest. Times are epoch seconds."""
    now = (now or dt.datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    end = now + dt.timedelta(days=HORIZON_DAYS)
    try:
        from backend.brain import chronos
        agenda = chronos.agenda()
    except Exception as exc:
        log.debug("upcoming: schedule unreadable (%s)", exc)
        agenda = {}
    try:
        from backend.storage import pauses
        pause = pauses.active(now=now.timestamp())
    except Exception:
        pause = None
    found: list[dict] = []
    for part in (lambda: _reminders(now, end, agenda), lambda: _brief(now, end, agenda),
                 lambda: _checks(now, end, agenda), lambda: _checkins(now, end),
                 lambda: _review(now, end)):
        try:
            found.extend(part())
        except Exception as exc:
            log.debug("upcoming: one part unreadable (%s)", exc)
    out = []
    for it in sorted(found, key=lambda i: i["at"]):
        at = it["at"].timestamp()
        if _held(pause, at, it.pop("scope")):
            continue
        out.append({**it, "at": at})
    return out[:MAX_ITEMS]
