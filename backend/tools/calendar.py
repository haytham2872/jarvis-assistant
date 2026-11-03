"""backend/tools/calendar.py — Google Calendar for JARVIS.

Everything here is **local time**, stated explicitly. It used to be the machine's clock:
a time with no offset was given the *server's* offset, and listings were shown in the
server's zone. The GCP server ran UTC, so "gym at 18:00" was stored as 20:00 local time, and
JARVIS then confirmed and listed it as 18:00 -- consistent with itself, wrong on
the owner's phone. Reproduced on a UTC Linux process before this change (control roadmap,
P0). Now:

  * A time without an offset means local time; one with an offset keeps its instant. Events are
    written with `timeZone: the owner's zone` and read back in local time, whatever clock the
    process runs on.
  * The confirmation shows what Google *stored*, not what was sent, and says so if they
    differ.
  * A listing names the day of every event (a week of bare `HH:MM` cannot tell Monday's
    meeting from Thursday's), shows each event's id (later phases refer to events by it),
    pages through the API instead of stopping silently at 20, and says when it stops.
  * An unreadable date is an error, not "today".
  * Every event created here carries a private marker (`extendedProperties.private.jarvis`)
    and a distinct colour, so JARVIS can tell his own events from the owner's (and the owner can
    see them at a glance). Listings show the marker.
  * `update_event` and `delete_event` work by id (P3). Whether the event is JARVIS's own decides
    who has to agree to changing it: the gateway looks it up with `event_owner` (an event he
    made runs, anything else is the owner's data and needs his word).
  * The week planner writes its blocks through `write_event` / `patch_event` / `delete_event_raw`
    under ids it derives (`event_id_for`), so a write that is retried after a lost answer is a
    409 that is recognised, never a second event.

This module imports nothing from `backend`, so it can be loaded standalone.
"""
import os
import asyncio
import base64
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TOKEN_PATH = Path(__file__).parent.parent.parent / "token_calendar.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def _owner_zone() -> ZoneInfo:
    """The owner's zone, JARVIS_TIMEZONE (default UTC). Read here rather than from
    backend.localtime: this module is loaded standalone and imports nothing from backend."""
    try:
        return ZoneInfo(os.environ.get("JARVIS_TIMEZONE", "").strip() or "UTC")
    except Exception:
        return ZoneInfo("UTC")


LOCAL_TZ = _owner_zone()
CALENDAR_ID = "primary"           # tests and live checks may point this at a scratch calendar
MARKER_KEY = "jarvis"             # private extended property set on every event JARVIS creates
PLAN_KEY = "jarvis_plan"          # ... the Monday of the plan's week, on a plan block
BLOCK_KEY = "jarvis_block"        # ... and the block's own uid
JARVIS_COLOR_ID = "3"             # Google's "grape": not the calendar's default, so his events stand out
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HARD_CAP = 250                   # most events one listing returns
_PAGE = 250                       # Google's page size for our requests
_MAX_DAYS = 90

_service = None


def _client_config() -> dict:
    client_id     = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env")
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _get_service():
    global _service
    if _service is not None:
        return _service

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        log.info("Calendar: loaded token from %s", TOKEN_PATH)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Calendar: refreshing expired token")
            creds.refresh(Request())
        else:
            log.info("Calendar: opening browser for OAuth consent...")
            flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
            log.info("Calendar: OAuth consent complete")
        TOKEN_PATH.write_text(creds.to_json())
        log.info("Calendar: token saved to %s", TOKEN_PATH)

    _service = build("calendar", "v3", credentials=creds)
    log.info("Calendar service ready.")
    return _service


# ---------------------------------------------------------------------------
# Time: local time, explicitly
# ---------------------------------------------------------------------------

def _now() -> datetime:
    """Now in local time, whatever this machine's clock says."""
    return datetime.now(LOCAL_TZ)


