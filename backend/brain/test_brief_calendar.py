"""What the morning brief reads from the calendar (P0.3 acceptance: the brief's calendar
lines match Google Calendar).

`_collect_calendar` treated only text starting with "error" as a failure. The calendar
tool's own failures are "Calendar error: ..." and "Calendar auth failed: ...", so on a
bad token or an API error the brief was composed from that sentence as if it were the
day's agenda. It also cut the listing at 800 characters, which now would drop events from
a busy day once each line carries an id.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from backend.brain import chronos
from backend.tools import calendar as cal
from backend.tools.calendar_fake import LOCAL_TZ, FakeCalendarService


@pytest.fixture
def token(monkeypatch, tmp_path):
    """The brief only touches the calendar when a token already exists."""
    path = tmp_path / "token_calendar.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cal, "TOKEN_PATH", path)
    return path


@pytest.fixture
def svc(monkeypatch, token):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    monkeypatch.setattr(cal, "_now", lambda: datetime(2026, 9, 30, 7, 0, tzinfo=LOCAL_TZ))
    return fake


def collect():
    return asyncio.run(chronos._collect_calendar())


def test_calendar_failures_do_not_reach_the_brief(monkeypatch, token):
    for message in ("Calendar error: 503 backend error", "Calendar auth failed: token expired",
                    "Error: something"):
        async def fake_action(**kw):
            return message
        monkeypatch.setattr(cal, "calendar_action", fake_action)
        assert collect() == "", message


def test_the_brief_lists_todays_events_in_paris_without_ids(svc):
    day = datetime(2026, 9, 30, tzinfo=LOCAL_TZ)
    svc.seed("Standup", day + timedelta(hours=9), day + timedelta(hours=9, minutes=30))
    svc.seed("Dentist", day + timedelta(hours=17, minutes=30), day + timedelta(hours=18, minutes=15),
             location="Rue Lafayette")
    svc.seed("Tomorrow's thing", day + timedelta(days=1, hours=10), day + timedelta(days=1, hours=11))
    text = collect()
    assert "Standup" in text and "Dentist @ Rue Lafayette" in text
    assert "09:00-09:30" in text and "17:30-18:15" in text
    assert "Tomorrow's thing" not in text
    assert "[id " not in text, "ids are for the tools, not for a morning message"


def test_a_busy_day_is_not_cut_off_mid_list(svc):
    """25 events with ids used to overflow the 800-character slice and lose the evening."""
    day = datetime(2026, 9, 30, tzinfo=LOCAL_TZ)
    for i in range(25):
        start = day + timedelta(hours=6, minutes=30 * i)
        svc.seed(f"Meeting number {i:02d}", start, start + timedelta(minutes=20))
    text = collect()
    assert "Meeting number 00" in text and "Meeting number 24" in text


def test_no_token_means_the_calendar_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(cal, "TOKEN_PATH", tmp_path / "nope.json")

    async def boom(**kw):
        raise AssertionError("must not open the OAuth flow from the scheduler")
    monkeypatch.setattr(cal, "calendar_action", boom)
    assert collect() == ""


def test_a_very_long_day_is_cut_between_events_with_a_count():
    lines = ["Events on Wed 2026-09-30 (local time), 200 in all:", "Wed 2026-09-30"]
    lines += [f"  {6 + i // 60:02d}:{i % 60:02d}-{6 + i // 60:02d}:{i % 60:02d}: Meeting number {i:03d} [id x{i:04d}]"
              for i in range(200)]
    out = chronos._tidy_calendar("\n".join(lines))
    assert len(out) <= chronos._CALENDAR_BRIEF_CHARS + 60
    body, note = out.rsplit("\n", 1)
    assert note.startswith("(+") and note.endswith("more lines not shown)")
    import re
    assert re.fullmatch(r"  \d\d:\d\d-\d\d:\d\d: Meeting number \d{3}", body.splitlines()[-1]), \
        "the cut must fall between events, never inside one"
    kept = len(body.splitlines())
    assert int(note[2:note.index(" ")]) == len(lines) - kept        # the count is exact
    assert "[id " not in out


def test_a_short_listing_only_loses_its_ids():
    text = "Events on Wed 2026-09-30 (local time), 1 in all:\nWed 2026-09-30\n  09:00-10:00: Standup [JARVIS-made]  [id abc123]"
    assert chronos._tidy_calendar(text) == (
        "Events on Wed 2026-09-30 (local time), 1 in all:\nWed 2026-09-30\n  09:00-10:00: Standup [JARVIS-made]")
