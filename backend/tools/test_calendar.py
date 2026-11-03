"""The calendar tool reads and writes local time explicitly (P0.3 of the control roadmap).

The bug these pin: on the UTC server, a time without an offset was given the server's
offset. "Gym at 18:00" was stored as 20:00 Paris, and JARVIS confirmed and listed it as
18:00 -- consistent with itself and wrong on Sam's phone. A listing also showed bare
`HH:MM` (a week of it cannot tell Monday from Thursday), carried no event ids, and stopped
silently at 20 events.

The Google client is the in-memory double from `calendar_fake.py`, which returns times in
Paris the way the real API does.
"""
import ast
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.tools import calendar as cal
from backend.tools import outcomes
from backend.tools.calendar_fake import LOCAL_TZ, FakeCalendarService, FakeHttpError, instant

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    # 00:30 Paris on 30 September is 22:30 UTC the day before: "today" must be the Paris day.
    monkeypatch.setattr(cal, "_now", lambda: datetime(2026, 9, 30, 0, 30, tzinfo=LOCAL_TZ))
    return fake


@pytest.fixture
def sent(svc):
    """Every body the tool actually sends to Google's insert, in order."""
    bodies = []
    real = svc.events

    class Spy:
        def __init__(self, inner):
            self._inner = inner

        def insert(self, **kw):
            bodies.append(kw["body"])
            return self._inner.insert(**kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    svc.events = lambda: Spy(real())
    return bodies


def run(**kw):
    return asyncio.run(cal.calendar_action(**kw))


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=LOCAL_TZ)


# ---------------------------------------------------------------------------
# Creating: a time means local time
# ---------------------------------------------------------------------------

def test_a_time_without_an_offset_is_paris_time_in_summer_and_in_winter(svc):
    run(action="create_event", title="Gym", start_time="2026-10-05T18:00", duration_minutes=60)
    run(action="create_event", title="Gym", start_time="2026-11-05T18:00", duration_minutes=60)
    summer, winter = svc.live()
    assert summer["start"]["dateTime"] == "2026-10-05T18:00:00+02:00"
    assert winter["start"]["dateTime"] == "2026-11-05T18:00:00+01:00"


def test_the_request_names_the_time_zone_and_carries_the_paris_offset(svc, sent):
    run(action="create_event", title="Gym", start_time="2026-10-05T18:00", duration_minutes=60)
    body = sent[0]
    assert body["start"] == {"dateTime": "2026-10-05T18:00:00+02:00", "timeZone": "Europe/Paris"}
    assert body["end"] == {"dateTime": "2026-10-05T19:00:00+02:00", "timeZone": "Europe/Paris"}


def test_an_explicit_offset_keeps_its_instant(svc):
    out = run(action="create_event", title="Call", start_time="2026-10-05T16:00:00+00:00",
              duration_minutes=30)
    assert "18:00-18:30 (local time)" in out
    assert svc.live()[0]["start"]["dateTime"] == "2026-10-05T18:00:00+02:00"


def test_a_z_suffix_is_utc(svc):
    run(action="create_event", title="Call", start_time="2026-10-05T16:00:00Z", duration_minutes=30)
    assert svc.live()[0]["start"]["dateTime"] == "2026-10-05T18:00:00+02:00"


def test_a_wall_time_that_does_not_exist_moves_to_the_real_instant(svc, sent):
    """02:30 on 28 March 2027 never happens in Paris (02:00 jumps to 03:00). What is SENT
    must already be the real time: the fake normalises whatever it receives, so checking
    only what it stored would pass even if a fictional 02:30+01:00 went out."""
    run(action="create_event", title="Odd", start_time="2027-03-28T02:30", duration_minutes=30)
    assert sent[0]["start"]["dateTime"] == "2027-03-28T03:30:00+02:00"
    assert svc.live()[0]["start"]["dateTime"] == "2027-03-28T03:30:00+02:00"


def test_a_duration_is_real_elapsed_time_across_a_clock_change(svc):
    """The clocks go back at 03:00 on 25 October 2026: 01:30 plus two hours is 02:30 in the
    new offset, not 03:30."""
    run(action="create_event", title="Night", start_time="2026-10-25T01:30", duration_minutes=120)
    ev = svc.live()[0]
    start, end = instant(ev["start"]), instant(ev["end"])
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=2)
    assert ev["end"]["dateTime"] == "2026-10-25T02:30:00+01:00"


