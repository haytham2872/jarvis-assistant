"""The Google Calendar test double has to behave like the real API where it matters.

Every later calendar test (editing, the week planner, retries) is only as honest as this
fake, so its own behaviour is pinned here: the Paris read-back that exposed the UTC bug,
paging, caller-chosen ids, cancellation, patching and injected failures.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.tools.calendar_fake import LOCAL_TZ, FakeCalendarService, FakeHttpError, instant


def _ev(summary="Gym", start="2026-10-05T18:00:00+02:00", end="2026-10-05T19:00:00+02:00", **extra):
    return {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


def test_times_read_back_in_paris_whatever_offset_they_were_sent_with():
    """The behaviour that made the UTC bug visible: 18:00 sent as UTC is 20:00 in Paris."""
    svc = FakeCalendarService()
    got = svc.events().insert(body=_ev(start="2026-10-05T18:00:00+00:00",
                                       end="2026-10-05T19:00:00+00:00")).execute()
    assert got["start"]["dateTime"] == "2026-10-05T20:00:00+02:00"
    assert got["start"]["timeZone"] == "Europe/Paris"


def test_lists_are_paged_sorted_and_carry_a_token():
    svc = FakeCalendarService(page_limit=3)
    base = datetime(2026, 10, 5, 8, tzinfo=LOCAL_TZ)
    for i in reversed(range(7)):                                  # inserted out of order
        svc.seed(f"e{i}", base + timedelta(hours=i), base + timedelta(hours=i, minutes=30))
    first = svc.events().list(timeMin=base.isoformat(),
                              timeMax=(base + timedelta(days=1)).isoformat(),
                              maxResults=250).execute()
    assert [e["summary"] for e in first["items"]] == ["e0", "e1", "e2"]
    assert first["nextPageToken"] == "3"
    last = svc.events().list(timeMin=base.isoformat(), timeMax=(base + timedelta(days=1)).isoformat(),
                             maxResults=250, pageToken="6").execute()
    assert [e["summary"] for e in last["items"]] == ["e6"] and "nextPageToken" not in last


def test_only_overlapping_events_are_listed():
    svc = FakeCalendarService()
    svc.seed("before", datetime(2026, 10, 4, 9, tzinfo=LOCAL_TZ), datetime(2026, 10, 4, 10, tzinfo=LOCAL_TZ))
    svc.seed("during", datetime(2026, 10, 5, 9, tzinfo=LOCAL_TZ), datetime(2026, 10, 5, 10, tzinfo=LOCAL_TZ))
    svc.seed("straddles", datetime(2026, 10, 4, 23, tzinfo=LOCAL_TZ), datetime(2026, 10, 5, 1, tzinfo=LOCAL_TZ))
    out = svc.events().list(timeMin="2026-10-05T00:00:00+02:00", timeMax="2026-10-06T00:00:00+02:00").execute()
    assert [e["summary"] for e in out["items"]] == ["straddles", "during"]


def test_caller_chosen_id_is_kept_and_a_duplicate_is_a_conflict():
    """What makes a retried plan write a no-op instead of a duplicate."""
    svc = FakeCalendarService()
    got = svc.events().insert(body={**_ev(), "id": "jv0abc123"}).execute()
    assert got["id"] == "jv0abc123"
    with pytest.raises(FakeHttpError) as exc:
        svc.events().insert(body={**_ev(), "id": "jv0abc123"}).execute()
    assert exc.value.resp.status == 409
    assert len(svc.live()) == 1


def test_invalid_ids_are_rejected_like_google_does():
    svc = FakeCalendarService()
    for bad in ("SHORT", "has-dash-1", "UPPERCASE1", "ab"):
        with pytest.raises(FakeHttpError) as exc:
            svc.events().insert(body={**_ev(), "id": bad}).execute()
        assert exc.value.resp.status == 400, bad


def test_delete_cancels_hides_and_a_second_delete_is_gone():
    svc = FakeCalendarService()
    ev = svc.events().insert(body=_ev()).execute()
    svc.events().delete(eventId=ev["id"]).execute()
    assert svc.live() == []
    assert svc.events().list().execute()["items"] == []
    with pytest.raises(FakeHttpError) as exc:
        svc.events().delete(eventId=ev["id"]).execute()
    assert exc.value.resp.status == 410


def test_patch_changes_times_merges_private_properties_and_keeps_the_rest():
    svc = FakeCalendarService()
    ev = svc.events().insert(body=_ev(extendedProperties={"private": {"jarvis": "1", "plan": "p1"}},
                                      location="Gym")).execute()
    got = svc.events().patch(eventId=ev["id"], body={
        "start": {"dateTime": "2026-10-05T20:00:00+02:00"},
        "end": {"dateTime": "2026-10-05T21:00:00+02:00"},
        "extendedProperties": {"private": {"block": "b7"}},
    }).execute()
    assert got["start"]["dateTime"] == "2026-10-05T20:00:00+02:00"
    assert got["location"] == "Gym" and got["summary"] == "Gym"
    assert got["extendedProperties"]["private"] == {"jarvis": "1", "plan": "p1", "block": "b7"}


def test_patch_cannot_make_an_event_end_before_it_starts():
    svc = FakeCalendarService()
    ev = svc.events().insert(body=_ev()).execute()
    with pytest.raises(FakeHttpError):
        svc.events().patch(eventId=ev["id"], body={"end": {"dateTime": "2026-10-05T17:00:00+02:00"}}).execute()


def test_missing_events_are_404():
    svc = FakeCalendarService()
    for call in (lambda: svc.events().get(eventId="nope1").execute(),
                 lambda: svc.events().patch(eventId="nope1", body={}).execute(),
                 lambda: svc.events().delete(eventId="nope1").execute()):
        with pytest.raises(FakeHttpError) as exc:
            call()
        assert exc.value.resp.status == 404


def test_private_property_filter_finds_only_marked_events():
    svc = FakeCalendarService()
    svc.seed("his", datetime(2026, 10, 5, 9, tzinfo=LOCAL_TZ), datetime(2026, 10, 5, 10, tzinfo=LOCAL_TZ))
    svc.events().insert(body=_ev("mine", extendedProperties={"private": {"jarvis": "1"}})).execute()
    out = svc.events().list(privateExtendedProperty="jarvis=1").execute()
    assert [e["summary"] for e in out["items"]] == ["mine"]


def test_injected_failure_hits_the_next_calls_only_then_clears():
    svc = FakeCalendarService()
    svc.fail("insert", FakeHttpError(500, "backend error"), times=2)
    for _ in range(2):
        with pytest.raises(FakeHttpError):
            svc.events().insert(body=_ev()).execute()
    assert svc.events().insert(body=_ev()).execute()["summary"] == "Gym"
    assert len(svc.live()) == 1


def test_freebusy_returns_utc_windows_and_skips_transparent_events():
    svc = FakeCalendarService()
    svc.seed("meeting", datetime(2026, 10, 5, 9, tzinfo=LOCAL_TZ), datetime(2026, 10, 5, 10, tzinfo=LOCAL_TZ))
    svc.seed("fyi", datetime(2026, 10, 5, 12, tzinfo=LOCAL_TZ), datetime(2026, 10, 5, 13, tzinfo=LOCAL_TZ),
             transparency="transparent")
    out = svc.freebusy().query(body={"timeMin": "2026-10-05T00:00:00+02:00",
                                     "timeMax": "2026-10-06T00:00:00+02:00",
                                     "items": [{"id": "primary"}]}).execute()
    assert out["calendars"]["primary"]["busy"] == [
        {"start": "2026-10-05T07:00:00Z", "end": "2026-10-05T08:00:00Z"}]


def test_all_day_events_span_paris_midnights():
    svc = FakeCalendarService()
    svc.items["ad001"] = {"id": "ad001", "status": "confirmed", "summary": "Holiday",
                          "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}}
    assert instant(svc.items["ad001"]["start"]) == datetime(2026, 10, 5, tzinfo=LOCAL_TZ)
    out = svc.events().list(timeMin="2026-10-05T12:00:00+02:00", timeMax="2026-10-05T13:00:00+02:00").execute()
    assert [e["summary"] for e in out["items"]] == ["Holiday"]
    assert instant(svc.items["ad001"]["end"]).astimezone(timezone.utc).hour == 22
