"""P4 acceptance: a simulated week, fake clock and fake calendar, through the real loop.

The roadmap: "a simulated week runs every check-in, reshuffle and the review with correct records".
This drives `planloop._cycle` at chosen instants across Saturday to Sunday, answers each check-in the
way JARVIS would (`week_plan report`), lets a meeting land on a block, and checks the record --
outcomes, check-in rows, approvals, the two views -- against what the week actually was:

  Sat  the week is planned and put in the calendar (gym Mon, Thu, Fri; work weekdays)
  Mon  a board dinner lands on the gym; he taps Approve; the gym moves to Tuesday. Check-in: work done
  Tue  gym (moved) done, work partly (stopped at 16:00)
  Wed  work done
  Thu  gym skipped (tired): there is no room left this week, and he is told
  Fri  gym done; he never says how work went -- one follow-up, then unknown when the day closes
  Sun  the review: counts from the record, next week drawn as a proposal
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brain import chronos
from backend.brain import planday as D
from backend.brain import planloop as L
from backend.brain import weekplan as W
from backend.storage import approvals, checkins, plans
from backend.tools import approval_flow
from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService

LOCAL_TZ = ZoneInfo("Europe/Paris")
WEEK = "2026-10-05"
RULES = {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
         "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}], "buffer": 15,
         "blocks": [{"kind": "gym", "count": 3, "minutes": 60, "not_before": "17:00", "not_after": "20:00"}]}


def paris(d, h, mi=0):
    return datetime(2026, 10, d, h, mi, tzinfo=LOCAL_TZ)


def utc(dt):
    return dt.astimezone(timezone.utc)


def run(coro):
    return asyncio.run(coro)


class World:
    """The fake clock, the calendar, and everything the loop sent."""

    def __init__(self, svc):
        self.svc = svc
        self.now = utc(paris(3, 12))
        self.dms: list[str] = []
        self.requests: list[dict] = []

    def at(self, moment):
        self.now = utc(moment)
        return self.now

    def cycle(self, moment):
        run(L._cycle(self.at(moment)))

    def report(self, moment, day, outcomes, said=""):
        return W.run(W.Ask(action="report", day=day, outcomes=outcomes, said=said), self.at(moment))

    def tap(self, request_id, approve=True):
        return run(approval_flow.approve(request_id, "button") if approve else approval_flow.deny(request_id, "button"))


@pytest.fixture
def world(monkeypatch):
    svc = FakeCalendarService()
    w = World(svc)
    monkeypatch.setattr(cal, "_get_service", lambda: svc)
    monkeypatch.setattr(L, "_calendar_ready", lambda: True)
    monkeypatch.setattr(W, "_now", lambda: w.now)

    async def dm(text):
        w.dms.append(text)
        return True

    async def ask(row):
        w.requests.append(row)
        return f"m{row['id']}"
    L.set_notifier(dm)
    L._unsent_review.clear()
    approval_flow.set_notifier(ask)
    approval_flow.set_resolution_notifier(None)
    yield w
    L.set_notifier(None)
    L._unsent_review.clear()
    approval_flow.set_notifier(None)


def test_a_whole_week_of_check_ins_reshuffles_and_a_review(world):
    svc = world.svc
    svc.seed("Standup", paris(5, 10), paris(5, 10, 30))
    svc.seed("Dentist", paris(7, 15), paris(7, 16))
    svc.seed("Offsite", paris(8, 9), paris(8, 17))

    # -- Saturday: the week is planned and written into the calendar ---------------------------
    assert W.run(W.Ask(action="propose", rules=RULES), world.at(paris(3, 12))).startswith("Proposed")
    assert W.run(W.Ask(action="apply"), world.now).startswith("Applied")
    mon_gym_uid = next(r["uid"] for r in D.live_blocks_on(date(2026, 10, 5)) if r["kind"] == "gym")

    # -- Monday morning: the brief lists today's blocks; nothing else goes out yet -------------
    brief = chronos._collect_plan_today(paris(5, 7))
    assert brief == "- 09:00-10:00 work\n- 10:30-17:00 work\n- 17:30-18:30 gym"
    world.cycle(paris(5, 7))
    assert world.dms == [] and world.requests == []

    # -- Monday 09:00: a board dinner lands on the gym; the look asks; he taps Approve ---------
    svc.seed("Board dinner", paris(5, 17, 45), paris(5, 19))
    world.cycle(paris(5, 9))
    (req,) = world.requests
    assert req["context"] == "planner" and req["preview"] == "Move Monday's gym (17:30-18:30) to Tuesday 17:30-18:30"
    assert "Board dinner" in req["why"]
    world.cycle(paris(5, 9, 10))
    assert len(world.requests) == 1, "the same clash is not put to him twice, nor is the look repeated within 30 minutes"
    reply = world.tap(req["id"])
    assert reply.startswith(f"Approved #{req['id']}. Moved Monday's gym to Tuesday 17:30-18:30")
    assert D.live_blocks_on(date(2026, 10, 5))[-1]["kind"] == "work"
    assert [r["kind"] for r in D.live_blocks_on(date(2026, 10, 6))] == ["work", "gym"]

    # -- Monday 21:30: the check-in; he answers ------------------------------------------------
    world.cycle(paris(5, 21, 30))
    assert len(world.dms) == 1 and world.dms[0].startswith("Evening check-in, Monday 5 Oct.")
    assert "• Work 09:00-10:00, 10:30-17:00" in world.dms[0] and "Gym" not in world.dms[0]
    assert "Evening check-in" in D.prompt_line(world.at(paris(5, 21, 35)))
    out = world.report(paris(5, 21, 40), "2026-10-05", [{"kind": "work", "outcome": "done"}], "work done")
    assert "Every block that day now has an outcome." in out and D.prompt_line(world.now) == ""

    # -- Tuesday ---------------------------------------------------------------------------------
    world.cycle(paris(6, 21, 30))
    assert "• Gym 17:30-18:30" in world.dms[1] and "• Work 09:00-17:00" in world.dms[1]
    world.report(paris(6, 21, 45), "2026-10-06",
                 [{"kind": "work", "outcome": "partly", "reason": "stopped at 16:00"},
                  {"kind": "gym", "outcome": "done"}], "stopped work at 4, gym done")

    # -- Wednesday: only work --------------------------------------------------------------------
    world.cycle(paris(7, 21, 30))
    assert "Gym" not in world.dms[2]
    world.report(paris(7, 21, 40), "2026-10-07", [{"kind": "work", "outcome": "done"}], "work fine")

    # -- Thursday: the gym is skipped, and nothing fits again this week -------------------------
    world.cycle(paris(8, 21, 30))
    assert world.dms[3].startswith("Evening check-in, Thursday 8 Oct.") and "Work" not in world.dms[3]
    out = world.report(paris(8, 21, 40), "2026-10-08",
                       [{"kind": "gym", "outcome": "skipped", "reason": "tired"}], "skipped the gym, tired")
    assert "Thursday's gym (17:30-18:30): no free slot for it this week." in out
    assert len(world.requests) == 1, "no second request: nothing fits"

    # -- Friday: gym done; how work went is never said -------------------------------------------
    world.cycle(paris(9, 21, 30))
    out = world.report(paris(9, 21, 40), "2026-10-09", [{"kind": "gym", "outcome": "done"}], "gym done")
    assert "Still open: work 09:00-17:00. Ask him ONCE" in out
    assert "already asked your one question" in D.prompt_line(world.now)

    # -- Saturday 04:00: Friday closes; what nobody answered for is unknown -----------------------
    world.cycle(paris(10, 4, 5))
    fri_work = next(r for r in D.live_blocks_on(date(2026, 10, 9)) if r["kind"] == "work")
    assert plans.outcomes([fri_work["uid"]])[fri_work["uid"]]["outcome"] == "unknown"
    assert D.prompt_line(world.now) == ""

    # -- Sunday evening: the review --------------------------------------------------------------
    world.cycle(paris(11, 19))
    review = world.dms[-1]
    assert review.startswith("Weekly review, week of 5 Oct.\nPlanned against what happened: ")
    assert "gym 2 of 3 done, 1 skipped" in review
    assert "work 4 of 6 done, 1 partly, 1 unknown" in review
    draft = plans.draft("2026-10-12")
    assert draft is not None and f"proposal #{draft['id']}" in review and "nothing in your calendar yet" in review
    n_dms = len(world.dms)
    world.cycle(paris(11, 19, 5))
    assert len(world.dms) == n_dms, "one review a week"

    # ================= the record =================================================================
    rows = {r["local_date"]: r for r in checkins.recent(30)}
    assert sorted(rows) == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    assert all(r["sent_at"] and r["closed_at"] for r in rows.values())
    assert rows["2026-10-06"]["reply"] == "stopped work at 4, gym done"
    assert [p["outcome"] for p in rows["2026-10-06"]["parsed"]] == ["partly", "done"]
    assert rows["2026-10-09"]["follow_up"].startswith("still open: work")

    view = {(r["kind"], r["planned_date"]): r for r in plans.block_outcomes(week_start=WEEK)}
    assert view[("gym", "2026-10-06")]["outcome"] == "done" and view[("gym", "2026-10-06")]["times_moved"] == 1
    assert view[("gym", "2026-10-08")]["outcome"] == "skipped" and view[("gym", "2026-10-08")]["reason"] == "tired"
    assert view[("gym", "2026-10-09")]["outcome"] == "done"
    assert view[("work", "2026-10-06")]["reason"] == "stopped at 16:00"
    assert view[("work", "2026-10-09")]["outcome"] == "unknown"

    by_kind = {r["kind"]: r for r in plans.adherence(since_week=WEEK, until_week=WEEK)}
    g, wk = by_kind["gym"], by_kind["work"]
    assert (g["planned"], g["done"], g["skipped"], g["unreported"]) == (3, 2, 1, 0)
    assert (wk["planned"], wk["done"], wk["partly"], wk["unknown"], wk["unreported"]) == (6, 4, 1, 1, 0)

    approval = approvals.get(req["id"])
    assert len(approvals.recent(10)) == 1, "the week produced exactly one request"
    assert approval["state"] == approvals.EXECUTED and approval["tool"] == "week_plan"

    # the moved gym: skipped never happened to it; the Thursday skip is the week's one slip
    slips = plans.slips(since_date="2026-10-01")
    assert [s["date"] for s in slips] == ["2026-10-08"] and slips[0]["kind"] == "gym"
    # every calendar event the plan owns is where the plan says; none of his was touched
    mine = sorted((e["summary"].lower(), cal.event_span(e)) for e in svc.live() if cal.is_jarvis_made(e))
    standing = sorted((r["kind"], (W._dt(W._eff(r)[0]).astimezone(LOCAL_TZ), W._dt(W._eff(r)[1]).astimezone(LOCAL_TZ)))
                      for r in plans.applied(WEEK)["blocks"])
    assert mine == standing
    for title in ("Standup", "Dentist", "Offsite", "Board dinner"):
        assert next(e for e in svc.items.values() if e["summary"] == title)["status"] == "confirmed"
    assert mon_gym_uid in {r["uid"] for r in D.live_blocks_on(date(2026, 10, 6))}
