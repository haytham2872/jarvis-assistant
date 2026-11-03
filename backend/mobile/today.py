"""The phone's Today tab (slice 2 of the 15 October plan): the owner's day on one screen.

His planned blocks with what became of each (tapped in the app, said at the evening check-in,
or recorded by the day's close), yesterday's blocks while that day is still open (until 04:00),
the rest of his calendar, the reminders and timed checks due today, and how the week is going.
Read-only except `tap`, which records one block's outcome through planday.record_tap, the same
record a check-in reply makes. Every part degrades on its own: a calendar that cannot be read
leaves the plan, reminders and week in place.
"""
from __future__ import annotations

import logging
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from threading import RLock

from backend.brain import planday, planner as P, weekplan as W
from backend.storage import checkins, plans

log = logging.getLogger(__name__)
_calendar_cache: dict[date, tuple[float, tuple[list[dict] | None, str]]] = {}
_calendar_lock = RLock()


def clear_calendar_cache(day: date | None = None) -> None:
    """A phone week-plan mutation must discard its calendar view (slice 4)."""
    with _calendar_lock:
        if day is None:
            _calendar_cache.clear()
        else:
            _calendar_cache.pop(day, None)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _offers() -> dict:
    """Pending requests to move a block (a skip's new slot, or a meeting landing on it), keyed
    the way reshuffle names the block: (kind, local day, start "HH:MM")."""
    from backend.storage import approvals
    out = {}
    for r in approvals.pending():
        a = r.get("arguments") or {}
        if r.get("tool") != "week_plan" or a.get("action") != "change" or not a.get("to_day"):
            continue
        try:
            to_day = date.fromisoformat(a["to_day"])
        except (TypeError, ValueError):
            continue
        when = "today" if a.get("day") == a["to_day"] else P.day_name(to_day)
        out[(a.get("kind"), a.get("day"), a.get("near"))] = {
            "id": r["id"], "label": f"Move it to {when} {a.get('start') or ''}".strip(),
            "why": r.get("why") or ""}
    return out


def _block(row: dict, told: dict, now: float, offers: dict | None = None) -> dict:
    start, end = W._eff(row)
    outcome = told.get(row["uid"])
    local = P.local(W._dt(start))
    offer = (offers or {}).get((row["kind"], local.date().isoformat(), f"{local:%H:%M}"))
    return {
        "uid": row["uid"], "kind": row["kind"], "title": W._title(row["kind"]),
        "start": start, "end": end,
        # "moved": he moved its calendar event himself; "failed": it never reached the calendar
        "status": row["status"],
        "outcome": ({"outcome": outcome["outcome"], "reason": outcome.get("reason") or "",
                     "actor": outcome.get("actor") or "", "at": outcome.get("at")} if outcome else None),
        "started": start <= now,
        "offer": offer,                 # a pending request to move it: Approve / Deny on the card
    }


def _blocks(day: date, now: float) -> list[dict]:
    rows = planday.live_blocks_on(day)
    told = plans.outcomes([r["uid"] for r in rows])
    try:
        offers = _offers()
    except Exception as exc:
        log.info("today: approvals unreadable (%s)", exc)
        offers = {}
    return [_block(r, told, now, offers) for r in rows]


def _week(monday: date, now: float) -> list[dict]:
    """Mon..Sun of this week: how many blocks were planned each day and what became of them."""
    out = []
    plan = plans.applied(monday.isoformat())
    by_day = planday._rows_by_day(plan)
    told = plans.outcomes([r["uid"] for rows in by_day.values() for r in rows])
    for i in range(7):
        d = monday + timedelta(days=i)
        rows = by_day.get(d, [])
        counts = {o: 0 for o in plans.OUTCOMES}
        for r in rows:
            o = told.get(r["uid"])
            if o:
                counts[o["outcome"]] += 1
        out.append({"date": d.isoformat(), "day": P.day_name(d)[:3], "planned": len(rows), **counts})
    return out


def _checkin(day: date) -> dict:
    enabled, at = planday.checkin_config()
    ci = checkins.get(day.isoformat())
    return {"enabled": enabled, "time": at,
            "waiting": bool(ci and ci["sent_at"] and not ci["closed_at"]),
            "sent": bool(ci and ci["sent_at"])}


def _agenda(day: date) -> tuple[list[dict], list[dict]]:
    """Today's reminders and timed checks from the schedule (local time)."""
    from backend.brain import chronos
    ag = chronos.agenda()

    def on_day(dt) -> bool:
        return P.local(dt).date() == day
    reminders = [{"at": _ts(r["due"]), "text": r["message"], "repeat": r["repeat"]}
                 for r in ag["reminders"] if on_day(r["due"])]
    tasks = [{"at": _ts(t["due"]), "text": t["task"], "repeat": t["repeat"], "skill": t.get("skill") or ""}
             for t in ag["tasks"] if on_day(t["due"])]
    return reminders, tasks