def test_the_confirmation_reports_what_google_stored(svc):
    out = run(action="create_event", title="Gym", start_time="2026-10-05T18:00", duration_minutes=60)
    assert out.startswith("Event created: 'Gym', Mon 2026-10-05 18:00-19:00 (local time)")
    assert f"[id {svc.live()[0]['id']}]" in out


def test_a_stored_time_that_differs_from_the_request_is_reported_not_confirmed(svc):
    """The old code confirmed the time it had SENT. If Google holds something else, say so."""
    real = svc.events

    class Shifting:
        def __init__(self, inner):
            self._inner = inner

        def insert(self, **kw):
            body = dict(kw["body"])
            shift = timedelta(hours=2)
            for key in ("start", "end"):
                moved = instant(body[key]) + shift
                body[key] = {"dateTime": moved.isoformat()}
            return self._inner.insert(**{**kw, "body": body})

        def __getattr__(self, name):
            return getattr(self._inner, name)

    svc.events = lambda: Shifting(real())
    out = run(action="create_event", title="Gym", start_time="2026-10-05T18:00", duration_minutes=60)
    assert out.startswith("Calendar error:")
    assert "20:00" in out and "18:00" in out and "id " in out
    assert outcomes.classify("calendar_action", out) == outcomes.FAILED


def test_every_created_event_carries_the_jarvis_marker(svc):
    run(action="create_event", title="Gym", start_time="2026-10-05T18:00", duration_minutes=60)
    assert svc.live()[0]["extendedProperties"]["private"] == {"jarvis": "1"}


def test_create_input_errors_are_errors_and_write_nothing(svc):
    for kw in (
        dict(title="", start_time="2026-10-05T18:00"),
        dict(title="Gym", start_time=""),
        dict(title="Gym", start_time="next tuesday"),
        dict(title="Gym", start_time="2026-10-05T18:00", end_time="2026-10-05T17:00"),
        dict(title="Gym", start_time="2026-10-05T18:00", duration_minutes=0),
        dict(title="Gym", start_time="2026-10-05T18:00", end_time="garbage"),
    ):
        out = run(action="create_event", **kw)
        assert out.startswith("Error:"), (kw, out)
        assert outcomes.classify("calendar_action", out) == outcomes.FAILED
    assert svc.live() == []


def test_description_and_location_are_kept(svc):
    run(action="create_event", title="Gym", start_time="2026-10-05T18:00",
        description="legs", location="Basic-Fit")
    ev = svc.live()[0]
    assert ev["description"] == "legs" and ev["location"] == "Basic-Fit"


# ---------------------------------------------------------------------------
# Listing: the day, the id, every event
# ---------------------------------------------------------------------------

def test_a_week_names_the_day_of_every_event_and_the_same_time_on_two_days_is_clear(svc):
    """The finding: bare HH:MM cannot tell Monday's meeting from Thursday's."""
    svc.seed("Standup", at(5, 9), at(5, 9, 30), event_id="mon00001")
    svc.seed("Standup", at(8, 9), at(8, 9, 30), event_id="thu00001")
    out = run(action="list_events", date="2026-10-05", days=7)
    lines = out.splitlines()
    assert lines[0].startswith("Events from Mon 2026-10-05 to Sun 2026-10-11 (local time)")
    assert lines.index("Mon 2026-10-05") < lines.index("Thu 2026-10-08")
    mon = lines.index("Mon 2026-10-05")
    assert lines[mon + 1] == "  09:00-09:30: Standup  [id mon00001]"
    assert "  09:00-09:30: Standup  [id thu00001]" in lines


def test_every_listed_event_shows_its_id_and_jarvis_made_ones_are_marked(svc):
    svc.seed("His meeting", at(5, 9), at(5, 10), event_id="his00001")
    run(action="create_event", title="Gym", start_time="2026-10-05T18:00", duration_minutes=60)
    out = run(action="list_events", date="2026-10-05")
    assert "His meeting  [id his00001]" in out
    assert "His meeting [JARVIS-made]" not in out
    gym = next(line for line in out.splitlines() if "Gym" in line)
    assert "[JARVIS-made]" in gym and "[id " in gym


