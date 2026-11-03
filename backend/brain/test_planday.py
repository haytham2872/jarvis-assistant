"""P4: the day in the plan (backend/brain/planday.py) -- the check-in text, reporting outcomes, closing a
day, the per-turn line, `/plan`. Against the calendar test double, like test_weekplan.py."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brain import planday as D
from backend.brain import planner as P
from backend.brain import weekplan as W
from backend.storage import checkins, plans
from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService

LOCAL_TZ = ZoneInfo("Europe/Paris")
WEEK = "2026-10-05"                                         # a Monday
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)      # the Thursday before
TUE = date(2026, 10, 6)
TUE_EVE = datetime(2026, 10, 6, 19, 35, tzinfo=timezone.utc)       # Tuesday 21:35 Paris
RULES = {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
         "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}], "buffer": 15}
GYM4 = {"blocks": [{"kind": "gym", "count": 4, "minutes": 60, "not_after": "20:00"}]}


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    return fake


def planned(svc, rules=None):
    """The standard week, in the calendar: work weekdays, gym Mon/Tue/Thu/Fri at 17:30."""
    svc.seed("Standup", paris(2026, 10, 5, 10), paris(2026, 10, 5, 10, 30))
    svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))
    svc.seed("Offsite", paris(2026, 10, 8, 9), paris(2026, 10, 8, 17))
    assert W.run(W.Ask(action="propose", rules=rules or {**RULES, **GYM4}), NOW).startswith("Proposed")
    assert W.run(W.Ask(action="apply"), NOW).startswith("Applied")


def report(outcomes, said="", day="", now=TUE_EVE):
    return W.run(W.Ask(action="report", outcomes=outcomes, said=said, day=day), now)


def tue_blocks():
    return D.live_blocks_on(TUE)


def block(kind, day=TUE):
    (row,) = [r for r in D.live_blocks_on(day) if r["kind"] == kind]
    return row


def send_checkin(day=TUE, at=None):
    """What the loop does: claim the day's row, mark it sent."""
    text, uids = D.checkin_payload(day)
    at = at or datetime(2026, 10, 6, 19, 30, tzinfo=timezone.utc).timestamp()
    assert checkins.claim(day.isoformat(), uids, now=at)
    checkins.mark_sent(day.isoformat(), now=at)
    return text


# ===========================================================================================
# settings
# ===========================================================================================

@pytest.mark.parametrize("text,expected", [("21:30", "21:30"), ("9:05", "09:05"), (" 07:00 ", "07:00"),
                                           ("24:00", None), ("9:5", None), ("noon", None), (None, None), ("", None)])
def test_a_time_is_hh_mm_or_nothing(text, expected):
    assert D.clean_time(text) == expected


def test_the_defaults_are_a_21_30_check_in_and_a_sunday_19_00_review():
    assert D.checkin_config() == (True, "21:30") and D.review_config() == (True, "19:00")
    plans.set_setting("checkin_time", "22:15")
    plans.set_setting("checkin_enabled", False)
    assert D.checkin_config() == (False, "22:15")
    plans.set_setting("review_time", "garbage")
    assert D.review_config() == (True, "19:00"), "an unreadable time falls back to the default"


def test_auto_move_takes_lowercase_kinds_and_refuses_work():
    assert D.auto_move_kinds() == []
    assert D.set_auto_move(["Gym", "reading", "gym"]) == ["gym", "reading"]
    assert D.auto_move_kinds() == ["gym", "reading"]
    assert D.set_auto_move("gym, walk") == ["gym", "walk"]
    assert D.set_auto_move([]) == [] and D.auto_move_kinds() == []
    for bad in (["work"], ["Not A Kind!"], 5):
        with pytest.raises(ValueError):
            D.set_auto_move(bad)


# ===========================================================================================
# the check-in
# ===========================================================================================