def _calendar(day: date, *, week: bool = False, plan_events: set[str] | None = None) -> tuple[list[dict] | None, str]:
    """One calendar read per day per minute, including a quiet failure."""
    with _calendar_lock:
        cached = _calendar_cache.get(day)
        if not cached or time.monotonic() - cached[0] >= 60:
            _calendar_cache[day] = (time.monotonic(), _read_calendar(day))
            if len(_calendar_cache) > 7:
                _calendar_cache.pop(next(iter(_calendar_cache)))
        rows, note = deepcopy(_calendar_cache[day][1])
    if rows is None:
        return None, note
    # Cache one source for both views. Today keeps its existing ownership filter;
    # Week also shows assistant-made meetings, excluding only tracked plan events.
    out = []
    for row in rows:
        hidden = row.get("_plan") and row.get("id") in (plan_events or set()) if week else row.get("_owned")
        if not hidden:
            out.append({k: v for k, v in row.items() if k not in ("_owned", "_plan") and (week or k != "id")})
    return out, note


def _read_calendar(day: date) -> tuple[list[dict] | None, str]:
    """His other events today (JARVIS's plan blocks are already shown as blocks). (None, why)
    when the calendar cannot be read; never opens the browser consent flow."""
    from backend.tools import calendar as cal
    if not cal.TOKEN_PATH.exists():
        return None, "Google Calendar is not connected on the server."
    try:
        svc = cal._get_service()
        start = datetime(day.year, day.month, day.day, tzinfo=P.LOCAL_TZ)
        events, truncated = cal.fetch_events(svc, start, start + timedelta(days=1))
    except Exception as exc:
        log.info("today: calendar unreadable (%s)", exc)
        return None, "The calendar could not be read just now."
    out = []
    for e in events:
        if e.get("status") == "cancelled":
            continue
        s, en, all_day = cal._bounds(e)
        if s is None:
            continue
        private = (e.get("extendedProperties") or {}).get("private") or {}
        out.append({"id": e.get("id") or "", "_owned": cal.is_jarvis_made(e),
                    "_plan": bool(cal.is_jarvis_made(e) and private.get(cal.PLAN_KEY) and private.get(cal.BLOCK_KEY)),
                    "title": e.get("summary") or "(no title)", "start": _ts(s), "end": _ts(en),
                    "all_day": all_day, "where": e.get("location") or ""})
    return out, "The calendar has more events than this view can show." if truncated else ""


def view(now: datetime | None = None, *, calendar: bool = True) -> dict:
    """Today, as data for the app. Never raises."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    today = P.local(now).date()
    yesterday = today - timedelta(days=1)
    out: dict = {"date": today.isoformat(), "label": f"{P.day_name(today)} {today.day} {today:%b}", "now": ts}
    try:
        out["blocks"] = _blocks(today, ts)
    except Exception as exc:
        log.warning("today: plan unreadable (%s)", exc)
        out["blocks"], out["plan_error"] = [], "The plan could not be read just now."
    try:
        open_yesterday = not planday.closable({"local_date": yesterday.isoformat()}, now)
        late = [b for b in _blocks(yesterday, ts) if b["outcome"] is None] if open_yesterday else []
        out["yesterday"] = {"date": yesterday.isoformat(), "label": P.day_name(yesterday), "blocks": late}
    except Exception:
        out["yesterday"] = {"date": yesterday.isoformat(), "label": P.day_name(yesterday), "blocks": []}
    try:
        out["checkin"] = _checkin(today)
    except Exception:
        out["checkin"] = {"enabled": False, "time": "", "waiting": False, "sent": False}
    try:
        out["reminders"], out["tasks"] = _agenda(today)
    except Exception as exc:
        log.warning("today: schedule unreadable (%s)", exc)
        out["reminders"], out["tasks"] = [], []
    try:
        out["week"] = _week(P.monday_of(today), ts)
    except Exception:
        out["week"] = []
    if calendar:
        out["events"], out["events_note"] = _calendar(today)
    else:
        out["events"], out["events_note"] = None, ""
    return out


def tap(uid: str, outcome: str, now: datetime | None = None, proposals: list | None = None) -> str:
    """Record one block's outcome. A skip's proposed new slot lands in `proposals`. Raises W._Fail
    with a reason he can read."""
    return planday.record_tap(uid, outcome, now or datetime.now(timezone.utc), proposals)
