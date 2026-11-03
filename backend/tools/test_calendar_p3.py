"""P3: what the week planner needs of the calendar tool -- edit and delete by id, ids that make a
retried write harmless, the busy time of a week, and who made an event.

Against `calendar_fake.FakeCalendarService`, which behaves like Google where it matters: caller
chosen ids (409 on a duplicate), deleted events stay reserved, times come back in Paris.
"""
import asyncio
import datetime
import re
from zoneinfo import ZoneInfo

import pytest

from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService, FakeHttpError

LOCAL_TZ = ZoneInfo("Europe/Paris")


def paris(y, mo, d, h, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def act(**kw):
    return run(cal.calendar_action(**kw))


def block(uid="b1", start=None, end=None):
    start = start or paris(2026, 10, 5, 9)
    return cal.block_body("Gym", start, end or start + datetime.timedelta(hours=1), week="2026-10-05", uid=uid)


# --- ids ---------------------------------------------------------------------------------

def test_event_ids_are_valid_google_ids_and_stable():
    a = cal.event_id_for("2026-10-05", "abc123", 0)
    assert a == cal.event_id_for("2026-10-05", "abc123", 0)
    assert re.fullmatch(r"[a-v0-9]{5,1024}", a) and a.startswith("jv") and len(a) == 28


def test_a_different_week_block_or_generation_gives_a_different_id():
    base = cal.event_id_for("2026-10-05", "abc123", 0)
    assert len({base, cal.event_id_for("2026-10-12", "abc123", 0), cal.event_id_for("2026-10-05", "abc124", 0),
                cal.event_id_for("2026-10-05", "abc123", 1)}) == 4


# --- the block event ---------------------------------------------------------------------

def test_a_block_event_is_marked_coloured_opaque_and_in_paris_time():
    body = block("u9")
    assert body["summary"] == "Gym" and body["transparency"] == "opaque"
    assert body["colorId"] == cal.JARVIS_COLOR_ID
    assert body["extendedProperties"]["private"] == {"jarvis": "1", "jarvis_plan": "2026-10-05", "jarvis_block": "u9"}
    assert body["start"]["timeZone"] == "Europe/Paris" and body["start"]["dateTime"].endswith("+02:00")


def test_a_utc_instant_is_written_as_paris_time():
    utc = datetime.datetime(2026, 10, 5, 7, 0, tzinfo=datetime.timezone.utc)
    body = cal.block_body("Gym", utc, utc + datetime.timedelta(hours=1), week="2026-10-05", uid="x")
    assert body["start"]["dateTime"].startswith("2026-10-05T09:00:00")


def test_events_created_by_create_event_are_coloured_too(svc):
    act(action="create_event", title="Dinner", start_time="2026-10-05T19:00")
    (ev,) = svc.live()
    assert ev["colorId"] == cal.JARVIS_COLOR_ID and cal.is_jarvis_made(ev)


# --- writing with a chosen id -------------------------------------------------------------

def test_write_event_creates_and_a_second_identical_write_is_recognised(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    assert cal.write_event(svc, eid, block()) == "created"
    assert cal.write_event(svc, eid, block()) == "exists"
    assert len(svc.live()) == 1, "a retried write must not leave a second event"


def test_a_write_whose_answer_was_lost_is_found_not_duplicated(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    svc.fail("insert", FakeHttpError(500, "answer lost"))
    with pytest.raises(FakeHttpError):
        cal.write_event(svc, eid, block())
    assert cal.write_event(svc, eid, block()) == "created"
    assert len(svc.live()) == 1


def test_an_older_attempt_with_different_times_is_put_right(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, eid, block(start=paris(2026, 10, 5, 9)))
    assert cal.write_event(svc, eid, block(start=paris(2026, 10, 5, 11))) == "repaired"
    (ev,) = svc.live()
    assert cal.event_span(ev)[0].hour == 11


def test_an_id_reserved_by_a_deleted_event_is_reported_not_reused(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, eid, block())
    assert cal.delete_event_raw(svc, eid) == "deleted"
    with pytest.raises(cal.EventIdTaken):
        cal.write_event(svc, eid, block())


def test_other_write_errors_are_raised(svc):
    svc.fail("insert", FakeHttpError(403, "forbidden"))
    with pytest.raises(FakeHttpError):
        cal.write_event(svc, cal.event_id_for("w", "u", 0), block())


# --- reading, owning, changing, deleting ---------------------------------------------------

def test_read_event_returns_none_for_missing_and_deleted_and_raises_on_other_errors(svc):
    assert cal.read_event(svc, "nope00") is None
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, eid, block())
    assert cal.read_event(svc, eid)["id"] == eid
    cal.delete_event_raw(svc, eid)
    assert cal.read_event(svc, eid) is None
    svc.fail("get", FakeHttpError(500, "boom"))
    with pytest.raises(FakeHttpError):
        cal.read_event(svc, eid)


def test_owner_is_jarvis_his_or_gone(svc):
    mine = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, mine, block())
    his = svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))["id"]
    assert cal.event_owner(svc, mine) == "jarvis"
    assert cal.event_owner(svc, his) == "owner"
    assert cal.event_owner(svc, "zzzzz") == "gone"