def test_a_listing_pages_through_the_api_instead_of_stopping_at_twenty(svc):
    """The old request asked for 20 and stopped, with no sign that more existed."""
    svc.page_limit = 25
    for i in range(60):
        svc.seed(f"e{i:02d}", at(5, 6) + timedelta(minutes=10 * i), at(5, 6) + timedelta(minutes=10 * i + 5))
    out = run(action="list_events", date="2026-10-05")
    assert out.count("[id ") == 60
    assert "60 in all" in out and "Only the first" not in out
    assert sum(1 for c in svc.calls if c[0] == "list") == 3            # it really paged


def test_a_listing_that_hits_the_cap_says_so(svc):
    for i in range(30):
        svc.seed(f"e{i:02d}", at(5, 6) + timedelta(minutes=10 * i), at(5, 6) + timedelta(minutes=10 * i + 5))
    out = run(action="list_events", date="2026-10-05", max_results=10)
    assert out.count("[id ") == 10
    assert "10+ in all" in out and "Only the first 10 are shown" in out


def test_exactly_the_cap_with_nothing_more_is_not_reported_as_cut_off(svc):
    for i in range(10):
        svc.seed(f"e{i}", at(5, 6 + i), at(5, 6 + i, 30))
    out = run(action="list_events", date="2026-10-05", max_results=10)
    assert out.count("[id ") == 10 and "Only the first" not in out


def test_overnight_and_all_day_events_read_clearly(svc):
    svc.seed("Night shift", at(5, 22), at(6, 6), event_id="night001")
    svc.items["hol001"] = {"id": "hol001", "status": "confirmed", "summary": "Holiday",
                           "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-08"}}
    out = run(action="list_events", date="2026-10-05")
    assert "22:00-06:00 (+1d): Night shift" in out
    assert "all day until Wed 2026-10-07: Holiday" in out


def test_today_is_the_paris_day_even_when_utc_is_still_yesterday(svc):
    """00:30 Paris on 30 September is 22:30 UTC on the 29th."""
    svc.seed("Early", at(1, 8), at(1, 9))
    svc.seed("Today", datetime(2026, 9, 30, 8, tzinfo=LOCAL_TZ), datetime(2026, 9, 30, 9, tzinfo=LOCAL_TZ))
    out = run(action="list_events")
    assert "Wed 2026-09-30" in out and "Today" in out and "Early" not in out
    tomorrow = run(action="list_events", date="tomorrow")
    assert "Thu 2026-10-01" in tomorrow and "Early" in tomorrow


def test_an_empty_range_says_which_range(svc):
    assert run(action="list_events", date="2026-10-05") == "No events on Mon 2026-10-05 (local time)."
    assert run(action="list_events", date="2026-10-05", days=3) == \
        "No events from Mon 2026-10-05 to Wed 2026-10-07 (local time)."


def test_an_unreadable_date_is_an_error_not_today(svc):
    svc.seed("Today", datetime(2026, 9, 30, 8, tzinfo=LOCAL_TZ), datetime(2026, 9, 30, 9, tzinfo=LOCAL_TZ))
    out = run(action="list_events", date="next tuesday")
    assert out.startswith("Error:") and "next tuesday" in out
    assert "Today" not in out


def test_days_are_clamped_and_bad_numbers_are_errors(svc):
    out = run(action="list_events", date="2026-10-05", days=5000)
    assert "to Sat 2027-01-02" in out                                    # 90 days, not 5000
    assert run(action="list_events", date="2026-10-05", days="many").startswith("Error:")


def test_a_listing_is_not_classified_as_a_failure(svc):
    svc.seed("Standup", at(5, 9), at(5, 10))
    for out in (run(action="list_events", date="2026-10-05"),
                run(action="list_events", date="2026-10-06"),
                run(action="check_free_busy", start_time="2026-10-05T12:00", end_time="2026-10-05T13:00")):
        assert outcomes.classify("calendar_action", out) == outcomes.OK, out


# ---------------------------------------------------------------------------
# Free / busy
# ---------------------------------------------------------------------------

def test_busy_windows_are_shown_in_paris_with_their_day(svc):
    svc.seed("Meeting", at(5, 9), at(5, 10))
    out = run(action="check_free_busy", start_time="2026-10-05T00:00", end_time="2026-10-06T00:00")
    assert out == "Busy windows (local time):\n  Mon 2026-10-05 09:00-10:00"


