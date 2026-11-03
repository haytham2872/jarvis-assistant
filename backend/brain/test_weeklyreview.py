"""P4: the Sunday review (backend/brain/weeklyreview.py) -- counts from the record, patterns as
suggestions only, and next week drawn as a proposal."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brain import planday as D
from backend.brain import weekplan as W
from backend.brain import weeklyreview as V
from backend.storage import plans
from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService

LOCAL_TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
SUN = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)                     # Sunday 19:00 Paris
RULES = {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
         "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}], "buffer": 15,
         "blocks": [{"kind": "gym", "count": 3, "minutes": 60, "not_before": "17:00", "not_after": "20:00"}]}


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    return fake


def plan_week(monday: str, rules=None):
    assert W.run(W.Ask(action="propose", week=monday, rules=rules or RULES), NOW).startswith("Proposed")
    assert W.run(W.Ask(action="apply", week=monday), NOW).startswith("Applied")


def report(day: str, outcomes, when=None):
    d = date.fromisoformat(day)
    when = when or datetime(d.year, d.month, d.day, 19, 35, tzinfo=timezone.utc)
    return W.run(W.Ask(action="report", day=day, outcomes=outcomes, said="x"), when)


def test_no_review_without_a_plan_this_week(svc):
    assert V.compose(SUN) is None


def test_the_review_puts_planned_against_what_happened_from_the_record(svc):
    plan_week("2026-10-05")
    report("2026-10-05", [{"kind": "gym", "outcome": "done"}, {"kind": "work", "outcome": "done"}])
    report("2026-10-08", [{"kind": "gym", "outcome": "skipped", "reason": "tired"}])
    report("2026-10-09", [{"kind": "gym", "outcome": "done"}])
    text = V.compose(SUN)
    assert text.startswith("Weekly review, week of 5 Oct.\nPlanned against what happened: ")
    assert "gym 2 of 3 done, 1 skipped" in text
    assert "not reported" in text, "work on the other days was never asked about"


def test_next_week_is_drawn_from_this_weeks_rules_as_a_proposal_only(svc):
    plan_week("2026-10-05")
    text = V.compose(SUN)
    draft = plans.draft("2026-10-12")
    assert draft is not None and draft["created_by"] == "owner"
    assert f"Next week drawn from the same rules (proposal #{draft['id']}, nothing in your calendar yet):" in text
    assert "Mon 12: work 09:00-17:00, gym 17:30-18:30" in text
    assert "Say yes and I'll put it in your calendar, or tell me what to change." in text
    assert plans.applied("2026-10-12") is None and [e for e in svc.live() if e["summary"].lower() == "gym"
                                                     and e["start"]["dateTime"].startswith("2026-10-12")] == []


def test_a_week_that_already_has_a_plan_is_left_alone(svc):
    plan_week("2026-10-05")
    plan_week("2026-10-12")
    text = V.compose(SUN)
    assert "already has a plan in your calendar; I left it alone." in text
    assert len(plans.versions("2026-10-12")) == 1, "no second version was drawn"


def test_a_proposal_already_waiting_is_not_redrawn(svc):
    plan_week("2026-10-05")
    W.run(W.Ask(action="propose", week="2026-10-12", rules=RULES), NOW)
    before = plans.draft("2026-10-12")["id"]
    text = V.compose(SUN)
    assert f"already has a proposal (#{before}); I left it alone." in text
    assert plans.draft("2026-10-12")["id"] == before


def test_without_a_calendar_next_week_is_not_drawn_and_the_review_says_so(svc):
    plan_week("2026-10-05")
    text = V.compose(SUN, calendar_ready=False)
    assert "I could not reach your calendar, so next week is not drawn yet." in text
    assert plans.draft("2026-10-12") is None


def test_a_failure_to_draw_next_week_is_reported_not_raised(svc, monkeypatch):
    plan_week("2026-10-05")
    real = W.run

    def fake(ask, now=None):
        return "Error: the calendar refused" if ask.action == "propose" else real(ask, now)
    monkeypatch.setattr(W, "run", fake)
    assert "I could not draw next week from these rules (the calendar refused)." in V.compose(SUN)


# ---- patterns ---------------------------------------------------------------------------------

def _thursday_gym_skips(weeks):
    for monday in weeks:
        thursday = str(date.fromisoformat(monday).replace(day=date.fromisoformat(monday).day + 3))
        report(thursday, [{"kind": "gym", "outcome": "skipped"}])


def test_a_kind_that_slipped_on_the_same_weekday_in_two_weeks_is_a_suggestion(svc):
    plan_week("2026-10-05")
    plan_week("2026-10-12")
    _thursday_gym_skips(["2026-10-05", "2026-10-12"])
    lines = V.patterns(date(2026, 10, 18))
    assert lines == ["Thursday gym slipped in 2 of the last 4 weeks. Want me to stop planning gym on Thursdays? "
                     "I only change a rule if you say so."]


def test_one_slip_is_not_a_pattern_and_work_is_never_one(svc):
    plan_week("2026-10-05")
    _thursday_gym_skips(["2026-10-05"])
    report("2026-10-06", [{"kind": "work", "outcome": "skipped"}])
    plan_week("2026-10-12")
    report("2026-10-13", [{"kind": "work", "outcome": "skipped"}])
    assert V.patterns(date(2026, 10, 18)) == []


def test_old_slips_fall_out_of_the_window(svc):
    plan_week("2026-10-05")
    plan_week("2026-10-12")
    _thursday_gym_skips(["2026-10-05", "2026-10-12"])
    assert V.patterns(date(2026, 11, 15)) == []


def test_the_review_includes_a_pattern_but_never_changes_a_rule(svc):
    plan_week("2026-10-05")
    plan_week("2026-10-12")
    _thursday_gym_skips(["2026-10-05", "2026-10-12"])
    text = V.compose(SUN)
    assert text is not None
    assert plans.applied("2026-10-05")["rules"]["blocks"][0]["count"] == 3


def test_a_slip_survives_a_reschedule_in_the_record(svc):
    """The view shows a rescheduled block as 'moved', but the skip still happened."""
    plan_week("2026-10-05")
    plan_week("2026-10-12")
    _thursday_gym_skips(["2026-10-05", "2026-10-12"])
    W.run(W.Ask(action="change", week="2026-10-05", kind="gym", day="2026-10-08", near="17:30",
                to_day="2026-10-10", start="17:30"), datetime(2026, 10, 8, 19, 40, tzinfo=timezone.utc))
    moved = [r for r in plans.block_outcomes(week_start="2026-10-05") if r["kind"] == "gym"]
    assert "moved" in {r["outcome"] for r in moved}
    assert len(plans.slips(since_date="2026-10-01")) == 2