def test_the_check_in_lists_the_days_blocks_grouped_by_kind(svc):
    planned(svc)
    text, uids = D.checkin_payload(TUE)
    assert text.startswith("Evening check-in, Tuesday 6 Oct. Today's plan:")
    assert "• Work 09:00-17:00" in text and "• Gym 17:30-18:30" in text
    assert "gym done, stopped work at 4, skipped the reading" in text
    assert len(uids) == 2
    monday, _ = D.checkin_payload(date(2026, 10, 5))
    assert "• Work 09:00-10:00, 10:30-17:00" in monday, "a split day is one line per kind"


def test_no_check_in_on_a_day_with_nothing_planned_or_no_plan(svc):
    assert D.checkin_payload(TUE) is None                                   # no plan at all
    planned(svc)
    assert D.checkin_payload(date(2026, 10, 10)) is None                    # Saturday: no blocks
    assert D.checkin_payload(date(2026, 10, 8)) is not None                 # Thursday: only gym


def test_no_check_in_when_he_already_told_me_everything(svc):
    planned(svc)
    report([{"kind": "work", "outcome": "done"}, {"kind": "gym", "outcome": "done"}])
    assert D.checkin_payload(TUE) is None


def test_a_block_he_deleted_himself_is_not_in_the_check_in(svc):
    planned(svc)
    gym = block("gym")
    svc.items[gym["event_id"]]["status"] = "cancelled"
    W.run(W.Ask(action="sync"), NOW)
    text, _ = D.checkin_payload(TUE)
    assert "Gym" not in text and "• Work" in text


def test_a_block_he_moved_shows_where_it_really_is(svc):
    planned(svc)
    gym = block("gym")
    ev = svc.items[gym["event_id"]]
    ev["start"] = {"dateTime": paris(2026, 10, 6, 7).isoformat(), "timeZone": "Europe/Paris"}
    ev["end"] = {"dateTime": paris(2026, 10, 6, 8).isoformat(), "timeZone": "Europe/Paris"}
    W.run(W.Ask(action="sync"), NOW)
    text, _ = D.checkin_payload(TUE)
    assert "• Gym 07:00-08:00" in text


# ===========================================================================================
# reporting what happened
# ===========================================================================================

def test_each_outcome_is_matched_to_a_real_block_and_recorded(svc):
    planned(svc)
    out = report([{"kind": "gym", "outcome": "done", "reason": "felt great"},
                  {"kind": "work", "outcome": "partly", "reason": "stopped at 16:00"}], said="gym done, stopped work at 4")
    assert out.startswith("Recorded for Tuesday 6 Oct:")
    assert "- gym 17:30-18:30: done (felt great)" in out
    assert "- work 09:00-17:00: partly (stopped at 16:00)" in out
    assert "Every block that day now has an outcome." in out
    got = plans.outcomes([r["uid"] for r in tue_blocks()])
    assert {v["outcome"] for v in got.values()} == {"done", "partly"}
    assert got[block("gym")["uid"]]["reason"] == "felt great" and got[block("gym")["uid"]]["actor"] == "owner"


def test_outcomes_after_a_check_in_are_credited_to_the_check_in_and_keep_his_words(svc):
    planned(svc)
    send_checkin()
    report([{"kind": "gym", "outcome": "skipped", "reason": "tired"}], said="skipped the gym, I was tired")
    assert plans.outcomes([block("gym")["uid"]])[block("gym")["uid"]]["actor"] == "checkin"
    row = checkins.get(TUE.isoformat())
    assert row["reply"] == "skipped the gym, I was tired" and row["replied_at"]
    assert [p["outcome"] for p in row["parsed"]] == ["skipped"]


def test_an_outcome_for_a_block_that_is_not_planned_is_refused_with_the_days_blocks(svc):
    planned(svc)
    out = report([{"kind": "reading", "outcome": "done"}])
    assert "Not recorded:" in out and "no reading is planned that day (the day has: work 09:00-17:00; gym 17:30-18:30)" in out
    assert plans.outcomes([r["uid"] for r in tue_blocks()]) == {}


def test_an_unknown_outcome_word_is_refused(svc):
    planned(svc)
    out = report([{"kind": "gym", "outcome": "crushed it"}])
    assert "outcome must be one of done, partly, skipped, moved, unknown" in out
    assert plans.outcomes([block("gym")["uid"]]) == {}


