"""An in-memory stand-in for the Google Calendar v3 client, for tests and probes.

It models the calls `calendar.py` makes (events list / get / insert / patch / delete and
freebusy query) and, more importantly, the behaviours that make calendar code go wrong
against the real API and that a naive fake would hide:

  * **Times come back in the calendar's own zone (the owner's zone)** whatever offset they were
    sent with. An event sent as 18:00+00:00 reads back as 20:00+02:00 -- which is exactly
    what happened when the UTC server created "18:00" events.
  * **Lists are paged** (`maxResults`, `nextPageToken`) and sorted, so a caller that reads
    only the first page silently loses events.
  * **Callers may choose an event id** (base32hex, at least 5 characters), and inserting an
    id that already exists is a 409. That is what lets a retried plan write be a no-op
    instead of a duplicate.
  * **Deleting marks an event cancelled**; lists skip cancelled events; deleting twice is
    410 Gone.
  * **Failures can be injected** per method, to test a write that fails half-way and is
    then retried.

Nothing here talks to the network. It is test support, not part of the running assistant.
"""
from __future__ import annotations

import copy
import os
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

def _owner_zone() -> ZoneInfo:
    """The owner's zone, JARVIS_TIMEZONE (default UTC). Read here rather than from
    backend.localtime: this module is loaded standalone and imports nothing from backend."""
    try:
        return ZoneInfo(os.environ.get("JARVIS_TIMEZONE", "").strip() or "UTC")
    except Exception:
        return ZoneInfo("UTC")


LOCAL_TZ = _owner_zone()
_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")


class FakeHttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: `.resp.status`."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(f'<HttpError {status} "{message}">')
        self.status_code = status
        self.resp = type("Resp", (), {"status": status})()


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def instant(node: dict) -> datetime:
    """The moment an event's start/end node refers to, as an aware datetime."""
    if "dateTime" in node:
        dt = datetime.fromisoformat(str(node["dateTime"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(node.get("timeZone") or LOCAL_TZ.key))
        return dt
    return datetime.combine(date.fromisoformat(node["date"]), dtime.min, LOCAL_TZ)


def _normalise(node: dict) -> dict:
    if "dateTime" in node:
        return {"dateTime": instant(node).astimezone(LOCAL_TZ).isoformat(), "timeZone": LOCAL_TZ.key}
    return {"date": node["date"]}


class FakeCalendarService:
    def __init__(self, page_limit: int = 2500):
        self.items: dict[str, dict] = {}
        self.page_limit = page_limit
        self.calls: list[tuple] = []          # (method, key details) in order
        self._counter = 0
        self._failures: dict[str, list] = {}

    # -- test helpers ---------------------------------------------------------------
    def fail(self, method: str, exc: Exception, times: int = 1) -> None:
        """Make the next `times` calls of `method` (insert, patch, delete, ...) raise."""
        self._failures.setdefault(method, []).extend([exc] * times)

    def seed(self, summary: str, start: datetime, end: datetime, *, event_id: str | None = None,
             **extra) -> dict:
        """Add an event as if the owner had made it himself: no JARVIS marker."""
        self._counter += 1
        eid = event_id or f"seed{self._counter:05d}"
        ev = {
            "id": eid, "status": "confirmed", "summary": summary,
            "start": _normalise({"dateTime": start.isoformat()}),
            "end": _normalise({"dateTime": end.isoformat()}),
            "htmlLink": f"https://calendar.example/{eid}",
            **extra,
        }
        self.items[eid] = ev
        return copy.deepcopy(ev)

    def live(self) -> list[dict]:
        """Every event that is not cancelled, in time order."""
        return sorted((copy.deepcopy(e) for e in self.items.values() if e["status"] != "cancelled"),
                      key=lambda e: (instant(e["start"]), e["id"]))

    def _maybe_fail(self, method: str) -> None:
        queue = self._failures.get(method)
        if queue:
            raise queue.pop(0)

    # -- API surface ----------------------------------------------------------------
    def events(self):
        return _Events(self)

    def freebusy(self):
        return _FreeBusy(self)


class _Events:
    def __init__(self, svc: FakeCalendarService):
        self._svc = svc

    def list(self, calendarId="primary", timeMin=None, timeMax=None, maxResults=250,
             singleEvents=True, orderBy=None, pageToken=None, privateExtendedProperty=None,
             showDeleted=False, **_ignored):
        svc = self._svc

        def run():
            svc._maybe_fail("list")
            svc.calls.append(("list", timeMin, timeMax, maxResults, pageToken))
            lo = datetime.fromisoformat(timeMin.replace("Z", "+00:00")) if timeMin else None
            hi = datetime.fromisoformat(timeMax.replace("Z", "+00:00")) if timeMax else None
            wanted = privateExtendedProperty
            if isinstance(wanted, str):
                wanted = [wanted]
            hits = []
            for ev in svc.items.values():
                if ev["status"] == "cancelled" and not showDeleted:
                    continue
                start, end = instant(ev["start"]), instant(ev["end"])
                if lo is not None and end <= lo:
                    continue
                if hi is not None and start >= hi:
                    continue
                if wanted:
                    priv = (ev.get("extendedProperties") or {}).get("private") or {}
                    if not all(priv.get(k) == v for k, v in (w.split("=", 1) for w in wanted)):
                        continue
                hits.append(ev)
            hits.sort(key=lambda e: (instant(e["start"]), e["id"]))
            offset = int(pageToken or 0)
            size = max(1, min(int(maxResults), svc.page_limit))
            page = hits[offset:offset + size]
            out = {"kind": "calendar#events", "items": copy.deepcopy(page)}
            if offset + size < len(hits):
                out["nextPageToken"] = str(offset + size)
            return out
        return _Request(run)

    def get(self, calendarId="primary", eventId="", **_ignored):
        svc = self._svc

        def run():
            svc._maybe_fail("get")
            svc.calls.append(("get", eventId))
            if eventId not in svc.items:
                raise FakeHttpError(404, "Not Found")
            return copy.deepcopy(svc.items[eventId])
        return _Request(run)

    def insert(self, calendarId="primary", body=None, **_ignored):
        svc = self._svc

        def run():
            svc._maybe_fail("insert")
            payload = copy.deepcopy(body or {})
            svc.calls.append(("insert", payload.get("id"), payload.get("summary")))
            if "start" not in payload or "end" not in payload:
                raise FakeHttpError(400, "Missing start or end")
            eid = payload.get("id")
            if eid is not None:
                if not _ID_RE.match(eid):
                    raise FakeHttpError(400, "Invalid resource id value")
                if eid in svc.items:
                    raise FakeHttpError(409, "The requested identifier already exists.")
            else:
                svc._counter += 1
                eid = f"gen{svc._counter:05d}"
            if instant(payload["end"]) <= instant(payload["start"]):
                raise FakeHttpError(400, "The specified time range is empty.")
            ev = {**payload, "id": eid, "status": "confirmed",
                  "start": _normalise(payload["start"]), "end": _normalise(payload["end"]),
                  "htmlLink": f"https://calendar.example/{eid}"}
            svc.items[eid] = ev
            return copy.deepcopy(ev)
        return _Request(run)

    def patch(self, calendarId="primary", eventId="", body=None, **_ignored):
        svc = self._svc

        def run():
            svc._maybe_fail("patch")
            svc.calls.append(("patch", eventId, sorted((body or {}).keys())))
            ev = svc.items.get(eventId)
            if ev is None or ev["status"] == "cancelled":
                raise FakeHttpError(404, "Not Found")
            for key, value in copy.deepcopy(body or {}).items():
                if key in ("start", "end"):
                    ev[key] = _normalise(value)
                elif key == "extendedProperties":
                    merged = ev.setdefault("extendedProperties", {}).setdefault("private", {})
                    merged.update((value or {}).get("private") or {})
                else:
                    ev[key] = value
            if instant(ev["end"]) <= instant(ev["start"]):
                raise FakeHttpError(400, "The specified time range is empty.")
            return copy.deepcopy(ev)
        return _Request(run)

    def delete(self, calendarId="primary", eventId="", **_ignored):
        svc = self._svc

        def run():
            svc._maybe_fail("delete")
            svc.calls.append(("delete", eventId))
            ev = svc.items.get(eventId)
            if ev is None:
                raise FakeHttpError(404, "Not Found")
            if ev["status"] == "cancelled":
                raise FakeHttpError(410, "Resource has been deleted")
            ev["status"] = "cancelled"
            return ""
        return _Request(run)


class _FreeBusy:
    def __init__(self, svc: FakeCalendarService):
        self._svc = svc

    def query(self, body=None, **_ignored):
        svc = self._svc

        def run():
            svc._maybe_fail("freebusy")
            svc.calls.append(("freebusy",))
            lo = datetime.fromisoformat(body["timeMin"].replace("Z", "+00:00"))
            hi = datetime.fromisoformat(body["timeMax"].replace("Z", "+00:00"))
            busy = []
            for ev in svc.live():
                if ev.get("transparency") == "transparent":
                    continue
                start, end = instant(ev["start"]), instant(ev["end"])
                if end <= lo or start >= hi:
                    continue
                busy.append({
                    "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            calendars = {item["id"]: {"busy": busy} for item in body.get("items", [])}
            return {"calendars": calendars}
        return _Request(run)