def test_owner_of_asks_the_calendar_and_reports_unknown_when_it_cannot(svc, monkeypatch):
    his = svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))["id"]
    assert run(cal.owner_of(his)) == "owner" and run(cal.owner_of("")) == "none"
    svc.fail("get", FakeHttpError(500, "boom"))
    assert run(cal.owner_of(his)) == "unknown"
    monkeypatch.setattr(cal, "_get_service", lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    assert run(cal.owner_of(his)) == "unknown"


def test_describe_event_names_the_title_and_the_time(svc):
    his = svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))["id"]
    assert run(cal.describe_event(his)) == "'Dentist' (Wed 2026-10-07 15:00-16:00)"
    assert run(cal.describe_event("gone00")) == "" and run(cal.describe_event("")) == ""


def test_patch_event_moves_the_time_and_returns_what_google_stored(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, eid, block())
    stored = cal.patch_event(svc, eid, start=paris(2026, 10, 6, 18), end=paris(2026, 10, 6, 19))
    assert cal.event_span(stored) == (paris(2026, 10, 6, 18), paris(2026, 10, 6, 19))
    assert cal.is_jarvis_made(stored), "a patch keeps the marker"


def test_delete_event_raw_is_deleted_then_gone(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, eid, block())
    assert cal.delete_event_raw(svc, eid) == "deleted"
    assert cal.delete_event_raw(svc, eid) == "gone"
    assert cal.delete_event_raw(svc, "never0") == "gone"


def test_event_span_is_none_for_all_day_events(svc):
    ev = {"id": "x", "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}}
    assert cal.event_span(ev) is None
    assert cal.event_span({"start": {}, "end": {}}) is None


# --- the busy time ------------------------------------------------------------------------

def test_busy_windows_skip_what_does_not_occupy_his_time(svc):
    svc.seed("Real meeting", paris(2026, 10, 5, 10), paris(2026, 10, 5, 11))
    svc.seed("Reminder", paris(2026, 10, 5, 12), paris(2026, 10, 5, 13), transparency="transparent")
    svc.seed("Declined", paris(2026, 10, 5, 14), paris(2026, 10, 5, 15),
             attendees=[{"self": True, "responseStatus": "declined"}])
    svc.seed("Accepted", paris(2026, 10, 5, 16), paris(2026, 10, 5, 17),
             attendees=[{"self": True, "responseStatus": "accepted"}])
    cancelled = svc.seed("Cancelled", paris(2026, 10, 5, 18), paris(2026, 10, 5, 19))
    svc.items[cancelled["id"]]["status"] = "cancelled"
    events = list(svc.items.values())
    labels = sorted(w[2] for w in cal.busy_windows(events))
    assert labels == ["Accepted", "Real meeting"]


def test_a_busy_all_day_event_covers_its_days_and_a_free_one_does_not():
    busy_day = {"id": "a", "summary": "Conference", "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-08"}}
    free_day = {"id": "b", "summary": "Birthday", "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"},
                "transparency": "transparent"}
    (s, e, label, _id), = cal.busy_windows([busy_day, free_day])
    assert (label, s, e) == ("Conference", paris(2026, 10, 6, 0), paris(2026, 10, 8, 0))


def test_an_unreadable_event_is_skipped_not_fatal():
    assert cal.busy_windows([{"id": "z", "start": {"dateTime": "garbage"}, "end": {"dateTime": "garbage"}}]) == []


def test_fetch_marked_returns_only_jarvis_events_and_the_deleted_ones_too(svc):
    mine = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, mine, block())
    gone = cal.event_id_for("2026-10-05", "b2", 0)
    cal.write_event(svc, gone, block("b2", paris(2026, 10, 6, 9)))
    cal.delete_event_raw(svc, gone)
    svc.seed("His", paris(2026, 10, 5, 10), paris(2026, 10, 5, 11))
    events, truncated = cal.fetch_marked(svc, paris(2026, 10, 5, 0), paris(2026, 10, 12, 0))
    assert not truncated
    assert {(e["id"], e["status"]) for e in events} == {(mine, "confirmed"), (gone, "cancelled")}


def test_fetch_events_pages_through_a_busy_week(svc):
    svc.page_limit = 3
    for i in range(8):
        svc.seed(f"m{i}", paris(2026, 10, 5, 8 + i), paris(2026, 10, 5, 9 + i))
    events, truncated = cal.fetch_events(svc, paris(2026, 10, 5, 0), paris(2026, 10, 6, 0))
    assert len(events) == 8 and not truncated


# --- the update_event / delete_event actions ------------------------------------------------

def _seed_meeting(svc):
    return svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))["id"]