def _parse_dt(text: str) -> datetime:
    """An ISO 8601 date-time as an aware local time datetime.

    No offset means local time; an offset (or Z) keeps its instant. A wall time that does not
    exist (02:30 on the night the clocks go forward) is moved to the real instant rather
    than kept as a fiction. Raises ValueError on text that is not ISO 8601.
    """
    dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def _plus_minutes(dt: datetime, minutes: int) -> datetime:
    """Real elapsed minutes: an hour-long event that spans a clock change is still an hour."""
    return (dt.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(LOCAL_TZ)


def _parse_day(date_str: str):
    """local midnight of 'today', 'tomorrow' or an ISO date; None when it cannot be read.

    (It used to fall back to today, so "next Tuesday" quietly listed the wrong day.)
    """
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    text = (date_str or "").strip().lower()
    if not text or text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return _parse_dt(date_str)
    except ValueError:
        return None


def _day_label(dt: datetime) -> str:
    """'Mon 2026-10-05': the weekday for reading, the ISO date for passing back to a tool."""
    return f"{_DAYS[dt.weekday()]} {dt.date().isoformat()}"


def _bounds(event: dict):
    """(start, end, all_day): aware local time datetimes; for all-day events, local midnights with
    the end exclusive. (None, None, False) when the event cannot be read."""
    try:
        s, e = event["start"], event["end"]
        if "dateTime" in s:
            return _parse_dt(s["dateTime"]), _parse_dt(e["dateTime"]), False
        return (datetime.combine(date.fromisoformat(s["date"]), dtime.min, LOCAL_TZ),
                datetime.combine(date.fromisoformat(e["date"]), dtime.min, LOCAL_TZ), True)
    except Exception:
        return None, None, False


def is_jarvis_made(event: dict) -> bool:
    return ((event.get("extendedProperties") or {}).get("private") or {}).get(MARKER_KEY) == "1"


def _fmt_event(event: dict) -> str:
    start, end, all_day = _bounds(event)
    if start is None:
        when = "?"
    elif all_day:
        last = end - timedelta(days=1)
        when = "all day" if last.date() <= start.date() else f"all day until {_day_label(last)}"
    else:
        when = f"{start:%H:%M}-{end:%H:%M}"
        spill = (end.date() - start.date()).days
        if spill > 0:
            when += f" (+{spill}d)"
    summary  = event.get("summary", "(no title)")
    loc      = event.get("location", "")
    loc_part = f" @ {loc}" if loc else ""
    mine     = " [JARVIS-made]" if is_jarvis_made(event) else ""
    ident    = f"  [id {event['id']}]" if event.get("id") else ""
    return f"{when}: {summary}{loc_part}{mine}{ident}"



# ---------------------------------------------------------------------------
# Plan blocks: ids, writes that survive a lost answer, busy time (P3)
# ---------------------------------------------------------------------------

class EventIdTaken(RuntimeError):
    """The id we derived belongs to an event that was deleted (Google keeps a deleted event's id
    reserved), so a new generation of the block has to use another one."""


def _http_status(exc) -> int | None:
    """The HTTP status of a Google API error (also the test double's), else None."""
    try:
        return int(getattr(getattr(exc, "resp", None), "status", None))
    except (TypeError, ValueError):
        return None


def event_id_for(week: str, uid: str, generation: int) -> str:
    """A Google event id for one block of a plan.

    Google accepts ids of 5 to 1024 characters from base32hex (0-9, a-v). This one is derived
    from (week, block, generation), so writing the same block twice is a 409 that
    `write_event` recognises -- a retried apply cannot leave two events. A block written again
    after its event was deleted gets the next generation, because Google keeps a deleted
    event's id reserved.
    """
    digest = hashlib.sha1(f"{week}|{uid}|{generation}".encode("utf-8")).digest()
    return "jv" + base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")[:26]


def block_body(title: str, start: datetime, end: datetime, *, week: str, uid: str) -> dict:
    """The event for one plan block: marked as JARVIS's, coloured, opaque (it blocks time)."""
    return {
        "summary": title,
        "start": {"dateTime": start.astimezone(LOCAL_TZ).isoformat(), "timeZone": LOCAL_TZ.key},
        "end": {"dateTime": end.astimezone(LOCAL_TZ).isoformat(), "timeZone": LOCAL_TZ.key},
        "colorId": JARVIS_COLOR_ID,
        "transparency": "opaque",
        "extendedProperties": {"private": {MARKER_KEY: "1", PLAN_KEY: week, BLOCK_KEY: uid}},
    }


def read_event(svc, event_id: str):
    """The event, or None when it does not exist or has been deleted."""
    try:
        event = svc.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except Exception as exc:
        if _http_status(exc) in (404, 410):
            return None
        raise
    return None if event.get("status") == "cancelled" else event


def event_owner(svc, event_id: str) -> str:
    """Whose event is this? `jarvis` (made by JARVIS: marker present), `owner` (anyone else's),
    or `gone` (no such event, or deleted)."""
    event = read_event(svc, event_id)
    if event is None:
        return "gone"
    return "jarvis" if is_jarvis_made(event) else "owner"


def _same_times(event: dict, body: dict) -> bool:
    start, end, _ = _bounds(event)
    try:
        return (start == _parse_dt(body["start"]["dateTime"])
                and end == _parse_dt(body["end"]["dateTime"]))
    except Exception:
        return False


def write_event(svc, event_id: str, body: dict) -> str:
    """Insert `body` under a caller-chosen id. Returns `created`, `exists` (an earlier attempt
    already wrote it and only the answer was lost: found, and it is the same event) or
    `repaired` (found, but different -- an earlier attempt of an older plan -- so it is put
    right). Raises `EventIdTaken` when the id belongs to a deleted event."""
    try:
        svc.events().insert(calendarId=CALENDAR_ID, body=dict(body, id=event_id)).execute()
        return "created"
    except Exception as exc:
        if _http_status(exc) != 409:
            raise
    existing = svc.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    if existing.get("status") == "cancelled":
        raise EventIdTaken(event_id)
    if _same_times(existing, body) and existing.get("summary") == body.get("summary"):
        return "exists"
    svc.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()
    return "repaired"


def patch_event(svc, event_id: str, *, start: datetime | None = None, end: datetime | None = None,
                title: str | None = None) -> dict:
    """Change an event's time and/or title. Returns what Google stored."""
    body: dict = {}
    if title is not None:
        body["summary"] = title
    if start is not None:
        body["start"] = {"dateTime": start.astimezone(LOCAL_TZ).isoformat(), "timeZone": LOCAL_TZ.key}
    if end is not None:
        body["end"] = {"dateTime": end.astimezone(LOCAL_TZ).isoformat(), "timeZone": LOCAL_TZ.key}
    return svc.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()


def delete_event_raw(svc, event_id: str) -> str:
    """Delete by id. `deleted`, or `gone` when there was nothing left to delete."""
    try:
        svc.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return "deleted"
    except Exception as exc:
        if _http_status(exc) in (404, 410):
            return "gone"
        raise


def _declined_by_me(event: dict) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined"
               for a in event.get("attendees") or [])