def test_two_blocks_of_a_kind_need_the_time_and_work_covers_all_its_pieces(svc):
    planned(svc)
    monday = date(2026, 10, 5)
    mon_eve = datetime(2026, 10, 5, 19, 35, tzinfo=timezone.utc)
    out = W.run(W.Ask(action="report", day="2026-10-05", outcomes=[{"kind": "work", "outcome": "done"}]), mon_eve)
    assert "- work 09:00-10:00: done" in out and "- work 10:30-17:00: done" in out
    # add a second gym on Monday so the kind is ambiguous
    W.run(W.Ask(action="add", rules={"blocks": [{"kind": "reading", "count": 2, "minutes": 30, "days": "mon", "max_per_day": 2}]}), NOW)
    W.run(W.Ask(action="apply"), NOW)
    readings = [r for r in D.live_blocks_on(monday) if r["kind"] == "reading"]
    assert len(readings) == 2
    out = W.run(W.Ask(action="report", day="2026-10-05", outcomes=[{"kind": "reading", "outcome": "done"}]), mon_eve)
    assert "there are 2 reading blocks that day" in out and "say which with near" in out
    near = f"{D._hm(W._eff(readings[1])[0])}"
    out = W.run(W.Ask(action="report", day="2026-10-05",
                      outcomes=[{"kind": "reading", "outcome": "skipped", "near": near}]), mon_eve)
    told = plans.outcomes([r["uid"] for r in readings])
    assert set(told) == {readings[1]["uid"]}, "only the block near that time"


def test_near_must_be_a_time_and_close_to_a_block(svc):
    planned(svc)
    assert "near must be a time" in report([{"kind": "gym", "outcome": "done", "near": "evening"}])
    assert "no gym starts near 09:00" in report([{"kind": "gym", "outcome": "done", "near": "09:00"}])


def test_reporting_the_same_thing_twice_records_it_once(svc):
    planned(svc)
    report([{"kind": "gym", "outcome": "done", "reason": "good"}])
    out = report([{"kind": "gym", "outcome": "done", "reason": "good"}])
    assert "(already recorded)" in out
    trail = [e for e in plans.events(block_uid=block("gym")["uid"]) if e["event"].startswith("outcome_")]
    assert len(trail) == 1


def test_a_later_correction_is_a_new_fact_and_wins(svc):
    planned(svc)
    report([{"kind": "gym", "outcome": "skipped"}])
    report([{"kind": "gym", "outcome": "done", "reason": "went after all"}])
    assert plans.outcomes([block("gym")["uid"]])[block("gym")["uid"]]["outcome"] == "done"
    assert len([e for e in plans.events(block_uid=block("gym")["uid"]) if e["event"].startswith("outcome_")]) == 2


def test_a_report_needs_outcomes_a_real_day_and_a_day_that_has_happened(svc):
    planned(svc)
    assert report(None).startswith("Error: say what happened")
    assert report([]).startswith("Error: say what happened")
    assert "outcomes must be a list" in report("not json")
    assert report([{"kind": "gym", "outcome": "done"}], day="2026-10-04").startswith("Error: there are no planned blocks")
    assert "has not happened yet" in report([{"kind": "gym", "outcome": "done"}], day="2026-10-09")
    assert "can't read the day" in report([{"kind": "gym", "outcome": "done"}], day="whenever")
    too_many = [{"kind": "gym", "outcome": "done"}] * 13
    assert "at most 12" in report(too_many)


def test_outcomes_may_be_given_as_json_text_or_a_single_object(svc):
    planned(svc)
    assert "gym 17:30-18:30: done" in report('[{"kind": "gym", "outcome": "done"}]')
    assert "work 09:00-17:00: partly" in report({"kind": "work", "outcome": "partly"})


def test_yesterday_and_a_weekday_name_are_days(svc):
    planned(svc)
    wed_morning = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    out = W.run(W.Ask(action="report", day="yesterday", outcomes=[{"kind": "gym", "outcome": "done"}]), wed_morning)
    assert out.startswith("Recorded for Tuesday 6 Oct")
    out = W.run(W.Ask(action="report", day="mon", outcomes=[{"kind": "gym", "outcome": "skipped"}]), wed_morning)
    assert out.startswith("Recorded for Monday 5 Oct")


