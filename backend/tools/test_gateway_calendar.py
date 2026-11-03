"""P3: who has to agree to a calendar change, through the real gateway and approval flow.

Changing or deleting an event JARVIS made is free. Anyone else's -- Sam's own meetings --
is his data: it runs only when he asked in the current message (a verified quote) or when he
approves that exact change; unattended work never gets to. Writing a whole week's plan into his
calendar (`week_plan apply`) follows the same rule. The Google calls and the tool bodies are
faked; what is asserted is whether the tool was CALLED.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from backend.brain import weekplan
from backend.storage import approvals, plans
from backend.tools import approval_flow, gateway, policy
from backend.tools import calendar as cal

SAID = "Move my dentist appointment to 4pm please."
QUOTE = "move my dentist appointment to 4pm"
EDIT = {"action": "update_event", "event_id": "dent0001", "start_time": "2026-10-07T16:00"}
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self):
        self.calls = []
        self.owner = "his"
        self.notified = []

    async def calendar(self, **kw):
        self.calls.append(("calendar_action", dict(kw)))
        return "Event updated. Was: x\nNow: y"

    async def week(self, **kw):
        self.calls.append(("week_plan", dict(kw)))
        return "Applied plan #1"

    def dispatch(self):
        return {"calendar_action": self.calendar, "week_plan": self.week}

    async def notifier(self, row):
        self.notified.append(row)
        return "msg-1"


@pytest.fixture
def env(monkeypatch):
    e = Env()

    async def owner_of(event_id):
        return e.owner if event_id else "none"

    async def describe(event_id):
        return "'Dentist' (Wed 2026-10-07 15:00-16:00)" if event_id else ""

    monkeypatch.setattr(cal, "owner_of", owner_of)
    monkeypatch.setattr(cal, "describe_event", describe)
    monkeypatch.setattr(weekplan, "_now", lambda: NOW)
    approval_flow.set_notifier(e.notifier)
    approval_flow._undelivered.clear()
    policy.start_scope([SAID], "")
    yield e
    approval_flow.set_notifier(None)
    approval_flow._undelivered.clear()


def execute(env, tool, args, *, caller):
    return run(gateway.execute(tool, args, dispatch=env.dispatch(), caller=caller))


# --- editing an event ---------------------------------------------------------------------

@pytest.mark.parametrize("caller", ["background", "scheduled", "night", "unattended"])
def test_unattended_work_never_changes_his_meeting(env, caller):
    res = execute(env, "calendar_action", EDIT, caller=caller)
    assert res.pending and env.calls == []
    (row,) = approvals.pending()
    assert row["tool"] == "calendar_action" and row["risk"] == policy.HIS_DATA


def test_the_request_he_reads_names_the_meeting_and_the_change(env):
    execute(env, "calendar_action", EDIT, caller="background")
    (row,) = approvals.pending()
    # (the store keeps one line of text per preview)
    assert row["preview"] == ("Change in your calendar: 'Dentist' (Wed 2026-10-07 15:00-16:00) "
                              "To: start_time=2026-10-07T16:00")
    assert "with nobody watching" in row["why"] and "would change your calendar" in row["why"]
    delete = execute(env, "calendar_action", {"action": "delete_event", "event_id": "dent0001"}, caller="night")
    assert delete.pending
    assert approvals.pending()[-1]["preview"].startswith("Delete in your calendar: 'Dentist'")


def test_an_event_jarvis_made_is_changed_even_by_unattended_work(env):
    env.owner = "jarvis"
    res = execute(env, "calendar_action", EDIT, caller="background")
    assert res.ok and env.calls == [("calendar_action", EDIT)]
    assert approvals.pending() == []


def test_live_with_his_words_the_meeting_is_changed(env):
    res = execute(env, "calendar_action", {**EDIT, "quote": QUOTE}, caller=policy.LIVE)
    assert res.ok and len(env.calls) == 1


def test_the_quote_is_evidence_not_an_argument(env):
    execute(env, "calendar_action", {**EDIT, "quote": QUOTE}, caller=policy.LIVE)
    assert "quote" not in env.calls[0][1]
    execute(env, "calendar_action", {**EDIT, "event_id": "other001", "quote": "words he never said"},
            caller=policy.LIVE)
    (row,) = approvals.pending()
    assert "quote" not in row["arguments"] and row["arguments"]["event_id"] == "other001"


def test_live_without_his_words_or_with_words_he_never_said_it_asks(env):
    assert execute(env, "calendar_action", EDIT, caller=policy.LIVE).pending
    assert execute(env, "calendar_action", {**EDIT, "quote": "reschedule everything to friday"},
                   caller=policy.LIVE).pending
    assert env.calls == [] and len(approvals.pending()) == 2


def test_an_owner_that_cannot_be_looked_up_is_treated_as_his(env):
    env.owner = "unknown"
    assert execute(env, "calendar_action", EDIT, caller="background").pending and env.calls == []


def test_an_event_that_is_gone_or_an_id_that_is_missing_reaches_the_tool_which_says_so(env):
    env.owner = "gone"
    assert execute(env, "calendar_action", EDIT, caller="background").ok
    res = execute(env, "calendar_action", {"action": "update_event", "title": "x"}, caller="background")
    assert res.ok and len(env.calls) == 2 and approvals.pending() == []


def test_an_approved_change_runs_once_with_the_stored_arguments(env):
    execute(env, "calendar_action", EDIT, caller="background")
    (row,) = approvals.pending()
    res = execute(env, "calendar_action", row["arguments"], caller=policy.APPROVED)
    assert res.ok and env.calls == [("calendar_action", EDIT)]


def test_reading_and_creating_never_need_anyone(env):
    for args in ({"action": "list_events"}, {"action": "check_free_busy", "start_time": "x", "end_time": "y"},
                 {"action": "create_event", "title": "Gym", "start_time": "2026-10-07T18:00"}):
        assert execute(env, "calendar_action", args, caller="background").ok
    assert approvals.pending() == []


# --- the plan ---------------------------------------------------------------------------------

def _draft():
    return plans.create_version("2026-10-05", {"blocks": []}, [
        {"kind": "gym", "start": datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc).timestamp(),
         "end": datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc).timestamp()}], created_by="owner")


APPLY = {"action": "apply", "week": "2026-10-05"}


def test_applying_a_plan_asks_unless_he_asked(env):
    pid = _draft()
    res = execute(env, "week_plan", APPLY, caller=policy.LIVE)
    assert res.pending and env.calls == []
    (row,) = approvals.pending()
    assert row["arguments"]["plan_id"] == pid, "the approval pins the exact proposal he was shown"
    assert row["preview"].startswith("Put this plan in your calendar (week of Mon 2026-10-05, 1 blocks)")
    assert "gym" in row["preview"]


def test_applying_with_his_words_runs_and_the_quote_is_not_passed_on(env):
    _draft()
    res = execute(env, "week_plan", {**APPLY, "quote": "SAID"}, caller=policy.LIVE)
    assert res.pending, "words that are not his do not count"
    policy.start_scope(["Yes, put it in my calendar."], "")
    res = execute(env, "week_plan", {**APPLY, "quote": "put it in my calendar"}, caller=policy.LIVE)
    assert res.ok
    assert env.calls[-1][1]["action"] == "apply" and "quote" not in env.calls[-1][1]


@pytest.mark.parametrize("caller", ["background", "scheduled", "night"])
def test_unattended_work_cannot_apply_a_plan(env, caller):
    _draft()
    assert execute(env, "week_plan", {**APPLY, "quote": "put it in my calendar"}, caller=caller).pending
    assert env.calls == []


def test_an_approved_apply_runs_the_proposal_that_was_shown_not_a_newer_one(env):
    first = _draft()
    execute(env, "week_plan", APPLY, caller="background")
    _draft()                                   # a newer proposal appears before he taps Approve
    (row,) = approvals.pending()
    res = execute(env, "week_plan", row["arguments"], caller=policy.APPROVED)
    assert res.ok and env.calls[-1][1]["plan_id"] == first


def test_the_other_plan_actions_are_his_own_business(env):
    for action in ("propose", "add", "change", "remove", "undo", "show", "sync"):
        assert execute(env, "week_plan", {"action": action}, caller="background").ok, action
    assert approvals.pending() == []


def test_an_apply_that_names_its_plan_is_left_alone(env):
    a, b = _draft(), _draft()
    args = {"action": "apply", "plan_id": a}
    assert weekplan.prepare_args(args) == args
    assert weekplan.prepare_args({"action": "apply", "week": "2026-10-05"})["plan_id"] == b
    assert "plan_id" not in weekplan.prepare_args({"action": "apply", "week": "2026-11-02"})
    assert weekplan.prepare_args({"action": "show"}) == {"action": "show"}


def test_a_failing_describer_or_preparer_never_stops_the_call(env, monkeypatch):
    _draft()
    monkeypatch.setattr(weekplan, "describe_apply", lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    gateway.set_describer("week_plan", weekplan.describe_apply)
    gateway.set_preparer("week_plan", lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        res = execute(env, "week_plan", APPLY, caller=policy.LIVE)
        assert res.pending
        (row,) = approvals.pending()
        assert row["preview"].startswith("week_plan apply")
    finally:
        gateway.set_describer("week_plan", weekplan.describe_apply)
        gateway.set_preparer("week_plan", weekplan.prepare_args)


# --- the pure parts of the policy ----------------------------------------------------------

def test_the_table_lowers_only_on_the_word_of_the_gateway():
    edit = {"action": "update_event"}
    assert policy.risk_of("calendar_action", edit) == policy.HIS_DATA
    d = policy.decide("calendar_action", edit, caller="background", risk=policy.OWN)
    assert d.verdict == policy.ALLOW and d.risk == policy.OWN
    d = policy.decide("calendar_action", edit, caller="background")
    assert d.verdict == policy.APPROVE and d.risk == policy.HIS_DATA
    assert policy.QUOTED_TOOLS >= {"gmail_action", "calendar_action", "week_plan"}


def test_previews_of_a_calendar_edit_without_a_label_still_say_what_will_change():
    text = policy.preview("calendar_action", {"action": "delete_event", "event_id": "abc123"})
    assert text == "Delete in your calendar: event abc123"
    text = policy.preview("calendar_action", {"action": "update_event", "event_id": "abc123", "title": "New"})
    assert text == "Change in your calendar: event abc123\nTo: title=New"
    assert policy.preview("week_plan", {"action": "apply"}) == "week_plan apply"