def busy_windows(events) -> list:
    """(start, end, label, event id) for the events that occupy the owner's time: not deleted,
    not marked "free", not an invitation he declined. An all-day event that blocks time (marked
    busy) covers its whole days."""
    out = []
    for event in events:
        if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
            continue
        if _declined_by_me(event):
            continue
        start, end, _all_day = _bounds(event)
        if start is None or end is None or end <= start:
            continue
        out.append((start, end, event.get("summary", "(no title)"), event.get("id", "")))
    return out


def event_span(event: dict):
    """(start, end) as aware datetimes for an event with a clock time, else None (all-day or
    unreadable)."""
    start, end, all_day = _bounds(event)
    if start is None or end is None or all_day:
        return None
    return start, end


def fetch_events(svc, time_min: datetime, time_max: datetime, cap: int = _HARD_CAP, **extra):
    """(events, truncated) in the window, paged. `extra`: query parameters."""
    return _fetch_events(svc, time_min, time_max, cap, **extra)


def fetch_marked(svc, time_min: datetime, time_max: datetime):
    """JARVIS's own events in the window, deleted ones included (so a block the owner deleted can
    be told from one that was never there)."""
    return _fetch_events(svc, time_min, time_max, _HARD_CAP,
                         privateExtendedProperty=f"{MARKER_KEY}=1", showDeleted=True)

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _fetch_events(svc, time_min: datetime, time_max: datetime, cap: int, **extra):
    """Every event in the window up to `cap`, paging through the API.

    Returns (events, truncated). The old single request asked for 20 and stopped, with no
    sign that more existed -- a busy week simply lost its later days. `extra` adds query
    parameters (the plan sync asks only for JARVIS-marked events, deleted ones included).
    """
    events: list = []
    token = None
    for _ in range(50):                                   # a runaway token loop cannot spin forever
        req = dict(
            calendarId=CALENDAR_ID,
            timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
            maxResults=max(1, min(_PAGE, cap - len(events))),
            singleEvents=True, orderBy="startTime", **extra,
        )
        if token:
            req["pageToken"] = token
        page = svc.events().list(**req).execute()
        events.extend(page.get("items", []))
        token = page.get("nextPageToken")
        if not token:
            return events, False
        if len(events) >= cap:
            return events, True
    return events, True