def test_free_message_keeps_the_phrase_the_classifier_relies_on(svc):
    out = run(action="check_free_busy", start_time="2026-10-05T12:00", end_time="2026-10-05T13:00")
    assert out == "You are free on Mon 2026-10-05 between 12:00 and 13:00 (local time)."
    span = run(action="check_free_busy", start_time="2026-10-05T12:00", end_time="2026-10-06T13:00")
    assert span.startswith("You are free between Mon 2026-10-05 12:00 and Tue 2026-10-06 13:00")


def test_free_busy_input_errors(svc):
    assert run(action="check_free_busy", start_time="2026-10-05T12:00").startswith("Error:")
    assert run(action="check_free_busy", start_time="x", end_time="y").startswith("Error:")
    assert run(action="check_free_busy", start_time="2026-10-05T13:00",
               end_time="2026-10-05T12:00").startswith("Error:")


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_an_api_failure_is_a_calendar_error_and_a_failed_outcome(svc):
    svc.fail("insert", FakeHttpError(500, "backend error"))
    out = run(action="create_event", title="Gym", start_time="2026-10-05T18:00")
    assert out.startswith("Calendar error:")
    assert outcomes.classify("calendar_action", out) == outcomes.FAILED


def test_auth_failure_and_unknown_action_keep_their_classified_prefixes(monkeypatch, svc):
    assert run(action="delete_everything") == "Unknown action: delete_everything"
    monkeypatch.setattr(cal, "_get_service", lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    out = run(action="list_events")
    assert out == "Calendar auth failed: no token"
    assert outcomes.classify("calendar_action", out) == outcomes.FAILED


# ---------------------------------------------------------------------------
# The module must not read the machine's clock
# ---------------------------------------------------------------------------

def test_calendar_never_asks_the_machine_for_the_time():
    """The whole bug was `.astimezone()` and `datetime.now()` with no zone. Structural: any
    reintroduction fails here even on a Paris-time desk where the behaviour would look right."""
    tree = ast.parse((ROOT / "backend" / "tools" / "calendar.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr == "astimezone" and not node.args and not node.keywords:
            offenders.append(f"line {node.lineno}: .astimezone() with no zone")
        if attr in ("now", "today", "utcnow", "fromtimestamp") and not node.args and not node.keywords:
            offenders.append(f"line {node.lineno}: .{attr}() reads the machine clock")
    assert offenders == []


def test_the_module_imports_nothing_from_backend():
    """It is loaded standalone by the Linux probe, and stays free of the rest of the app."""
    tree = ast.parse((ROOT / "backend" / "tools" / "calendar.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend"):
            pytest.fail(f"imports {node.module}")




_UTC_PROBE = """
import asyncio, datetime, importlib.util, sys

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

fake = load("calfake", {fake_path!r})
cal = load("jcal", {cal_path!r})
svc = fake.FakeCalendarService()
cal._get_service = lambda: svc
print(int(datetime.datetime.now().astimezone().utcoffset().total_seconds()))
asyncio.run(cal.calendar_action(action="create_event", title="Gym",
                                start_time="2026-10-05T18:00", duration_minutes=60))
print(svc.live()[0]["start"]["dateTime"])
print(asyncio.run(cal.calendar_action(action="list_events", date="2026-10-05")))
"""


def test_a_utc_process_stores_and_lists_paris_time():
    """The 30 September finding, end to end: the module is loaded by path in a process whose
    clock is UTC and which never imports `backend` (so nothing sets local time for it).

    `TZ=UTC0` is a POSIX zone string that both Linux and the Windows C runtime honour, so
    this runs on the Paris-time desk as well as on the server -- without it, a regression
    to the machine's clock would pass on the one machine where the clock happens to be right.
    """
    code = _UTC_PROBE.format(fake_path=str(ROOT / "backend" / "tools" / "calendar_fake.py"),
                             cal_path=str(ROOT / "backend" / "tools" / "calendar.py"))
    env = dict(os.environ, TZ="UTC0")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=60, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "0", "the probe process was meant to run on a UTC clock"
    assert lines[1] == "2026-10-05T18:00:00+02:00"
    assert "18:00-19:00: Gym" in out.stdout