def test_without_a_day_the_report_is_about_the_check_in_that_is_waiting(svc):
    planned(svc)
    send_checkin()
    after_midnight = datetime(2026, 10, 6, 22, 40, tzinfo=timezone.utc)          # Wednesday 00:40 Paris
    out = W.run(W.Ask(action="report", outcomes=[{"kind": "gym", "outcome": "done"}]), after_midnight)
    assert out.startswith("Recorded for Tuesday 6 Oct"), "his reply after midnight is still about Tuesday"


# ---- the follow-up question and the unknowns ------------------------------------------------

def test_a_block_left_open_after_a_check_in_gets_one_follow_up_and_then_none(svc):
    planned(svc)
    send_checkin()
    first = report([{"kind": "work", "outcome": "done"}])
    assert "Still open: gym 17:30-18:30. Ask him ONCE" in first
    assert checkins.get(TUE.isoformat())["follow_up"].startswith("still open: gym")
    second = report([{"kind": "work", "outcome": "done", "reason": "and a bit more"}])
    assert "You already asked once today; leave them" in second and "ONCE" not in second


def test_without_a_check_in_the_open_blocks_are_just_listed(svc):
    planned(svc)
    out = report([{"kind": "work", "outcome": "done"}])
    assert "Not reported yet: gym 17:30-18:30." in out and "Ask him" not in out
    assert checkins.get(TUE.isoformat()) is None, "a volunteered report does not invent a check-in"


def test_when_the_day_closes_what_nobody_answered_for_is_unknown(svc):
    planned(svc)
    send_checkin()
    report([{"kind": "work", "outcome": "done"}])
    row = checkins.get(TUE.isoformat())
    n = D.close_day(row, datetime(2026, 10, 7, 4, 5, tzinfo=LOCAL_TZ))
    assert n == 1
    told = plans.outcomes([r["uid"] for r in tue_blocks()])
    gym = told[block("gym")["uid"]]
    assert gym["outcome"] == "unknown" and gym["actor"] == "system" and "no answer" in gym["reason"]
    assert told[block("work")["uid"]]["outcome"] == "done", "what he did say is not overwritten"
    assert checkins.get(TUE.isoformat())["closed_at"]


def test_a_day_that_was_never_asked_about_is_closed_without_inventing_unknowns(svc):
    planned(svc)
    checkins.claim(TUE.isoformat(), [r["uid"] for r in tue_blocks()], now=TUE_EVE.timestamp())      # claimed, never sent
    n = D.close_day(checkins.get(TUE.isoformat()), datetime(2026, 10, 7, 5, 0, tzinfo=LOCAL_TZ))
    assert n == 0 and plans.outcomes([r["uid"] for r in tue_blocks()]) == {}
    assert checkins.get(TUE.isoformat())["closed_at"]


def test_a_day_closes_at_four_the_next_morning_or_two_days_later():
    row = {"local_date": "2026-10-06"}
    assert not D.closable(row, datetime(2026, 10, 6, 23, 59, tzinfo=LOCAL_TZ))
    assert not D.closable(row, datetime(2026, 10, 7, 0, 30, tzinfo=LOCAL_TZ)), "his reply after midnight still counts"
    assert not D.closable(row, datetime(2026, 10, 7, 3, 59, tzinfo=LOCAL_TZ))
    assert D.closable(row, datetime(2026, 10, 7, 4, 0, tzinfo=LOCAL_TZ))
    assert D.closable(row, datetime(2026, 10, 8, 0, 5, tzinfo=LOCAL_TZ)), "a backend that was off closes late days"


# ===========================================================================================
# the line JARVIS reads every turn
# ===========================================================================================

def test_no_line_without_a_waiting_check_in(svc):
    planned(svc)
    assert D.prompt_line(TUE_EVE) == ""
    checkins.claim(TUE.isoformat(), [], now=TUE_EVE.timestamp())               # claimed, not sent
    assert D.prompt_line(TUE_EVE) == ""