def _list_events(svc, date_str: str, days: int, max_results: int) -> str:
    day_start = _parse_day(date_str)
    if day_start is None:
        return f"Error: could not read the date {date_str!r}; use 'today', 'tomorrow' or YYYY-MM-DD."
    try:
        days = max(1, min(int(days or 1), _MAX_DAYS))
        cap = max(1, min(int(max_results or _HARD_CAP), _HARD_CAP))
    except (TypeError, ValueError):
        return "Error: days and max_results must be whole numbers."
    day_end = day_start + timedelta(days=days)
    last_day = day_end - timedelta(days=1)
    span = (f"on {_day_label(day_start)}" if days == 1
            else f"from {_day_label(day_start)} to {_day_label(last_day)}")

    events, truncated = _fetch_events(svc, day_start, day_end, cap)
    if not events:
        return f"No events {span} (local time)."

    lines = [f"Events {span} (local time), {len(events)}{'+' if truncated else ''} in all:"]
    current = None
    for ev in events:
        start, _, _ = _bounds(ev)
        key = start.date() if start else None
        if key != current:
            current = key
            lines.append(_day_label(start) if start else "(undated)")
        lines.append(f"  {_fmt_event(ev)}")
    if truncated:
        lines.append(f"(Only the first {len(events)} are shown; this range holds more. "
                     "Ask for fewer days to see the rest.)")
    return "\n".join(lines)


def _create_event(svc, title, start_time, end_time, duration_minutes, description, location) -> str:
    if not title:
        return "Error: title is required for create_event."
    if not start_time:
        return "Error: start_time is required for create_event (ISO 8601 format)."
    try:
        start_dt = _parse_dt(start_time)
        if end_time:
            end_dt = _parse_dt(end_time)
        else:
            minutes = int(duration_minutes)
            if minutes <= 0:
                return "Error: duration_minutes must be positive."
            end_dt = _plus_minutes(start_dt, minutes)
    except (TypeError, ValueError):
        return "Error: start_time and end_time must be ISO 8601, for example 2026-10-05T18:00."
    if end_dt <= start_dt:
        return "Error: the event must end after it starts."

    event_body: dict = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": LOCAL_TZ.key},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": LOCAL_TZ.key},
        "colorId": JARVIS_COLOR_ID,
        "extendedProperties": {"private": {MARKER_KEY: "1"}},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    created = svc.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
    stored_start, stored_end, _ = _bounds(created)
    ident = created.get("id", "")
    if stored_start is None or stored_start != start_dt or stored_end != end_dt:
        got = (f"{stored_start:%Y-%m-%d %H:%M}" if stored_start else "an unreadable time")
        return (f"Calendar error: the event was created (id {ident}) but Google stored it at "
                f"{got} instead of {start_dt:%Y-%m-%d %H:%M} local time. Check it in the calendar.")
    link = created.get("htmlLink", "")
    link_part = f" Link: {link}" if link else ""
    return (
        f"Event created: '{title}', {_day_label(stored_start)} "
        f"{stored_start:%H:%M}-{stored_end:%H:%M} (local time) [id {ident}].{link_part}"
    )


