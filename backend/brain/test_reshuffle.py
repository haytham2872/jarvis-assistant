"""P4: keeping the plan true (backend/brain/reshuffle.py) -- a skipped block is offered a new slot, a
meeting that lands on a block gets a proposed move, and the answer is a tap (or a pre-approval)."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brain import planday as D
from backend.brain import planner as P
from backend.brain import reshuffle as R
from backend.brain import weekplan as W
from backend.storage import approvals, plans
from backend.tools import approval_flow
from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService

LOCAL_TZ = ZoneInfo("Europe/Paris")
WEEK = "2026-10-05"
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
MON_EVE = datetime(2026, 10, 5, 19, 35, tzinfo=timezone.utc)          # Monday 21:35 Paris
MON = date(2026, 10, 5)
# gym three times, from 17:00: Monday, Thursday and Friday (Tuesday is free for a make-up)
RULES = {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
         "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}], "buffer": 15,
         "blocks": [{"kind": "gym", "count": 3, "minutes": 60, "not_before": "17:00", "not_after": "20:00"}]}


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    monkeypatch.setattr(W, "_now", lambda: MON_EVE)
    return fake


@pytest.fixture
def asked(monkeypatch):
    """Requests that were put in front of him."""
    seen = []

    async def notifier(row):
        seen.append(row)
        return f"m{row['id']}"
    approval_flow.set_notifier(notifier)
    approval_flow.set_resolution_notifier(None)
    yield seen
    approval_flow.set_notifier(None)


def planned(svc, rules=None):
    svc.seed("Standup", paris(2026, 10, 5, 10), paris(2026, 10, 5, 10, 30))
    svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))
    svc.seed("Offsite", paris(2026, 10, 8, 9), paris(2026, 10, 8, 17))
    assert W.run(W.Ask(action="propose", rules=rules or RULES), NOW).startswith("Proposed")
    assert W.run(W.Ask(action="apply"), NOW).startswith("Applied")


def skip_monday_gym():
    return W.run(W.Ask(action="report", day="2026-10-05", said="skipped the gym",
                       outcomes=[{"kind": "gym", "outcome": "skipped", "reason": "tired"}]), MON_EVE)


def gym(day):
    (row,) = [r for r in D.live_blocks_on(day) if r["kind"] == "gym"]
    return row


# ===========================================================================================
# a skipped block
# ===========================================================================================

def test_a_skipped_gym_is_offered_the_next_free_slot_as_a_request(svc, asked):
    planned(svc)
    out = skip_monday_gym()
    (p,) = list(D._pending_proposals)
    assert p["label"] == "Move Monday's gym (17:30-18:30) to Tuesday 17:30-18:30"
    assert p["why"] == "you skipped it"
    assert p["args"] == {"action": "change", "week": WEEK, "kind": "gym", "day": "2026-10-05", "near": "17:30",
                         "to_day": "2026-10-06", "start": "17:30", "reason": "you skipped it"}
    assert "Recorded for Monday 5 Oct" in out
    line = run(R.request_move(D.drain_proposals()[0]))
    assert line.startswith("Asked him: Move Monday's gym") and "he taps Approve or Deny" in line
    (row,) = asked
    assert (row["tool"], row["context"], row["risk"], row["state"]) == ("week_plan", "planner", "own", "pending")
    assert row["preview"] == "Move Monday's gym (17:30-18:30) to Tuesday 17:30-18:30"
    assert row["arguments"]["to_day"] == "2026-10-06"


def test_approving_the_request_moves_the_block_and_keeps_the_skip_in_the_record(svc, asked):
    planned(svc)
    skip_monday_gym()
    run(R.request_move(D.drain_proposals()[0]))
    (row,) = asked
    old = gym(MON)
    text = run(approval_flow.approve(row["id"], "button"))
    assert text.startswith(f"Approved #{row['id']}. Moved Monday's gym to Tuesday 17:30-18:30"), text
    assert cal.event_span(svc.items[old["event_id"]]) == (paris(2026, 10, 6, 17, 30), paris(2026, 10, 6, 18, 30))
    moved = [r for r in D.live_blocks_on(date(2026, 10, 6)) if r["kind"] == "gym"]
    assert len(moved) == 1 and moved[0]["uid"] == old["uid"], "the same block, rescheduled"
    told = plans.outcomes([old["uid"]])[old["uid"]]
    assert told["outcome"] == "moved" and "rescheduled after a skip" in told["reason"]
    trail = [e["event"] for e in plans.events(block_uid=old["uid"]) if e["event"].startswith("outcome_")]
    assert trail == ["outcome_skipped", "outcome_moved"], "the skip stays a fact"
    assert [s["uid"] for s in plans.slips(since_date="2026-10-01")] == [old["uid"]]


def test_denying_it_leaves_everything_and_it_is_not_asked_again(svc, asked):
    planned(svc)
    skip_monday_gym()
    run(R.request_move(D.drain_proposals()[0]))
    run(approval_flow.deny(asked[0]["id"], "button"))
    assert D.live_blocks_on(MON)[-1]["kind"] == "gym"
    assert R.proposed(gym(MON)["uid"], "skipped 2026-10-05")
    plans_before = len(plans.versions(WEEK))
    lines = R.after_skip([gym(MON)], plans.applied(WEEK), MON, MON_EVE)
    assert lines == [] and D._pending_proposals == [] and len(plans.versions(WEEK)) == plans_before


def test_a_pre_approved_kind_is_moved_at_once_and_reported(svc, asked):
    planned(svc)
    D.set_auto_move(["gym"])
    old = gym(MON)
    out = skip_monday_gym()
    assert "Rescheduled (you pre-approved moving gym): Moved Monday's gym to Tuesday 17:30-18:30" in out
    assert "undo" not in out, "the slot it left is past: undo could not put it back"
    assert D._pending_proposals == [] and asked == []
    assert cal.event_span(svc.items[old["event_id"]])[0] == paris(2026, 10, 6, 17, 30)
    assert plans.outcomes([old["uid"]])[old["uid"]]["outcome"] == "moved"


def test_a_pre_approval_for_another_kind_does_not_cover_gym(svc, asked):
    planned(svc)
    D.set_auto_move(["reading"])
    skip_monday_gym()
    assert len(D._pending_proposals) == 1
    D._pending_proposals.clear()


def test_when_nothing_fits_he_is_told_and_nothing_is_asked(svc, asked):
    planned(svc, {**RULES, "blocks": [{"kind": "gym", "count": 4, "minutes": 60, "not_before": "17:00",
                                        "not_after": "20:00"}]})
    out = W.run(W.Ask(action="report", day="2026-10-06", said="no gym",
                      outcomes=[{"kind": "gym", "outcome": "skipped"}]),
                datetime(2026, 10, 6, 19, 35, tzinfo=timezone.utc))
    assert "Tuesday's gym (17:30-18:30): no free slot for it this week." in out
    assert "No free slot for the skipped" not in out                        # (it used to say it twice)
    assert D._pending_proposals == [] and asked == []
    assert R.proposed(gym(date(2026, 10, 6))["uid"], "skipped 2026-10-06"), "and it is not tried again"


def test_work_is_never_rescheduled_after_a_skip(svc, asked):
    planned(svc)
    out = W.run(W.Ask(action="report", day="2026-10-05", outcomes=[{"kind": "work", "outcome": "skipped"}]), MON_EVE)
    assert "work 09:00-10:00: skipped" in out
    assert D._pending_proposals == [] and "Rescheduled" not in out


def test_a_partly_done_block_is_not_offered_a_new_slot(svc, asked):
    planned(svc)
    W.run(W.Ask(action="report", day="2026-10-05", outcomes=[{"kind": "gym", "outcome": "partly"}]), MON_EVE)
    assert D._pending_proposals == []


def test_no_calendar_means_no_proposal_and_a_line_that_says_why(svc, asked, monkeypatch):
    planned(svc)

    def down():
        raise W._Fail("the calendar is not available (offline); nothing was planned or changed")
    monkeypatch.setattr(W, "_svc", down)
    out = skip_monday_gym()
    assert "I could not read your calendar to find a new slot" in out
    assert D._pending_proposals == []


def test_the_report_tool_wrapper_puts_the_request_to_him(svc, asked):
    planned(svc)
    out = run(W.week_plan(action="report", day="2026-10-05", said="skipped it",
                          outcomes=[{"kind": "gym", "outcome": "skipped"}]))
    assert "Asked him: Move Monday's gym (17:30-18:30) to Tuesday 17:30-18:30 (request #" in out
    assert len(asked) == 1 and D._pending_proposals == []


def test_a_request_that_cannot_be_recorded_is_said_in_words(svc, asked, monkeypatch):
    planned(svc)
    skip_monday_gym()
    monkeypatch.setattr(approvals, "create", lambda **kw: None)
    line = run(R.request_move(D.drain_proposals()[0]))
    assert line.startswith("I could not record the request (Move Monday's gym") and "tell him the option in words" in line


# ===========================================================================================
# a meeting lands on a block
# ===========================================================================================

def test_a_new_meeting_on_a_block_is_found_with_the_slot_it_could_move_to(svc):
    planned(svc)                                                        # gym Mon, Thu, Fri
    svc.seed("Board dinner", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))
    (m,) = R.scan(NOW)
    assert m.kind == "gym" and m.why == "'Board dinner' now overlaps it (Thu 17:45-19:00)"
    assert m.label == "Move Thursday's gym (17:30-18:30) to Tuesday 17:30-18:30", "the day nearest with room"
    assert m.args["to_day"] == "2026-10-06" and m.args["day"] == "2026-10-08" and m.args["near"] == "17:30"
    assert m.key == "clash Board dinner 2026-10-08 17:45"


def test_a_meeting_that_does_not_touch_a_block_is_left_alone(svc):
    planned(svc)
    svc.seed("Lunch", paris(2026, 10, 8, 12), paris(2026, 10, 8, 13))
    svc.seed("Late call", paris(2026, 10, 9, 18, 30), paris(2026, 10, 9, 19))       # after the gym ends at 18:30
    assert R.scan(NOW) == []


def test_the_plans_own_blocks_are_not_a_clash_and_work_is_never_reshuffled(svc):
    planned(svc)
    svc.seed("Sync", paris(2026, 10, 6, 11), paris(2026, 10, 6, 12))                 # inside Tuesday's work block
    assert R.scan(NOW) == []


def test_a_free_or_declined_event_is_not_a_clash(svc):
    planned(svc)
    ev = svc.seed("Maybe drinks", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))
    svc.items[ev["id"]]["transparency"] = "transparent"
    ev2 = svc.seed("Declined", paris(2026, 10, 9, 17, 45), paris(2026, 10, 9, 19))
    svc.items[ev2["id"]]["attendees"] = [{"self": True, "responseStatus": "declined"}]
    assert R.scan(NOW) == []


def test_a_block_already_over_is_not_looked_at(svc):
    planned(svc)
    svc.seed("Board dinner", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))
    assert R.scan(datetime(2026, 10, 8, 17, 50, tzinfo=timezone.utc)) == [], "19:50 Paris: the gym is over"


def test_a_clash_is_proposed_once(svc, asked):
    planned(svc)
    svc.seed("Board dinner", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))
    moves = R.scan(NOW)
    said = run(R.handle(moves, NOW, lambda t: asyncio.sleep(0, True)))
    assert len(said) == 1 and said[0].startswith("Asked him: Move Thursday's gym")
    assert R.scan(NOW) == [], "the same clash is not put to him twice, whatever he answers"
    svc.seed("Other thing", paris(2026, 10, 8, 18), paris(2026, 10, 8, 19))
    assert len(R.scan(NOW)) == 1, "a different clash on the same block is a new question"


def test_a_clash_with_no_room_is_said_once_in_words(svc, asked):
    planned(svc, {**RULES, "blocks": [{"kind": "gym", "count": 4, "minutes": 60, "not_before": "17:00",
                                        "not_after": "20:00"}]})
    svc.seed("Board dinner", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))
    told = []

    async def notify(text):
        told.append(text)
        return True
    said = run(R.handle(R.scan(NOW), NOW, notify))
    assert told == ["Thursday's gym (17:30-18:30): no free slot for it this week, and 'Board dinner' now overlaps it "
                    "(Thu 17:45-19:00)."] and said == told
    assert asked == [] and R.scan(NOW) == []


def test_a_pre_approved_clash_is_moved_and_he_is_told_why(svc, asked):
    planned(svc)
    D.set_auto_move(["gym"])
    svc.seed("Board dinner", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))
    told = []

    async def notify(text):
        told.append(text)
        return True
    old = gym(date(2026, 10, 8))
    run(R.handle(R.scan(NOW), NOW, notify))
    assert asked == []
    (msg,) = told
    assert msg.startswith("Moved Thursday's gym to Tuesday 17:30-18:30") and "'Board dinner' now overlaps it" in msg
    assert "You pre-approved moving gym; say undo to put it back." in msg
    assert cal.event_span(svc.items[old["event_id"]])[0] == paris(2026, 10, 6, 17, 30)


def test_at_most_three_questions_from_one_look(svc):
    planned(svc, {**RULES, "blocks": [{"kind": "gym", "count": 3, "minutes": 60, "not_before": "17:00",
                                        "not_after": "20:00"},
                                       {"kind": "reading", "count": 3, "minutes": 30, "not_before": "20:00",
                                        "not_after": "22:00", "days": "mon-fri"}]})
    for d in (5, 6, 7, 8, 9):
        svc.seed(f"Late {d}", paris(2026, 10, d, 17, 30), paris(2026, 10, d, 22))
    assert len(R.scan(NOW)) == 3


def test_every_week_with_a_plan_is_looked_at(svc):
    planned(svc)
    next_week = date(2026, 10, 12)
    assert W.run(W.Ask(action="propose", week="2026-10-12", rules=RULES), NOW).startswith("Proposed")
    assert W.run(W.Ask(action="apply", week="2026-10-12"), NOW).startswith("Applied")
    (g,) = [r for r in plans.applied("2026-10-12")["blocks"] if r["kind"] == "gym"][:1]
    start = P.local(W._dt(g["start_at"]))
    svc.seed("Offsite 2", start + timedelta(minutes=15), start + timedelta(minutes=90))
    moves = R.scan(NOW)
    assert moves and all(m.args.get("week") == "2026-10-12" for m in moves)
    assert next_week.weekday() == 0


def test_slot_days_run_forward_from_a_skip_and_outward_from_a_clash():
    assert R.days_after(date(2026, 10, 6), date(2026, 10, 5)) == [date(2026, 10, 6 + k) for k in range(6)]
    assert R.days_after(date(2026, 10, 4), date(2026, 10, 6))[0] == date(2026, 10, 6), "not into the past"
    assert R.days_around(date(2026, 10, 8), date(2026, 10, 5))[:4] == [
        date(2026, 10, 8), date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 6)]
    assert min(R.days_around(date(2026, 10, 8), date(2026, 10, 7))) == date(2026, 10, 7)


def test_a_proposal_left_over_from_earlier_is_not_put_to_him_with_this_report(svc, asked):
    planned(svc)
    D._pending_proposals.append({"args": {"action": "change", "kind": "gym", "day": "2026-10-05", "week": WEEK},
                                 "label": "Move a stale thing", "why": "long ago", "uid": "stale"})
    out = run(W.week_plan(action="report", day="2026-10-05", said="did the gym",
                          outcomes=[{"kind": "gym", "outcome": "done"}]))
    assert "Move a stale thing" not in out and asked == [] and D._pending_proposals == []
