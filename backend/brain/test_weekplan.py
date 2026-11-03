"""P3: the `week_plan` tool (backend/brain/weekplan.py) against the calendar test double.

The acceptance lines from the roadmap: apply, change, undo and a failed-then-retried apply leave
exactly the right events and no duplicates; a block Sam moved or deleted himself is noticed and
never overwritten; nothing that was not made by the planner is ever touched. The last group runs
random sequences of operations and checks those invariants on whatever comes out.
"""
import asyncio
import os
import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brain import planner as P
from backend.brain import weekplan as W
from backend.storage import plans
from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService, FakeHttpError

LOCAL_TZ = ZoneInfo("Europe/Paris")
WEEK = "2026-10-05"                         # a Monday, no clock change
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)          # Thursday 10:00 Paris, the week before


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


def at(day_offset, hhmm, week=WEEK):
    return P.at(date.fromisoformat(week) + timedelta(days=day_offset), hhmm)


WORK = {"days": "mon-fri", "start": "09:00", "end": "17:00"}
RULES = {"work": WORK, "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}], "buffer": 15}
GYM4 = {"blocks": [{"kind": "gym", "count": 4, "minutes": 60, "not_after": "20:00"}]}


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    return fake


def call(action, now=NOW, **kw):
    return W.run(W.Ask(action=action, **kw), now)


def mine(svc):
    """What JARVIS has in the calendar: (title, start, end), in time order."""
    out = []
    for ev in svc.live():
        if cal.is_jarvis_made(ev):
            s, e = cal.event_span(ev)
            out.append((ev["summary"].lower(), s.astimezone(timezone.utc), e.astimezone(timezone.utc)))
    return sorted(out, key=lambda t: (t[1], t[0]))


def standing(week=WEEK):
    """What the applied plan says should be in the calendar."""
    plan = plans.applied(week)
    out = []
    for r in plan["blocks"]:
        if r["status"] == plans.DELETED:
            continue
        s, e = W._eff(r)
        out.append((r["kind"], datetime.fromtimestamp(s, timezone.utc), datetime.fromtimestamp(e, timezone.utc)))
    return sorted(out, key=lambda t: (t[1], t[0]))


def kinds(rows):
    return sorted(k for k, *_ in rows)


def written(svc):
    """The calls that changed the calendar, in order."""
    return [c for c in svc.calls if c[0] in ("insert", "patch", "delete")]


def week_with_meetings(svc):
    svc.seed("Standup", paris(2026, 10, 5, 10), paris(2026, 10, 5, 10, 30))
    svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))
    svc.seed("Offsite", paris(2026, 10, 8, 9), paris(2026, 10, 8, 17))


def planned_in_calendar(svc, rules=RULES):
    """Propose and apply the standard week."""
    week_with_meetings(svc)
    assert call("propose", rules=rules).startswith("Proposed.")
    out = call("apply")
    assert out.startswith("Applied plan"), out
    return out


# ===========================================================================================
# propose
# ===========================================================================================

def test_propose_shows_the_week_and_writes_nothing_to_the_calendar(svc):
    week_with_meetings(svc)
    out = call("propose", rules=RULES)
    assert out.startswith("Proposed.\nPlan #1 (version 1) for the week of Mon 2026-10-05, a proposal")
    assert "Mon 2026-10-05\n  09:00-10:00 work\n  10:30-17:00 work" in out
    assert "Offsite" not in out and "Thu 2026-10-08" not in out, "the offsite day has no room for work"
    assert "Kept clear of 3 event(s)" in out and "apply (plan_id=1)" in out
    assert written(svc) == [] and mine(svc) == []
    plan = plans.draft(WEEK)
    assert plan["state"] == plans.DRAFT and plan["created_by"] == "owner" and len(plan["blocks"]) == 6


def test_propose_reports_what_did_not_fit_and_why(svc):
    week_with_meetings(svc)
    out = call("propose", rules={"blocks": [{"kind": "gym", "count": 7, "minutes": 60, "days": "mon-fri"}]})
    assert "Could not place 2 of 7 gym (60 min each)" in out and "Friday: already has gym" in out
    out = call("propose", rules={"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "days": "thu",
                                             "not_before": "09:00", "not_after": "17:00"}]})
    assert "Could not place 1 of 1 gym" in out and "Thursday: no free time between 09:00 and 17:00" in out


def test_propose_defaults_to_next_week_and_reads_this_and_dates(svc):
    call("propose", rules=RULES)
    assert plans.draft(WEEK) is not None                        # NOW is Thursday 1 Oct: next week is the 5th
    call("propose", rules=RULES, week="this")
    assert plans.draft("2026-09-28") is not None
    call("propose", rules=RULES, week="2026-10-14")             # any day in the week
    assert plans.draft("2026-10-12") is not None
    assert call("propose", rules=RULES, week="whenever").startswith("Error: week must be")


@pytest.mark.parametrize("rules,fragment", [
    ({"colour": "blue"}, "unknown field"),
    ({}, "ask for nothing"),
    ({"blocks": [{"kind": "gym", "count": 2, "minutes": "an hour"}]}, "whole number"),
    (None, "must be an object"),
])
def test_rules_that_cannot_be_used_say_what_to_fix_and_store_nothing(svc, rules, fragment):
    out = call("propose", rules=rules)
    assert out.startswith("Error:") and fragment in out
    assert plans.versions(WEEK) == []