def test_the_line_names_the_check_in_and_what_is_still_open(svc):
    planned(svc)
    send_checkin()
    line = D.prompt_line(TUE_EVE)
    assert "I asked him at 21:30 how Tuesday 6 Oct went" in line
    assert "work 09:00-17:00; gym 17:30-18:30" in line
    assert "week_plan report" in line and "said" in line and "Never guess an outcome" in line
    assert "at most one short question" in line
    report([{"kind": "work", "outcome": "done"}])
    line = D.prompt_line(TUE_EVE)
    assert "gym 17:30-18:30" in line and "work 09:00-17:00 yet" not in line
    assert "He has already told you about the others." in line
    checkins.note_followup(TUE.isoformat(), "x")
    assert "already asked your one question today" in D.prompt_line(TUE_EVE)


def test_the_line_goes_when_everything_is_answered(svc):
    planned(svc)
    send_checkin()
    report([{"kind": "work", "outcome": "done"}, {"kind": "gym", "outcome": "done"}])
    assert D.prompt_line(TUE_EVE) == ""


def test_the_line_goes_when_the_check_in_is_stale(svc):
    planned(svc)
    send_checkin(date(2026, 10, 5), at=datetime(2026, 10, 5, 19, 30, tzinfo=timezone.utc).timestamp())
    assert D.prompt_line(datetime(2026, 10, 5, 19, 40, tzinfo=timezone.utc)) != ""
    assert D.prompt_line(datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)) == "", "older than 20 hours"


def test_the_line_never_raises(monkeypatch):
    monkeypatch.setattr(checkins, "waiting", lambda **kw: (_ for _ in ()).throw(RuntimeError("no db")))
    assert D.prompt_line(TUE_EVE) == ""


def test_the_runtime_identity_carries_the_check_in_line(monkeypatch):
    from backend.brain import runtime_status
    monkeypatch.setattr(D, "prompt_line", lambda now=None: "Evening check-in: waiting.")
    assert "Evening check-in: waiting." in runtime_status.runtime_identity_text()
    monkeypatch.setattr(D, "prompt_line", lambda now=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert runtime_status.runtime_identity_text().startswith("[Runtime identity]")


# ===========================================================================================
# reading the plan back
# ===========================================================================================

def test_todays_blocks_for_the_brief_are_one_line_each(svc):
    planned(svc)
    text = D.todays_blocks_text(datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc))
    assert text == "- 09:00-17:00 work\n- 17:30-18:30 gym"
    assert D.todays_blocks_text(datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)) == ""


def test_plan_shows_today_with_outcomes_then_the_week(svc):
    planned(svc)
    report([{"kind": "gym", "outcome": "skipped", "reason": "tired"}])
    out = D.handle_plan_command("", TUE_EVE)
    assert out.startswith("Today, Tuesday 6 Oct\n  09:00-17:00 work\n  17:30-18:30 gym  (skipped, tired)")
    assert "Week of Mon 2026-10-05" in out and "Tue 2026-10-06  <- today" in out
    assert "Evening check-in: every day at 21:30" in out and "Weekly review: Sundays at 19:00" in out
    assert "Pre-approved moves: none" in out


def test_plan_week_and_stats(svc):
    planned(svc)
    report([{"kind": "gym", "outcome": "done"}, {"kind": "work", "outcome": "done"}])
    week = D.handle_plan_command("week", TUE_EVE)
    assert week.startswith("Week of Mon 2026-10-05 (plan version 1)") and "gym  (done)" in week
    stats = D.handle_plan_command("stats", TUE_EVE)
    assert "Week of 5 Oct:" in stats and "gym 1 of 4 done, 3 not reported" in stats
    assert "work 1 of 5" in stats or "work 1 of 6" in stats


def test_plan_before_any_plan_says_so(svc):
    out = D.handle_plan_command("", TUE_EVE)
    assert out.startswith("There is no plan in your calendar yet")
    assert D.handle_plan_command("stats", TUE_EVE) == "No plan history in the last few weeks yet."