def test_update_event_moves_the_start_and_keeps_the_length(svc):
    eid = _seed_meeting(svc)
    out = act(action="update_event", event_id=eid, start_time="2026-10-07T16:30")
    assert out.startswith("Event updated.") and "16:30-17:30" in out
    assert cal.event_span(svc.items[eid]) == (paris(2026, 10, 7, 16, 30), paris(2026, 10, 7, 17, 30))


def test_update_event_can_set_both_ends_and_the_title(svc):
    eid = _seed_meeting(svc)
    out = act(action="update_event", event_id=eid, title="Dentist (moved)", start_time="2026-10-08T09:00",
              end_time="2026-10-08T10:15")
    assert "09:00-10:15" in out and "Dentist (moved)" in out
    assert svc.items[eid]["summary"] == "Dentist (moved)"


def test_update_event_leaves_the_marker_of_an_event_jarvis_made(svc):
    eid = cal.event_id_for("2026-10-05", "b1", 0)
    cal.write_event(svc, eid, block())
    act(action="update_event", event_id=eid, start_time="2026-10-05T10:00")
    assert cal.is_jarvis_made(svc.items[eid])


@pytest.mark.parametrize("kw,fragment", [
    ({"event_id": ""}, "event_id is required"),
    ({"event_id": "nothing0", "title": "x"}, "no event with id nothing0"),
    ({"event_id": "SEED", }, "nothing to change"),
    ({"event_id": "SEED", "start_time": "tomorrow-ish"}, "ISO 8601"),
    ({"event_id": "SEED", "start_time": "2026-10-07T17:00", "end_time": "2026-10-07T16:00"}, "end after it starts"),
])
def test_update_event_refuses_what_it_cannot_do(svc, kw, fragment):
    eid = _seed_meeting(svc)
    kw = {k: (eid if v == "SEED" else v) for k, v in kw.items()}
    out = act(action="update_event", **kw)
    assert out.startswith("Error:") and fragment in out
    assert cal.event_span(svc.items[eid]) == (paris(2026, 10, 7, 15), paris(2026, 10, 7, 16)), "nothing was changed"


def test_an_all_day_event_cannot_be_moved_by_clock_time(svc):
    svc.items["allday"] = {"id": "allday", "status": "confirmed", "summary": "Trip",
                           "start": {"date": "2026-10-07"}, "end": {"date": "2026-10-08"}}
    out = act(action="update_event", event_id="allday", start_time="2026-10-07T10:00")
    assert out.startswith("Error:") and "no clock time" in out


def test_update_event_reports_when_google_stores_a_different_time(svc, monkeypatch):
    eid = _seed_meeting(svc)
    real = svc.events

    class Drifting:
        def __getattr__(self, name):
            return getattr(real(), name)

        def patch(self, **kw):
            req = real().patch(**kw)
            return type("R", (), {"execute": staticmethod(lambda: dict(req.execute(), start={"dateTime": "2026-10-07T20:00:00+02:00"}))})()

    monkeypatch.setattr(svc, "events", lambda: Drifting())
    assert "Google stored it at" in act(action="update_event", event_id=eid, start_time="2026-10-07T16:00")


def test_delete_event_deletes_and_says_what_went(svc):
    eid = _seed_meeting(svc)
    out = act(action="delete_event", event_id=eid)
    assert out.startswith("Event deleted:") and "Dentist" in out
    assert svc.items[eid]["status"] == "cancelled"
    again = act(action="delete_event", event_id=eid)
    assert again.startswith("Error:") and "may already have been deleted" in again
    assert act(action="delete_event").startswith("Error:")


def test_the_new_actions_are_classified_as_failures_when_they_fail(svc):
    from backend.tools import outcomes
    out = act(action="delete_event", event_id="nothing0")
    assert outcomes.classify("calendar_action", out) == outcomes.FAILED
    assert outcomes.classify("calendar_action", act(action="delete_event", event_id=_seed_meeting(svc))) == outcomes.OK


def test_the_module_still_never_asks_the_machine_for_the_time_or_imports_backend():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(cal))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("backend"), node.module
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("now", "today", "utcnow"):
                owner = ast.unparse(node.func.value)
                assert owner in ("datetime", "datetime.datetime") and node.args, "now() must be given a zone"


def test_update_event_keeps_a_length_that_is_not_an_hour(svc):
    eid = svc.seed("Standup", paris(2026, 10, 5, 10), paris(2026, 10, 5, 10, 45))["id"]
    out = act(action="update_event", event_id=eid, start_time="2026-10-05T14:00")
    assert out.startswith("Event updated.") and "14:00-14:45" in out
    assert cal.event_span(svc.items[eid]) == (paris(2026, 10, 5, 14), paris(2026, 10, 5, 14, 45))


def test_a_failed_owner_lookup_is_unknown_never_jarvis(svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar unreachable")
    monkeypatch.setattr(cal, "event_owner", boom)
    assert cal._owner_sync("anything") == "unknown"
    assert run(cal.owner_of("anything")) == "unknown"