def test_without_a_calendar_nothing_is_planned(svc, monkeypatch):
    monkeypatch.setattr(cal, "_get_service", lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    out = call("propose", rules=RULES)
    assert out.startswith("Error: the calendar is not available") and plans.versions(WEEK) == []


def test_a_week_with_too_many_events_is_refused_not_planned_around_partially(svc):
    for i in range(255):
        svc.seed(f"e{i}", paris(2026, 10, 5, 8) + timedelta(minutes=i), paris(2026, 10, 5, 8) + timedelta(minutes=i + 1))
    out = call("propose", rules=RULES)
    assert out.startswith("Error:") and "more than 250 events" in out


def test_a_second_proposal_replaces_the_first(svc):
    call("propose", rules=RULES)
    call("propose", rules={"work": {"days": "mon-thu", "start": "08:00", "end": "16:00"}})
    states = [v["state"] for v in plans.versions(WEEK)]
    assert states == [plans.DRAFT, plans.DISCARDED]
    assert plans.draft(WEEK)["rules"]["work"]["days"] == "mon,tue,wed,thu"


# ===========================================================================================
# apply
# ===========================================================================================

def test_apply_writes_every_block_as_a_marked_paris_time_event(svc):
    out = planned_in_calendar(svc)
    assert "6 created" in out
    events = [e for e in svc.live() if cal.is_jarvis_made(e)]
    assert len(events) == 6 and all(e["summary"] == "Work" and e["colorId"] == cal.JARVIS_COLOR_ID for e in events)
    assert all(e["extendedProperties"]["private"]["jarvis_plan"] == WEEK for e in events)
    first = min(events, key=lambda e: cal.event_span(e)[0])
    assert cal.event_span(first) == (paris(2026, 10, 5, 9), paris(2026, 10, 5, 10))
    assert mine(svc) == standing()
    plan = plans.applied(WEEK)
    assert all(r["status"] == plans.SCHEDULED and r["event_id"] for r in plan["blocks"])
    assert {e["event"] for e in plans.events(week_start=WEEK)} == {"proposed", "created"}


def test_the_plan_never_touches_or_overlaps_his_own_events(svc):
    planned_in_calendar(svc)
    his = [(cal.event_span(e)) for e in svc.live() if not cal.is_jarvis_made(e)]
    assert len(his) == 3
    for _title, s, e in mine(svc):
        for hs, he in his:
            assert e <= hs.astimezone(timezone.utc) or s >= he.astimezone(timezone.utc)
    assert not [c for c in written(svc) if c[0] != "insert"]
    assert all(e["summary"] in ("Standup", "Dentist", "Offsite") for e in svc.live() if not cal.is_jarvis_made(e))


def test_a_failed_apply_reports_what_failed_and_a_retry_finishes_it_without_duplicates(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    svc.fail("insert", FakeHttpError(500, "backend error"), times=2)
    out = call("apply")
    assert "4 created" in out and out.count("Not done:") == 2 and "apply again" in out
    assert len(mine(svc)) == 4
    plan = plans.applied(WEEK)
    assert sorted(r["status"] for r in plan["blocks"]) == ["failed", "failed", "scheduled", "scheduled", "scheduled", "scheduled"]
    retry = call("apply")
    assert "2 created" in retry and "4 unchanged" in retry
    assert len(mine(svc)) == 6 and mine(svc) == standing()
    assert call("apply").startswith("Nothing to apply")


def test_a_write_whose_answer_was_lost_does_not_become_two_events(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    real = svc.events

    class LosesOne:
        def __init__(self):
            self.lost = False

        def __getattr__(self, name):
            return getattr(real(), name)

        def insert(self, **kw):
            req = real().insert(**kw)
            outer = self

            class Req:
                def execute(self_inner):
                    result = req.execute()
                    if not outer.lost:
                        outer.lost = True
                        raise FakeHttpError(503, "the answer never arrived")
                    return result
            return Req()

    svc.events = lambda: LosesOne()
    out = call("apply")
    assert "Not done:" in out and len(mine(svc)) == 6, "the lost write did land"
    svc.events = real
    retry = call("apply")
    assert len(mine(svc)) == 6, "and the retry recognised it instead of writing it again"
    assert "Not done" not in retry and mine(svc) == standing()
    assert any(e["reason"].startswith("found already there") for e in plans.events(week_start=WEEK))


def test_apply_refuses_a_proposal_that_was_replaced(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    call("propose", rules=RULES)
    out = call("apply", plan_id=1)
    assert out.startswith("Error: plan #1 was replaced by a newer one (#2)") and mine(svc) == []
    assert call("apply", plan_id=99).startswith("Error: there is no plan #99")
    assert call("apply", week="2026-11-02").startswith("Error:")


def test_a_meeting_added_after_the_proposal_keeps_its_block_out_of_the_calendar(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    svc.seed("New client call", paris(2026, 10, 6, 11), paris(2026, 10, 6, 12))       # lands on Tuesday's work
    out = call("apply")
    assert "Not done:" in out and "Tuesday 09:00-17:00 work" in out and "clashes with 'New client call'" in out
    assert len(mine(svc)) == 5
    for _t, s, e in mine(svc):
        assert e <= paris(2026, 10, 6, 11).astimezone(timezone.utc) or s >= paris(2026, 10, 6, 12).astimezone(timezone.utc)


def test_a_block_that_has_already_started_is_not_written(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    late = datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc)             # Monday 10:30 Paris
    out = call("apply", now=late)
    assert "in the past" in out and len(mine(svc)) == 5


def test_applying_a_second_version_changes_only_what_differs(svc):
    planned_in_calendar(svc)
    ids_before = {e["id"] for e in svc.live() if cal.is_jarvis_made(e)}
    call("add", rules=GYM4)
    before = len(svc.calls)
    out = call("apply")
    assert "4 created" in out and "6 unchanged" in out
    new_calls = [c for c in svc.calls[before:] if c[0] in ("insert", "patch", "delete")]
    assert [c[0] for c in new_calls] == ["insert"] * 4, "no work block was rewritten"
    assert ids_before <= {e["id"] for e in svc.live()}
    assert mine(svc) == standing()
    rows = {v["version"]: v["state"] for v in plans.versions(WEEK)}
    assert rows == {2: plans.APPLIED, 1: plans.SUPERSEDED}


# ===========================================================================================
# add
# ===========================================================================================

def test_add_places_new_blocks_around_what_stands_and_moves_nothing(svc):
    planned_in_calendar(svc)
    first = {r["uid"]: (r["start_at"], r["end_at"]) for r in plans.applied(WEEK)["blocks"]}
    out = call("add", rules=GYM4)
    assert out.startswith("Added to.") and "gym" in out
    draft = plans.draft(WEEK)
    assert kinds([(r["kind"],) for r in draft["blocks"]]).count("gym") == 4
    for r in draft["blocks"]:
        if r["uid"] in first:
            assert (r["start_at"], r["end_at"]) == first[r["uid"]]
    assert draft["rules"]["blocks"][0]["kind"] == "gym" and draft["rules"]["work"]["days"] == "mon,tue,wed,thu,fri"
    assert draft["parent_id"] == plans.applied(WEEK)["id"]


def test_add_gym_after_work_respects_the_hours_the_buffer_and_his_evening(svc):
    planned_in_calendar(svc)
    call("add", rules=GYM4)
    for r in plans.draft(WEEK)["blocks"]:
        if r["kind"] != "gym":
            continue
        s, e = P.local(W._dt(r["start_at"])), P.local(W._dt(r["end_at"]))
        assert e.hour <= 20 and not (s.weekday() == 2 and e.hour >= 18), "never after 20:00, Wednesday evening free"


def test_add_a_kind_that_exists_means_more_of_it(svc):
    week_with_meetings(svc)
    call("propose", rules={**RULES, "blocks": [{"kind": "gym", "count": 2, "minutes": 60}]})
    call("apply")
    call("add", rules={"blocks": [{"kind": "gym", "count": 1}]})
    draft = plans.draft(WEEK)
    assert sum(1 for r in draft["blocks"] if r["kind"] == "gym") == 3
    assert draft["rules"]["blocks"][0]["count"] == 3


def test_add_without_any_plan_is_a_first_proposal(svc):
    week_with_meetings(svc)
    out = call("add", rules={**RULES, **GYM4})
    assert out.startswith("Proposed.") and plans.draft(WEEK)["version"] == 1


def test_add_with_a_rule_it_cannot_read_changes_nothing(svc):
    planned_in_calendar(svc)
    assert call("add", rules={"blocks": [{"kind": "gym", "count": 99}]}).startswith("Error:")
    assert call("add", rules={"favourite": "blue"}).startswith("Error:")
    assert plans.draft(WEEK) is None


def test_adding_a_limit_that_existing_blocks_break_names_them_and_moves_none(svc):
    week_with_meetings(svc)
    call("propose", rules={"blocks": [{"kind": "gym", "count": 2, "minutes": 60, "prefer": "evening"}]})
    call("apply")
    out = call("add", rules={"keep_free": [{"days": "all", "from": "17:00", "to": "22:00"}]})
    assert "no longer fits the rules" in out and out.count("gym") >= 2
    assert kinds([(r["kind"],) for r in plans.draft(WEEK)["blocks"]]) == ["gym", "gym"]


# ===========================================================================================
# change / remove
# ===========================================================================================

def planned_with_gym(svc):
    week_with_meetings(svc)
    call("propose", rules={**RULES, **GYM4})
    assert call("apply").startswith("Applied")


def gym_on(day_offset):
    return [r for r in plans.applied(WEEK)["blocks"] if r["kind"] == "gym"
            and P.local(W._dt(r["start_at"])).date() == date.fromisoformat(WEEK) + timedelta(days=day_offset)]


def test_moving_thursdays_gym_to_the_evening_patches_the_same_event(svc):
    planned_with_gym(svc)
    (before,) = gym_on(3)
    out = call("change", kind="gym", day="thu", prefer="evening")
    assert out.startswith("Moved Thursday's gym to 18:30-19:30") and "1 moved" in out
    (after,) = gym_on(3)
    assert after["event_id"] == before["event_id"] and after["uid"] == before["uid"]
    assert cal.event_span(svc.items[after["event_id"]]) == (paris(2026, 10, 8, 18, 30), paris(2026, 10, 8, 19, 30))
    assert [c[0] for c in written(svc)][-1] == "patch"
    assert mine(svc) == standing()
    versions = plans.versions(WEEK)
    assert [v["state"] for v in versions[:2]] == [plans.APPLIED, plans.SUPERSEDED]
    assert versions[0]["note"].startswith("moved gym to Thu 18:30") and versions[0]["parent_id"] == versions[1]["id"]


def test_finish_at_four_on_friday_shortens_the_last_work_block(svc):
    planned_with_gym(svc)
    out = call("change", kind="work", day="fri", end="16:00")
    assert out.startswith("Moved Friday's work to 09:00-16:00"), out
    friday = [r for r in plans.applied(WEEK)["blocks"] if r["kind"] == "work"
              and P.local(W._dt(r["start_at"])).date() == date(2026, 10, 9)]
    assert len(friday) == 1 and P.local(W._dt(friday[0]["end_at"])).strftime("%H:%M") == "16:00"
    assert mine(svc) == standing()


def test_a_change_that_cannot_stand_says_why_and_changes_nothing(svc):
    planned_with_gym(svc)
    version = plans.applied(WEEK)["version"]
    calls = len(written(svc))
    out = call("change", kind="gym", day="thu", start="10:00")
    assert out.startswith("Error:") and "clashes with 'Offsite'" in out and "Nothing was changed" in out
    out = call("change", kind="gym", day="mon", to_day="wed", start="19:00")
    assert "keep Wednesday" in out
    assert plans.applied(WEEK)["version"] == version and len(written(svc)) == calls


def test_several_matching_blocks_ask_which_one(svc):
    planned_with_gym(svc)
    out = call("change", kind="work", day="mon", minutes=30)
    assert out.startswith("Error: several blocks match") and "near" in out
    # ("start at 09:30" on a day with several work blocks means the first one, "finish at" the last)
    assert call("change", kind="work", day="mon", start="09:30").startswith(
        "Error: Monday 09:00-10:00 work can't go there")
    out = call("change", kind="work", day="mon", near="10:30", start="10:45")
    assert out.startswith("Moved Monday's work"), out


def test_a_block_that_is_not_there_lists_what_is(svc):
    planned_with_gym(svc)
    out = call("change", kind="swim", day="tue", start="08:00")
    assert out.startswith("Error: no swim on Tuesday in the plan (it has:")
    assert call("change", kind="gym", day="someday", start="08:00").startswith("Error: I can't read the day")
    assert call("change", kind="gym", day="thu").startswith("Error: say what to change")
    assert call("change", kind="gym", day="thu", prefer="dusk").startswith("Error: prefer must be")


def test_moving_to_another_day_overrides_the_kinds_weekday_rule_but_not_his_meetings(svc):
    week_with_meetings(svc)
    call("propose", rules={**RULES, "blocks": [{"kind": "gym", "count": 2, "minutes": 60, "days": "mon,tue"}]})
    call("apply")
    (monday,) = gym_on(0)
    out = call("change", kind="gym", day="mon", to_day="fri")
    assert out.startswith("Moved Monday's gym to Friday"), out
    assert call("change", kind="gym", day="tue", to_day="thu", start="10:00").startswith("Error:")


def test_a_change_on_a_proposal_edits_the_proposal_and_writes_nothing(svc):
    week_with_meetings(svc)
    call("propose", rules={**RULES, **GYM4})
    out = call("change", kind="gym", day="thu", prefer="morning")
    assert "It is a proposal; nothing is in the calendar" in out and written(svc) == []
    assert plans.draft(WEEK)["version"] == 2


def test_remove_deletes_the_event_and_the_block(svc):
    planned_with_gym(svc)
    (gym,) = gym_on(4)
    out = call("remove", kind="gym", day="fri")
    assert out.startswith("Removed Friday") and "1 removed" in out
    assert svc.items[gym["event_id"]]["status"] == "cancelled"
    assert gym_on(4) == [] and mine(svc) == standing()
    assert [e["event"] for e in plans.events(block_uid=gym["uid"])] == ["proposed", "created", "removed"]


def test_a_reason_is_kept_in_the_history(svc):
    planned_with_gym(svc)
    call("change", kind="gym", day="thu", prefer="evening", reason="he has a call at five")
    assert "he has a call at five" in plans.versions(WEEK)[0]["note"]


# ===========================================================================================
# undo
# ===========================================================================================

def test_undo_of_a_move_puts_the_same_event_back(svc):
    planned_with_gym(svc)
    (before,) = gym_on(3)
    call("change", kind="gym", day="thu", prefer="evening")
    out = call("undo")
    assert out.startswith("Undid version 2") and "1 moved" in out
    (after,) = gym_on(3)
    assert after["event_id"] == before["event_id"] and W._planned(after) == W._planned(before)
    assert mine(svc) == standing()
    assert plans.applied(WEEK)["version"] == 3 and plans.applied(WEEK)["created_by"] == "owner"


def test_undo_of_a_removal_brings_the_block_back_as_a_new_event(svc):
    planned_with_gym(svc)
    (gym,) = gym_on(4)
    call("remove", kind="gym", day="fri")
    out = call("undo")
    assert "1 created" in out
    (back,) = gym_on(4)
    assert back["uid"] == gym["uid"] and back["event_id"] != gym["event_id"], "Google keeps a deleted id reserved"
    assert mine(svc) == standing() and len(mine(svc)) == 10


def test_undo_of_an_add_takes_the_new_blocks_out_and_restores_the_work_it_split(svc):
    planned_in_calendar(svc)
    original = standing()
    call("add", rules={"blocks": [{"kind": "gym", "count": 2, "minutes": 60, "prefer": "afternoon", "days": "mon,tue"}]})
    call("apply")
    assert kinds(standing()).count("gym") == 2
    call("undo")
    assert standing() == original and mine(svc) == original


def test_undoing_twice_walks_back_and_then_says_there_is_nothing_left(svc):
    planned_with_gym(svc)
    zero = standing()
    call("change", kind="gym", day="thu", prefer="evening")
    one = standing()
    call("remove", kind="gym", day="fri")
    call("undo")
    assert standing() == one
    call("undo")
    assert standing() == zero and mine(svc) == zero
    assert call("undo").startswith("Nothing to undo")


def test_undo_with_a_proposal_pending_just_drops_the_proposal(svc):
    planned_with_gym(svc)
    call("add", rules={"blocks": [{"kind": "reading", "count": 1, "minutes": 30}]})
    out = call("undo")
    assert out.startswith("Dropped the proposal") and "plan version 1" in out
    assert plans.draft(WEEK) is None and len(written(svc)) == 10


def test_undo_does_not_undo_what_he_did_himself(svc):
    planned_with_gym(svc)
    call("change", kind="gym", day="thu", prefer="evening")
    (mon,) = gym_on(0)
    svc.items[mon["event_id"]]["start"] = {"dateTime": paris(2026, 10, 5, 18).isoformat(), "timeZone": "Europe/Paris"}
    svc.items[mon["event_id"]]["end"] = {"dateTime": paris(2026, 10, 5, 19).isoformat(), "timeZone": "Europe/Paris"}
    out = call("undo")
    assert out.startswith("Undid version 2")
    span = cal.event_span(svc.items[mon["event_id"]])
    assert span == (paris(2026, 10, 5, 18), paris(2026, 10, 5, 19)), "his move of Monday's gym stands"
    (thu,) = gym_on(3)
    assert cal.event_span(svc.items[thu["event_id"]])[0] == paris(2026, 10, 8, 17, 30)


def test_undo_will_not_move_a_block_back_over_something_new(svc):
    planned_with_gym(svc)
    (thu,) = gym_on(3)
    call("change", kind="gym", day="thu", prefer="evening")
    svc.seed("Concert", paris(2026, 10, 8, 17, 30), paris(2026, 10, 8, 19))         # now sits on the old slot
    out = call("undo")
    assert "could not move" in out and "clashes with 'Concert'" in out
    assert cal.event_span(svc.items[thu["event_id"]])[0] == paris(2026, 10, 8, 18, 30)


# ===========================================================================================
# what he changes himself
# ===========================================================================================

def move_in_calendar(svc, event_id, start, end):
    svc.items[event_id]["start"] = {"dateTime": start.isoformat(), "timeZone": "Europe/Paris"}
    svc.items[event_id]["end"] = {"dateTime": end.isoformat(), "timeZone": "Europe/Paris"}


def test_a_block_he_moved_is_noticed_recorded_as_his_and_left_where_he_put_it(svc):
    planned_with_gym(svc)
    (tue,) = gym_on(1)
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    calls = len(written(svc))
    out = call("sync")
    assert out.startswith("You changed:") and "Tuesday" in out and "moved it to Tue 07:00-08:00" in out
    assert len(written(svc)) == calls, "noticing never writes"
    (row,) = gym_on(1)
    assert row["status"] == plans.MOVED and W._eff(row) != W._planned(row)
    events = plans.events(block_uid=tue["uid"])
    assert events[-1]["event"] == "seen_moved" and events[-1]["actor"] == "owner"
    assert (events[-1]["old_start"], events[-1]["new_start"]) == (tue["start_at"], paris(2026, 10, 6, 7).timestamp())
    assert call("sync").startswith("The calendar matches"), "and he is told about it once"


def test_a_block_he_deleted_is_noticed_and_not_brought_back(svc):
    planned_with_gym(svc)
    (wed,) = [r for r in plans.applied(WEEK)["blocks"] if r["kind"] == "work"][:1]
    svc.items[wed["event_id"]]["status"] = "cancelled"
    out = call("sync")
    assert "you deleted it from your calendar" in out
    row = next(r for r in plans.applied(WEEK)["blocks"] if r["uid"] == wed["uid"])
    assert row["status"] == plans.DELETED
    assert plans.events(block_uid=wed["uid"])[-1]["event"] == "seen_deleted"
    call("add", rules={"blocks": [{"kind": "reading", "count": 1, "minutes": 30}]})
    call("apply")
    assert svc.items[wed["event_id"]]["status"] == "cancelled"
    assert mine(svc) == standing(), "his deletion stands: no new event for that block"


def test_a_block_he_put_back_where_it_was_is_scheduled_again(svc):
    planned_with_gym(svc)
    (tue,) = gym_on(1)
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    call("sync")
    move_in_calendar(svc, tue["event_id"], *(paris(2026, 10, 6, 17, 30), paris(2026, 10, 6, 18, 30)))
    call("sync")
    (row,) = gym_on(1)
    assert row["status"] in (plans.SCHEDULED, plans.MOVED)
    if row["status"] == plans.SCHEDULED:
        assert row["live_start"] is None


def test_a_change_to_another_block_leaves_his_move_alone(svc):
    planned_with_gym(svc)
    (tue,) = gym_on(1)
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    call("change", kind="gym", day="thu", prefer="evening")
    assert cal.event_span(svc.items[tue["event_id"]]) == (paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    (row,) = gym_on(1)
    assert row["status"] == plans.MOVED


def test_when_he_asks_to_change_a_block_he_moved_it_goes_from_where_he_put_it(svc):
    planned_with_gym(svc)
    (tue,) = gym_on(1)
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    out = call("change", kind="gym", day="tue", start="08:00")
    assert out.startswith("Moved Tuesday's gym to 08:00-09:00"), out
    assert cal.event_span(svc.items[tue["event_id"]])[0] == paris(2026, 10, 6, 8)
    (row,) = gym_on(1)
    assert row["status"] == plans.SCHEDULED and row["live_start"] is None


def test_an_event_that_is_not_the_blocks_own_is_not_deleted(svc):
    planned_with_gym(svc)
    (fri,) = gym_on(4)
    svc.items[fri["event_id"]]["extendedProperties"]["private"]["jarvis_block"] = "somebody-elses"
    out = call("remove", kind="gym", day="fri")
    assert "not the one I made, so I left it" in out
    assert svc.items[fri["event_id"]]["status"] == "confirmed"


def test_show_lists_the_week_with_the_status_of_each_block(svc):
    planned_with_gym(svc)
    (tue,) = gym_on(1)
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    (wed_work,) = [r for r in plans.applied(WEEK)["blocks"] if r["kind"] == "work"][:1]
    svc.items[wed_work["event_id"]]["status"] = "cancelled"
    out = call("show")
    assert "in your calendar:" in out and "07:00-08:00 gym  (you moved it here from 17:30)" in out
    assert "(you deleted it from your calendar)" in out
    assert "Since I last looked:" in out
    day = call("show", day="thu")
    assert "Mon 2026-10-05\n" not in day and "Thu 2026-10-08\n  17:30-18:30 gym" in day


def test_show_labels_a_proposal_and_says_when_there_is_no_plan(svc):
    assert call("show").startswith("Error: there is no plan yet")
    assert call("show", week="next").startswith("There is no plan for the week of Mon 2026-10-05")
    week_with_meetings(svc)
    call("propose", rules=RULES)
    assert "a proposal: nothing in your calendar yet" in call("show")
    call("apply")
    call("add", rules=GYM4)
    assert "Your calendar still has plan version 1" in call("show")


def test_sync_with_nothing_in_the_calendar_says_so(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    assert call("sync").startswith("There is no plan in the calendar for that week")


# ===========================================================================================
# the clock change, the async entry point, unknown actions
# ===========================================================================================

def test_the_week_the_clocks_go_back_keeps_wall_clock_hours(svc):
    rules = {"blocks": [{"kind": "run", "count": 2, "minutes": 60, "days": "mon,sun", "prefer": "morning",
                         "not_before": "09:00", "not_after": "10:00"}]}
    assert call("propose", rules=rules, week="2026-10-19").startswith("Proposed.")
    assert call("apply", week="2026-10-19").startswith("Applied")
    starts = sorted(cal.event_span(e)[0] for e in svc.live())
    assert [s.strftime("%a %H:%M %z") for s in starts] == ["Mon 09:00 +0200", "Sun 09:00 +0100"]
    assert all(cal.event_span(e)[1] - cal.event_span(e)[0] == timedelta(hours=1) for e in svc.live())


def test_a_run_across_the_repeated_hour_is_still_an_hour(svc):
    rules = {"not_before": "01:00", "not_after": "04:00",
             "blocks": [{"kind": "night owl", "count": 1, "minutes": 120, "days": "sun", "prefer": "morning"}]}
    call("propose", rules=rules, week="2026-10-19")
    call("apply", week="2026-10-19")
    (ev,) = svc.live()
    s, e = cal.event_span(ev)
    assert (e.astimezone(timezone.utc) - s.astimezone(timezone.utc)) == timedelta(hours=2)


def test_the_tool_entry_point_runs_the_same_thing_off_the_event_loop(svc, monkeypatch):
    monkeypatch.setattr(W, "_now", lambda: NOW)
    week_with_meetings(svc)
    out = asyncio.run(W.week_plan(action="propose", rules=RULES, quote="ignored"))
    assert out.startswith("Proposed.")
    assert asyncio.run(W.week_plan(action="show")).startswith("Plan #1")
    assert W.run(W.Ask(action="dance")).startswith("Error: unknown action 'dance'")


def test_a_failure_nobody_planned_for_becomes_an_error_not_an_exception(svc, monkeypatch):
    week_with_meetings(svc)
    monkeypatch.setattr(P, "plan_week", lambda *a, **k: (_ for _ in ()).throw(ZeroDivisionError("bug")))
    out = call("propose", rules=RULES)
    assert out.startswith("Error: week_plan propose failed (bug)") and plans.versions(WEEK) == []


def test_without_storage_nothing_is_claimed_planned(svc, monkeypatch):
    from backend.storage import db
    week_with_meetings(svc)
    monkeypatch.setattr(db, "connect", lambda: None)
    assert call("propose", rules=RULES).startswith("Error: I could not record the plan")


# ===========================================================================================
# invariants on random sequences
# ===========================================================================================

def _random_rules(rng):
    rules = {"buffer": rng.choice([0, 10, 15])}
    if rng.random() < 0.8:
        rules["work"] = {"days": rng.choice(["mon-fri", "mon-thu"]), "start": rng.choice(["08:00", "09:00"]),
                         "end": rng.choice(["16:00", "17:30"])}
    if rng.random() < 0.5:
        rules["keep_free"] = [{"days": rng.choice(["wed", "fri"]), "from": "12:00", "to": "13:30"}]
    rules["blocks"] = [{"kind": k, "count": rng.randint(1, 3), "minutes": rng.choice([30, 60]),
                        "prefer": rng.choice(["", "morning", "evening"])}
                       for k in rng.sample(["gym", "reading", "run"], rng.randint(0, 2))]
    if not rules["blocks"] and "work" not in rules:
        rules["blocks"] = [{"kind": "gym", "count": 2, "minutes": 60}]
    return rules


def test_random_sequences_keep_the_calendar_equal_to_the_plan_and_never_touch_his_events(monkeypatch):
    # (WEEKPLAN_RANDOM_TRIALS / WEEKPLAN_RANDOM_SEED run it deeper by hand)
    rng = random.Random(int(os.getenv("WEEKPLAN_RANDOM_SEED", "20261005")))
    week = "2026-10-05"
    checked = 0
    trials = int(os.getenv("WEEKPLAN_RANDOM_TRIALS", "40"))
    for trial in range(trials):
        svc = FakeCalendarService()
        monkeypatch.setattr(cal, "_get_service", lambda s=svc: s)
        from backend.storage import db
        db.close_all()
        monkeypatch.setenv("JARVIS_DB_PATH", str(__import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "t.db"))
        his = []
        for _ in range(rng.randint(0, 6)):
            d = rng.randint(0, 4)
            s = rng.randrange(8 * 60, 17 * 60, 30)
            ev = svc.seed(f"m{_}", paris(2026, 10, 5 + d, s // 60, s % 60),
                          paris(2026, 10, 5 + d, s // 60, s % 60) + timedelta(minutes=rng.choice([30, 60, 120])))
            his.append(ev["id"])
        call("propose", rules=_random_rules(rng))
        call("apply")
        for _step in range(rng.randint(2, 6)):
            op = rng.choice(["add", "add", "apply", "change", "remove", "undo", "sync"])
            if op == "add":
                call("add", rules={"blocks": [{"kind": rng.choice(["gym", "reading", "run", "swim"]),
                                               "count": rng.randint(1, 2), "minutes": 30}]})
            elif op == "change":
                plan = plans.latest_live(week)
                rows = [r for r in (plan or {"blocks": []})["blocks"] if r["kind"] != "work"]
                if rows:
                    r = rng.choice(rows)
                    call("change", kind=r["kind"], day=P.day_name(P.local(W._dt(r["start_at"])).date()),
                         prefer=rng.choice(["morning", "afternoon", "evening"]),
                         to_day=rng.choice(["", "", "tue", "thu"]))
            elif op == "remove":
                plan = plans.latest_live(week)
                rows = [r for r in (plan or {"blocks": []})["blocks"] if r["kind"] != "work"]
                if rows:
                    r = rng.choice(rows)
                    call("remove", kind=r["kind"], day=P.day_name(P.local(W._dt(r["start_at"])).date()),
                         near=P.local(W._dt(r["start_at"])).strftime("%H:%M"))
            else:
                call(op)
            if plans.draft(week) is None and plans.applied(week) is not None:
                assert mine(svc) == standing(), f"trial {trial} after {op}: calendar and plan differ"
                checked += 1
        # his events are untouched, and nothing JARVIS made overlaps them
        for eid in his:
            assert svc.items[eid]["status"] == "confirmed"
        assert not [c for c in written(svc) if c[0] in ("patch", "delete") and c[1] in his]
        for _t, s, e in mine(svc):
            for eid in his:
                hs, he = (x.astimezone(timezone.utc) for x in cal.event_span(svc.items[eid]))
                assert e <= hs or s >= he, f"trial {trial}: a block overlaps his event"
    assert checked > trials * 1.4


# ===========================================================================================
# what he is told on every turn while a proposal waits for his yes
# ===========================================================================================

def test_no_line_when_nothing_is_waiting(svc):
    assert W.prompt_line(NOW) == ""
    planned_in_calendar(svc)
    assert W.prompt_line(NOW) == ""


def test_a_proposal_waiting_for_his_yes_is_named(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    line = W.prompt_line(NOW)
    assert "proposal #1" in line and "week of Mon 05 Oct" in line and "6 work not in his calendar yet" in line
    assert "week_plan apply" in line and "do not propose or add again" in line


def test_an_added_draft_names_only_what_is_not_in_the_calendar_yet(svc):
    planned_in_calendar(svc)
    call("add", rules=GYM4)
    line = W.prompt_line(NOW)
    assert "4 gym not in his calendar yet" in line
    assert "work" not in line.split("(")[1].split(")")[0]


def test_the_line_goes_once_the_proposal_is_applied(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    assert W.prompt_line(NOW)
    call("apply")
    assert W.prompt_line(NOW) == ""


def test_a_proposal_older_than_a_day_is_no_longer_the_one_being_discussed():
    plans.create_version(WEEK, {"work": WORK}, [{"kind": "work", "start": at(0, "09:00").timestamp(),
                                                 "end": at(0, "17:00").timestamp()}],
                         created_by="owner", now=NOW.timestamp())
    assert W.prompt_line(NOW + timedelta(hours=23))
    assert W.prompt_line(NOW + timedelta(hours=25)) == ""


def test_a_broken_store_gives_no_line_and_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(plans, "newest_draft", boom)
    assert W.prompt_line(NOW) == ""


def test_the_runtime_identity_carries_the_line_and_survives_its_failure(monkeypatch):
    from backend.brain import runtime_status
    monkeypatch.setattr(W, "prompt_line", lambda now=None: "Week plan: proposal #7 waits.")
    assert "Week plan: proposal #7 waits." in runtime_status.runtime_identity_text()

    def boom(now=None):
        raise RuntimeError("x")
    monkeypatch.setattr(W, "prompt_line", boom)
    assert runtime_status.runtime_identity_text().startswith("[Runtime identity]")


# ===========================================================================================
# gaps that mutation testing found
# ===========================================================================================

def test_a_move_made_after_the_last_look_is_not_overwritten_by_a_change(svc, monkeypatch):
    """The look at the calendar can be stale (or fail); the write itself checks before it moves anything."""
    planned_with_gym(svc)
    (tue,) = gym_on(1)
    monkeypatch.setattr(W, "_guarded_sync", lambda svc_, monday: [])
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    out = call("change", kind="gym", day="tue", start="08:00")
    assert cal.event_span(svc.items[tue["event_id"]]) == (paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    assert "you moved it in your calendar since I set it up" in out
    (row,) = gym_on(1)
    assert row["status"] == plans.MOVED and row["live_start"] == paris(2026, 10, 6, 7).timestamp()


def test_a_proposal_drawn_before_he_changed_the_calendar_takes_over_what_is_true_now(svc):
    planned_with_gym(svc)
    call("add", rules={"blocks": [{"kind": "reading", "count": 1, "minutes": 30}]})      # a draft on top
    (tue,) = gym_on(1)
    move_in_calendar(svc, tue["event_id"], paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))
    (wed,) = [r for r in plans.applied(WEEK)["blocks"] if r["kind"] == "work"][:1]
    svc.items[wed["event_id"]]["status"] = "cancelled"
    assert call("apply").startswith("Applied")
    rows = {r["uid"]: r for r in plans.applied(WEEK)["blocks"]}
    assert rows[tue["uid"]]["status"] == plans.MOVED
    assert rows[tue["uid"]]["live_start"] == paris(2026, 10, 6, 7).timestamp()
    assert rows[wed["uid"]]["status"] == plans.DELETED
    assert svc.items[wed["event_id"]]["status"] == "cancelled", "his deletion stands"
    assert cal.event_span(svc.items[tue["event_id"]]) == (paris(2026, 10, 6, 7), paris(2026, 10, 6, 8))


def test_undo_leaves_a_block_he_moved_himself_after_the_change_it_undoes(svc):
    planned_with_gym(svc)
    (thu,) = gym_on(3)
    call("change", kind="gym", day="thu", prefer="evening")                # version 2
    move_in_calendar(svc, thu["event_id"], paris(2026, 10, 8, 20), paris(2026, 10, 8, 21))
    out = call("undo")
    assert "was changed since (by you or by a later change), so I left it" in out
    assert cal.event_span(svc.items[thu["event_id"]]) == (paris(2026, 10, 8, 20), paris(2026, 10, 8, 21))


def test_undoing_an_add_leaves_an_added_block_he_moved_himself(svc):
    planned_with_gym(svc)
    call("add", rules={"blocks": [{"kind": "reading", "count": 1, "minutes": 30}]})
    call("apply")
    (reading,) = [r for r in plans.applied(WEEK)["blocks"] if r["kind"] == "reading"]
    move_in_calendar(svc, reading["event_id"], paris(2026, 10, 6, 6), paris(2026, 10, 6, 6, 30))
    out = call("undo")
    assert "was changed since, so I left it" in out
    assert svc.items[reading["event_id"]]["status"] == "confirmed"
    assert cal.event_span(svc.items[reading["event_id"]])[0] == paris(2026, 10, 6, 6)


def test_an_event_id_held_by_a_deleted_event_gets_a_new_generation(svc):
    week_with_meetings(svc)
    call("propose", rules=RULES)
    draft = plans.draft(WEEK)
    for r in draft["blocks"]:                      # something deleted earlier still holds each first id
        eid = cal.event_id_for(WEEK, r["uid"], 0)
        svc.seed("old", paris(2026, 10, 5, 6), paris(2026, 10, 5, 7), event_id=eid)
        svc.items[eid]["status"] = "cancelled"
    out = call("apply")
    assert out.startswith("Applied plan") and "Not done" not in out, out
    assert len(mine(svc)) == 6 and mine(svc) == standing()
    assert any(e["event"] == "restored" for e in plans.events(block_uid=draft["blocks"][0]["uid"]))


def test_a_sunday_event_is_busy_time_for_a_sunday_block(svc):
    svc.seed("Family lunch", paris(2026, 10, 11, 10), paris(2026, 10, 11, 12))
    out = call("propose", rules={"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "days": "sun",
                                             "not_before": "10:00", "not_after": "12:00"}]})
    assert "Could not place 1 of 1 gym" in out


def test_the_plans_own_events_are_found_by_id_even_when_their_marker_is_lost(svc):
    planned_in_calendar(svc)
    before = [(r["kind"], r["start_at"]) for r in plans.applied(WEEK)["blocks"]]
    for ev in svc.items.values():
        priv = (ev.get("extendedProperties") or {}).get("private")
        if priv and "jarvis_block" in priv:
            del priv["jarvis_plan"]
            del priv["jarvis_block"]
    call("propose", rules=RULES)
    assert [(r["kind"], r["start_at"]) for r in plans.draft(WEEK)["blocks"]] == before


# ===========================================================================================
# P4: the standing OK to move blocks, and reporting through the tool
# ===========================================================================================

def test_auto_move_alone_in_the_rules_is_a_standing_ok_and_not_a_plan(svc):
    from backend.brain import planday
    out = call("propose", rules={"auto_move": ["gym"]})
    assert out.startswith("Noted: I may move your gym blocks myself when you skip one or a meeting lands on it")
    assert planday.auto_move_kinds() == ["gym"] and plans.draft(WEEK) is None and written(svc) == []


def test_auto_move_next_to_real_rules_is_taken_out_before_planning(svc):
    from backend.brain import planday
    week_with_meetings(svc)
    out = call("propose", rules={**RULES, **GYM4, "auto_move": ["gym"]})
    assert out.startswith("Noted: I may move your gym blocks myself") and "\nProposed." in out
    assert planday.auto_move_kinds() == ["gym"]
    assert "auto_move" not in plans.draft(WEEK)["rules"], "it is a setting, not part of the plan's rules"


def test_an_empty_auto_move_takes_the_ok_back(svc):
    from backend.brain import planday
    call("propose", rules={"auto_move": ["gym"]})
    out = call("add", rules={"auto_move": []})
    assert out == "Cleared: I ask before moving any block." and planday.auto_move_kinds() == []


def test_auto_move_may_come_as_json_text(svc):
    from backend.brain import planday
    call("propose", rules='{"auto_move": ["reading", "gym"]}')
    assert planday.auto_move_kinds() == ["reading", "gym"]


def test_auto_move_for_work_or_a_nonsense_kind_is_an_error_and_changes_nothing(svc):
    from backend.brain import planday
    assert call("propose", rules={"auto_move": ["work"]}).startswith("Error: work blocks are not moved")
    assert call("propose", rules={"auto_move": ["Not A Kind!"]}).startswith("Error:")
    assert call("propose", rules={"auto_move": "gym", "work": WORK}).startswith("Noted:") is True
    assert planday.auto_move_kinds() == ["gym"]


def test_an_unknown_rule_field_is_still_an_error_when_auto_move_is_present(svc):
    out = call("propose", rules={"auto_move": ["gym"], "gymm": 3, "work": WORK})
    assert out.startswith("Noted:") is False and "gymm" in out
    from backend.brain import planday
    assert planday.auto_move_kinds() == ["gym"], "the setting was taken, the typo was still refused"


def test_report_is_one_of_the_actions_and_needs_no_approval_from_anyone():
    from backend.tools import policy
    assert "report" in W.ACTIONS and policy.RISK["week_plan"]["report"] == policy.OWN
    assert "report" in W._HANDLERS


def test_the_async_tool_passes_outcomes_and_his_words_through(svc, monkeypatch):
    planned_with_gym(svc)
    monkeypatch.setattr(W, "_now", lambda: datetime(2026, 10, 6, 19, 35, tzinfo=timezone.utc))
    out = asyncio.run(W.week_plan(action="report", day="2026-10-06", said="  gym  done ",
                                  outcomes=[{"kind": "gym", "outcome": "done"}]))
    assert out.startswith("Recorded for Tuesday 6 Oct")


def test_the_tool_takes_the_array_as_items_the_name_the_model_reaches_for(svc, monkeypatch):
    """Live (2026-09-30, P4): with the array called `outcomes`, the model sent it as `items` on its first try in
    every reply to the check-in and was rejected (18 wasted round trips in 20 trials). The schema now uses the
    name it uses; `outcomes` stays accepted by the function for callers that already pass it."""
    from backend.tools.registry import TOOLS
    props = next(t for t in TOOLS if t["name"] == "week_plan")["input_schema"]["properties"]
    assert "items" in props and "outcomes" not in props
    planned_with_gym(svc)
    monkeypatch.setattr(W, "_now", lambda: datetime(2026, 10, 6, 19, 35, tzinfo=timezone.utc))
    out = asyncio.run(W.week_plan(action="report", day="2026-10-06", items=[{"kind": "gym", "outcome": "done"}]))
    assert out.startswith("Recorded for Tuesday 6 Oct") and "gym 17:30-18:30: done" in out
    both = asyncio.run(W.week_plan(action="report", day="2026-10-06", outcomes=[{"kind": "work", "outcome": "done"}],
                                   items=[{"kind": "gym", "outcome": "skipped"}]))
    assert "work 09:00-17:00: done" in both and "skipped" not in both, "outcomes wins when both are given"