def test_plan_switches_for_the_check_in_and_the_review():
    out = D.handle_plan_command("checkin 22:00")
    assert out == "Evening check-in set to 22:00 local time." and D.checkin_config() == (True, "22:00")
    assert D.handle_plan_command("checkin off") == "Evening check-in off." and D.checkin_config()[0] is False
    assert D.handle_plan_command("checkin on") == "Evening check-in on." and D.checkin_config()[0] is True
    assert "every day at 22:00" in D.handle_plan_command("checkin")
    assert D.handle_plan_command("review 18:30") == "Weekly review set to 18:30 local time."
    assert D.review_config() == (True, "18:30")
    assert D.handle_plan_command("review off") == "Weekly review off."
    assert "Weekly review: off" in D.handle_plan_command("review")
    assert "isn't a time or on/off" in D.handle_plan_command("checkin tomorrow")
    assert "isn't something I understand" in D.handle_plan_command("nonsense") and "Commands:" in D.handle_plan_command("nonsense")


# ===========================================================================================
# what /activity shows
# ===========================================================================================

TUE_MORNING = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc).timestamp()          # Tuesday 09:00 Paris


def test_activity_lists_the_next_check_in_the_review_and_the_look(svc):
    planned(svc)
    items = D.scheduled_items(TUE_MORNING)
    texts = {t: ts for ts, t in items}
    check = next(t for t in texts if t.startswith("evening check-in for Tuesday 6 Oct"))
    assert "(2 planned block(s))" in check and texts[check] == paris(2026, 10, 6, 21, 30).timestamp()
    review = next(t for t in texts if t.startswith("weekly review"))
    assert texts[review] == paris(2026, 10, 11, 19, 0).timestamp()
    look = next(t for t in texts if t.startswith("look at your calendar"))
    assert texts[look] == TUE_MORNING and "every 30 minutes, 07:00-23:00" in look


def test_nothing_is_listed_when_there_is_no_plan_to_keep_true(svc):
    assert D.scheduled_items(TUE_MORNING) == []


def test_the_next_check_in_moves_on_once_today_s_is_sent_and_skips_empty_days(svc):
    planned(svc)
    send_checkin()
    (ts, text) = next(i for i in D.scheduled_items(TUE_MORNING) if i[1].startswith("evening check-in"))
    assert "Wednesday 7 Oct" in text and ts == paris(2026, 10, 7, 21, 30).timestamp()
    friday_night = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc).timestamp()
    nxt = [i for i in D.scheduled_items(friday_night) if i[1].startswith("evening check-in")]
    assert "Friday 9 Oct" in nxt[0][1], "Friday's has not gone out yet at 23:00 Paris"


def test_a_switched_off_check_in_or_review_is_not_listed(svc):
    planned(svc)
    plans.set_setting("checkin_enabled", False)
    plans.set_setting("review_enabled", False)
    texts = [t for _, t in D.scheduled_items(TUE_MORNING)]
    assert not any(t.startswith("evening check-in") or t.startswith("weekly review") for t in texts)


def test_a_review_already_sent_this_week_is_next_sunday(svc):
    planned(svc)
    plans.set_setting("last_review_week", "2026-10-05")
    review = next(i for i in D.scheduled_items(TUE_MORNING) if i[1].startswith("weekly review"))
    assert review[0] == paris(2026, 10, 18, 19, 0).timestamp()


def test_the_next_look_waits_half_an_hour_and_never_lands_at_night(svc):
    planned(svc)
    plans.set_setting("last_scan_at", TUE_MORNING)
    look = next(i for i in D.scheduled_items(TUE_MORNING + 60) if i[1].startswith("look at"))
    assert look[0] == TUE_MORNING + 1800
    night = datetime(2026, 10, 6, 21, 30, tzinfo=timezone.utc).timestamp()               # 23:30 Paris
    look = next(i for i in D.scheduled_items(night) if i[1].startswith("look at"))
    assert look[0] == paris(2026, 10, 7, 7, 0).timestamp()