def _update_event(svc, event_id, title, start_time, end_time, description, location) -> str:
    if not event_id:
        return "Error: event_id is required for update_event (a listing shows each event's id)."
    event = read_event(svc, event_id)
    if event is None:
        return f"Error: there is no event with id {event_id} (it may have been deleted)."
    start, end, all_day = _bounds(event)
    changes: dict = {}
    if title:
        changes["summary"] = title
    if description:
        changes["description"] = description
    if location:
        changes["location"] = location
    new_start, new_end = start, end
    if start_time or end_time:
        if all_day or start is None:
            return "Error: that event has no clock time (all-day or unreadable), so its time cannot be moved here."
        try:
            new_start = _parse_dt(start_time) if start_time else start
            # Moving the start keeps the length unless an end is given.
            new_end = (_parse_dt(end_time) if end_time
                       else _plus_minutes(new_start, int((end - start).total_seconds() // 60)))
        except ValueError:
            return "Error: start_time and end_time must be ISO 8601, for example 2026-10-05T18:00."
        if new_end <= new_start:
            return "Error: the event must end after it starts."
        changes["start"] = {"dateTime": new_start.isoformat(), "timeZone": LOCAL_TZ.key}
        changes["end"] = {"dateTime": new_end.isoformat(), "timeZone": LOCAL_TZ.key}
    if not changes:
        return "Error: nothing to change; give title, start_time, end_time, description or location."
    before = _fmt_event(event)
    updated = svc.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=changes).execute()
    stored_start, stored_end, _ = _bounds(updated)
    if "start" in changes and (stored_start != new_start or stored_end != new_end):
        got = f"{stored_start:%Y-%m-%d %H:%M}" if stored_start else "an unreadable time"
        return (f"Calendar error: the event was updated but Google stored it at {got} instead of "
                f"{new_start:%Y-%m-%d %H:%M} local time. Check it in the calendar.")
    return f"Event updated. Was: {before}\nNow: {_fmt_event(updated)}"


def _delete_event(svc, event_id) -> str:
    if not event_id:
        return "Error: event_id is required for delete_event (a listing shows each event's id)."
    event = read_event(svc, event_id)
    if event is None:
        return f"Error: there is no event with id {event_id} (it may already have been deleted)."
    what = _fmt_event(event)
    svc.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    return f"Event deleted: {what}"



def _check_free_busy(svc, start_time, end_time) -> str:
    if not start_time or not end_time:
        return "Error: start_time and end_time are required for check_free_busy."
    try:
        start_dt, end_dt = _parse_dt(start_time), _parse_dt(end_time)
    except ValueError:
        return "Error: start_time and end_time must be ISO 8601, for example 2026-10-05T09:00."
    if end_dt <= start_dt:
        return "Error: end_time must be after start_time."

    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "items":   [{"id": CALENDAR_ID}],
    }
    result    = svc.freebusy().query(body=body).execute()
    busy_list = result.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
    same_day = start_dt.date() == end_dt.date()
    if not busy_list:
        if same_day:
            return (f"You are free on {_day_label(start_dt)} between {start_dt:%H:%M} and "
                    f"{end_dt:%H:%M} (local time).")
        return (f"You are free between {_day_label(start_dt)} {start_dt:%H:%M} and "
                f"{_day_label(end_dt)} {end_dt:%H:%M} (local time).")
    lines = ["Busy windows (local time):"]
    for slot in busy_list:
        try:
            s, e = _parse_dt(slot["start"]), _parse_dt(slot["end"])
            spill = f" (+{(e.date() - s.date()).days}d)" if e.date() > s.date() else ""
            lines.append(f"  {_day_label(s)} {s:%H:%M}-{e:%H:%M}{spill}")
        except Exception:
            lines.append(f"  {slot.get('start')} to {slot.get('end')}")
    return "\n".join(lines)


def _do(action, date_str, days, title, start_time, end_time, duration_minutes,
        description, location, max_results, event_id=""):
    try:
        svc = _get_service()
    except Exception as e:
        log.error("Calendar auth error: %s", e)
        return f"Calendar auth failed: {e}"

    try:
        if action == "list_events":
            return _list_events(svc, date_str, days, max_results)
        if action == "create_event":
            return _create_event(svc, title, start_time, end_time, duration_minutes,
                                 description, location)
        if action == "update_event":
            return _update_event(svc, event_id, title, start_time, end_time, description, location)
        if action == "delete_event":
            return _delete_event(svc, event_id)
        if action == "check_free_busy":
            return _check_free_busy(svc, start_time, end_time)
        return f"Unknown action: {action}"
    except Exception as e:
        log.error("Calendar action '%s' failed: %s", action, e)
        return f"Calendar error: {e}"


async def calendar_action(
    action: str,
    date: str = "",
    days: int = 1,
    title: str = "",
    start_time: str = "",
    end_time: str = "",
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    max_results: int = _HARD_CAP,
    event_id: str = "",
    quote: str = "",             # evidence for the policy check; the gateway removes it before dispatch
) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _do, action, date, days, title, start_time, end_time,
        duration_minutes, description, location, max_results, event_id
    )


def _owner_sync(event_id: str) -> str:
    try:
        return event_owner(_get_service(), event_id)
    except Exception as exc:
        log.warning("Calendar: could not look up event %s (%s)", event_id, exc)
        return "unknown"


def _describe_sync(event_id: str) -> str:
    try:
        event = read_event(_get_service(), event_id)
    except Exception:
        return ""
    if event is None:
        return ""
    span = event_span(event)
    title = event.get("summary", "(no title)")
    if span is None:
        return f"'{title}' (all day)"
    return f"'{title}' ({_day_label(span[0])} {span[0]:%H:%M}-{span[1]:%H:%M})"


async def describe_event(event_id: str) -> str:
    """A short description of an event ("'Dentist' (Wed 2026-10-07 15:00-16:00)"), or "". What
    the owner reads when he is asked to approve changing it."""
    if not event_id:
        return ""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _describe_sync, event_id)


async def owner_of(event_id: str) -> str:
    """`jarvis`, `his`, `gone`, or `unknown` (the lookup itself failed). The tool gateway asks
    this before an edit or delete, because it decides who has to agree to it."""
    if not event_id:
        return "none"
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _owner_sync, event_id)