def test_finished_check_ins_show_how_much_of_the_day_was_answered(svc):
    planned(svc)
    send_checkin()
    report([{"kind": "work", "outcome": "done"}])
    (ts, text) = D.done_items(TUE_EVE.timestamp() - 3600)[0]
    assert text == "evening check-in for Tuesday 6 Oct: 1 of 2 block(s) answered"
    D.close_day(checkins.get(TUE.isoformat()), datetime(2026, 10, 7, 5, 0, tzinfo=LOCAL_TZ))
    assert D.done_items(TUE_EVE.timestamp() - 3600)[0][1].endswith("2 of 2 block(s) answered (closed)")
    assert D.done_items(TUE_EVE.timestamp() + 86400) == []


def test_the_activity_view_carries_both(svc, monkeypatch):
    from backend.brain import activity, chronos, research
    planned(svc)
    send_checkin()
    monkeypatch.setattr(chronos, "agenda", lambda: {"reminders": [], "tasks": [], "night": [], "done": [],
                                                    "brief": {"enabled": False, "time": "08:00", "last_date": ""}})
    monkeypatch.setattr(research, "snapshot", lambda: {"pace": "off", "next_at": None, "queued": [], "passes": []})
    scheduled = "\n".join(activity._scheduled(TUE_MORNING))
    assert "weekly review, and next week drawn as a proposal" in scheduled
    assert "evening check-in for Wednesday 7 Oct" in scheduled
    done = "\n".join(activity._done(TUE_EVE.timestamp() + 600))
    assert "evening check-in for Tuesday 6 Oct: 0 of 2 block(s) answered" in done


def test_a_failure_in_the_plan_lines_never_costs_the_rest_of_the_view(svc, monkeypatch):
    from backend.brain import activity, chronos, research
    monkeypatch.setattr(chronos, "agenda", lambda: {"reminders": [], "tasks": [], "night": [], "done": [],
                                                    "brief": {"enabled": False, "time": "08:00", "last_date": ""}})
    monkeypatch.setattr(research, "snapshot", lambda: {"pace": "normal", "next_at": None, "queued": [], "passes": []})
    monkeypatch.setattr(D, "scheduled_items", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    assert any("my own research" in line for line in activity._scheduled(TUE_MORNING))


# ===========================================================================================
# gaps that mutation testing found
# ===========================================================================================

def test_a_block_he_moved_to_another_day_belongs_to_that_day(svc):
    planned(svc)
    gym = block("gym")                                                        # Tuesday 17:30
    ev = svc.items[gym["event_id"]]
    ev["start"] = {"dateTime": paris(2026, 10, 7, 7).isoformat(), "timeZone": "Europe/Paris"}
    ev["end"] = {"dateTime": paris(2026, 10, 7, 8).isoformat(), "timeZone": "Europe/Paris"}
    W.run(W.Ask(action="sync"), NOW)
    assert [r["kind"] for r in D.live_blocks_on(TUE)] == ["work"]
    wed = D.live_blocks_on(date(2026, 10, 7))
    assert [(r["kind"], D._span(r)) for r in wed if r["kind"] == "gym"] == [("gym", "07:00-08:00")]
    text, _ = D.checkin_payload(date(2026, 10, 7))
    assert "• Gym 07:00-08:00" in text


def test_stats_cover_the_last_four_weeks_newest_first(svc):
    planned(svc)
    assert W.run(W.Ask(action="propose", week="2026-10-12", rules={**RULES, **GYM4}), NOW).startswith("Proposed")
    assert W.run(W.Ask(action="apply", week="2026-10-12"), NOW).startswith("Applied")
    report([{"kind": "gym", "outcome": "done"}], day="2026-10-06")
    W.run(W.Ask(action="report", day="2026-10-13", outcomes=[{"kind": "gym", "outcome": "skipped"}]),
          datetime(2026, 10, 13, 19, 35, tzinfo=timezone.utc))
    lines = D.stats_text(date(2026, 10, 14)).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("Week of 12 Oct:") and "gym 0 of 4 done, 1 skipped, 3 not reported" in lines[0]
    assert lines[1].startswith("Week of 5 Oct:") and "gym 1 of 4 done, 3 not reported" in lines[1]
    assert D.stats_text(date(2026, 10, 14), weeks=1).count("Week of") == 1
    assert D.stats_text(date(2026, 11, 20)) == "No plan history in the last few weeks yet."
