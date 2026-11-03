"""schedule_task: a lookup that runs AT a time and messages its answer (chronos.py's
"Scheduled tasks", background.start_scheduled).

No network: the background worker is faked where a run would start.
"""
import asyncio
import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.brain import background, chronos, engine
from backend.brain.chronos import _TZ

NY = ZoneInfo("America/New_York")


def paris(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """Tuesday 29 September 2026, 23:30 Paris."""
    now = {"t": paris(2026, 9, 29, 23, 30)}
    monkeypatch.setattr(chronos, "now_local", lambda: now["t"])
    return now


@pytest.fixture
def worker(monkeypatch):
    """Records every run chronos starts; `fail` makes the next starts report busy."""
    state = {"started": [], "fail": 0}

    async def start(label, brief, skill="", occurrence=""):
        if state["fail"]:
            state["fail"] -= 1
            return "Error: 3 background tasks are already running"
        state["started"].append((label, brief))
        return ""
    monkeypatch.setattr(background, "start_scheduled", start)
    return state


@pytest.fixture
def dms(monkeypatch):
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(chronos, "_notifier", notifier)
    return sent


def tasks():
    return [t for t in (chronos._parse_task_line(l) for l in chronos._read_schedule()) if t]


NVDA = "Look up NVDA's live share price and today's change, and report it with the source."


def add_nvda_open():
    """`when` is local time; the task then stays on the New York clock."""
    return run(chronos.schedule_task("add", "2026-09-30 15:30", NVDA, "weekdays",
                                     "2026-10-02", "America/New_York"))


# --- the line format ------------------------------------------------------------------

def test_a_task_line_round_trips_even_with_a_pipe_in_the_text():
    due = datetime.datetime(2026, 9, 30, 9, 30, tzinfo=NY)
    line = chronos._format_task_line(due, "weekdays", "2026-10-02", "America/New_York",
                                     "compare A | B and report")
    t = chronos._parse_task_line(line)
    assert t == {"due": due, "repeat": "weekdays", "until": "2026-10-02",
                 "tz": "America/New_York", "task": "compare A | B and report", "skill": ""}


def test_other_schedule_lines_are_not_tasks():
    assert chronos._parse_task_line("[reminder] 2026-09-30 10:00 | once | call") is None
    assert chronos._parse_task_line("[task] 2026-09-30 10:00 | once | until= | tz=Mars/Base | x y z") is None


# --- recurrence -----------------------------------------------------------------------

def test_weekdays_skip_the_weekend():
    fri = paris(2026, 10, 2, 15, 30)
    assert chronos.next_task_occurrence(fri, "weekdays", fri) == paris(2026, 10, 5, 15, 30)


def test_weekly_and_once():
    wed = paris(2026, 9, 30, 9, 0)
    assert chronos.next_task_occurrence(wed, "weekly", wed) == paris(2026, 10, 7, 9, 0)
    assert chronos.next_task_occurrence(wed, "once", wed) is None


def test_a_week_offline_fires_once_not_seven_times():
    due = paris(2026, 9, 1, 8, 0)
    now = paris(2026, 9, 8, 12, 0)
    assert chronos.next_task_occurrence(due, "daily", now) == paris(2026, 9, 9, 8, 0)


def test_the_new_york_open_follows_new_york_across_the_mismatched_clock_change():
    """Paris changes clocks on 25 October 2026, New York on 1 November: that week the
    09:30 open is 14:30 Paris. A task kept in local time would be an hour late."""
    due = datetime.datetime(2026, 10, 23, 9, 30, tzinfo=NY)          # Friday
    seen = []
    for _ in range(7):
        seen.append(due.astimezone(_TZ).strftime("%a %d %H:%M"))
        due = chronos.next_task_occurrence(due, "weekdays", due)
    assert seen == ["Fri 23 15:30", "Mon 26 14:30", "Tue 27 14:30", "Wed 28 14:30",
                    "Thu 29 14:30", "Fri 30 14:30", "Mon 02 15:30"]


def test_a_paris_task_keeps_its_wall_clock_time_across_the_paris_change():
    due = paris(2026, 10, 24, 15, 30)
    nxt = chronos.next_task_occurrence(due, "daily", due)
    assert nxt.strftime("%Y-%m-%d %H:%M %Z") == "2026-10-25 15:30 CET"


# --- adding ---------------------------------------------------------------------------

def test_adding_the_nvda_open_check(files, clock):
    out = add_nvda_open()
    assert out.startswith("Confirmed written to jarvis-vault/schedule.md")
    assert ("3 run(s) in all, local time, when Sam gets them: Wed 30 Sep 15:30, "
            "Thu 01 Oct 15:30, Fri 02 Oct 15:30") in out
    assert "It stays at 09:30 on the America/New_York clock when clocks change" in out
    [t] = tasks()
    assert t["due"] == datetime.datetime(2026, 9, 30, 9, 30, tzinfo=NY)
    assert (t["repeat"], t["until"], t["tz"]) == ("weekdays", "2026-10-02", "America/New_York")


@pytest.mark.parametrize("args,expect", [
    (("add", "2026-09-30 15:30", "price?"), "full sentence"),
    (("add", "2026-09-30 15:30", "run git log and summarise the week"), "shell"),
    (("add", "2026-09-30 15:30", NVDA, "once", "", "Paris"), "unknown timezone"),
    (("add", "tomorrow 15:30", NVDA), "YYYY-MM-DD HH:MM"),
    (("add", "2026-09-29 15:30", NVDA), "is in the past"),
    (("add", "2026-09-30 15:30", NVDA, "hourly"), "repeat must be"),
    (("add", "2026-10-05 15:30", NVDA, "daily", "2026-10-01"), "is before the first run"),
    (("add", "2026-10-05 15:30", NVDA, "daily", "Friday"), "YYYY-MM-DD"),
    (("add", "2028-01-01 15:30", NVDA), "within a year"),
    (("remove",), "unknown action"),
])
def test_bad_requests_are_refused_and_nothing_is_written(files, clock, args, expect):
    out = run(chronos.schedule_task(*args))
    assert out.startswith("Error:") and expect in out
    assert tasks() == []


def test_a_weekday_task_set_for_saturday_starts_monday(files, clock):
    run(chronos.schedule_task("add", "2026-10-03 15:30", NVDA, "weekdays"))
    assert tasks()[0]["due"] == paris(2026, 10, 5, 15, 30)


def test_a_one_off_ignores_until_and_a_duplicate_is_refused(files, clock):
    run(chronos.schedule_task("add", "2026-09-30 15:30", NVDA, "once", "2026-10-09"))
    assert tasks()[0]["until"] == ""
    assert "already scheduled" in run(chronos.schedule_task("add", "2026-09-30 15:30", NVDA))


def test_list_and_cancel_every_match(files, clock):
    add_nvda_open()
    run(chronos.schedule_task("add", "2026-09-30 22:00", NVDA.replace("live", "closing"),
                              "weekdays", "2026-10-02", "America/New_York"))
    run(chronos.schedule_task("add", "2026-10-01 09:00", "Check my inbox for the lawyer's reply and tell me."))
    listing = run(chronos.schedule_task("list"))
    assert "next Wed 2026-09-30 15:30 local time (09:30 America/New_York), on weekdays until 2026-10-02" in listing
    assert "scheduled task, next" in run(chronos.schedule_reminder("list"))
    out = run(chronos.schedule_task("cancel", task="NVDA"))
    assert out.startswith("Cancelled 2 scheduled task(s)")
    assert [t["task"] for t in tasks()] == ["Check my inbox for the lawyer's reply and tell me."]


# --- running --------------------------------------------------------------------------

def test_a_due_run_starts_with_a_brief_and_the_schedule_moves_on(files, clock, worker):
    add_nvda_open()
    clock["t"] = paris(2026, 9, 30, 15, 30)
    run(chronos._fire_due_tasks(clock["t"]))
    [(label, brief)] = worker["started"]
    assert label.startswith("scheduled Wed 15:30: Look up NVDA")
    assert "running now, Wednesday 2026-09-30 15:30 local time (09:30 America/New_York)" in brief
    assert f"Task: {NVDA}" in brief and "sent to Sam on Discord exactly as you write it" in brief
    assert tasks()[0]["due"] == datetime.datetime(2026, 10, 1, 9, 30, tzinfo=NY)
    assert any("task run: Look up NVDA" in l for l in chronos._read_schedule())


def test_the_last_run_removes_the_task(files, clock, worker):
    add_nvda_open()
    for day in (30, 1, 2):
        clock["t"] = paris(2026, 9 if day == 30 else 10, day, 15, 31)
        run(chronos._fire_due_tasks(clock["t"]))
    assert len(worker["started"]) == 3 and tasks() == []
    assert any("(last run)" in l for l in chronos._read_schedule())


def test_a_run_found_too_late_is_reported_missed_not_run(files, clock, worker, dms):
    add_nvda_open()
    clock["t"] = paris(2026, 9, 30, 20, 0)                  # backend came back at 20:00
    run(chronos._fire_due_tasks(clock["t"]))
    assert worker["started"] == []
    assert dms and dms[0].startswith("I missed a scheduled check at Wed 15:30 local time")
    assert tasks()[0]["due"] == datetime.datetime(2026, 10, 1, 9, 30, tzinfo=NY)


def test_a_busy_worker_is_retried_within_the_grace(files, clock, worker, dms):
    add_nvda_open()
    worker["fail"] = 1
    clock["t"] = paris(2026, 9, 30, 15, 30)
    run(chronos._fire_due_tasks(clock["t"]))
    assert worker["started"] == [] and len(chronos._load_state().get("due_task_runs", [])) == 1
    clock["t"] = paris(2026, 9, 30, 15, 31)
    run(chronos._fire_due_tasks(clock["t"]))
    assert len(worker["started"]) == 1 and len(chronos._load_state().get("due_task_runs", [])) == 0 and dms == []


def test_a_retry_that_runs_out_of_grace_is_reported(files, clock, worker, dms):
    add_nvda_open()
    worker["fail"] = 1
    clock["t"] = paris(2026, 9, 30, 15, 30)
    run(chronos._fire_due_tasks(clock["t"]))
    clock["t"] = paris(2026, 9, 30, 16, 0)
    run(chronos._fire_due_tasks(clock["t"]))
    assert worker["started"] == [] and len(chronos._load_state().get("due_task_runs", [])) == 0
    assert "every background slot stayed busy" in dms[0]


def test_a_task_added_while_a_run_starts_is_not_lost(files, clock, monkeypatch):
    """The schedule is advanced before anything awaits (A04 slice 4)."""
    add_nvda_open()

    async def start(label, brief, skill="", occurrence=""):
        await chronos.schedule_task("add", "2026-10-01 09:00",
                                    "Check my inbox for the lawyer's reply and tell me.")
        return ""
    monkeypatch.setattr(background, "start_scheduled", start)
    clock["t"] = paris(2026, 9, 30, 15, 30)
    run(chronos._fire_due_tasks(clock["t"]))
    assert len(tasks()) == 2


def test_the_morning_brief_lists_todays_runs(files, clock):
    add_nvda_open()
    clock["t"] = paris(2026, 9, 30, 8, 0)
    assert "- 15:30 (scheduled check): Look up NVDA" in chronos._collect_todays_reminders(clock["t"])


# --- the background side --------------------------------------------------------------

def test_a_scheduled_run_messages_its_answer_and_writes_no_note(monkeypatch):
    sent, notes = [], []

    systems = []

    async def runner(text, deep=False, system=None):
        systems.append(system)
        return "NVDA opened at $231.20, up 0.4% (Yahoo Finance, 15:30)."

    async def note(t):
        notes.append(t)

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(background, "_run_compat_loop", runner)
    monkeypatch.setattr(background, "_write_result_note", note)
    monkeypatch.setattr(background, "_notifier", notifier)
    monkeypatch.setattr(background, "_tasks", {})
    t = background.BgTask(id=1, description="scheduled Wed 15:30: NVDA", brief="b",
                          kind="scheduled", notify=True)
    background._tasks[1] = t
    run(background._worker(t))
    assert sent == ["NVDA opened at $231.20, up 0.4% (Yahoo Finance, 15:30)."]
    assert notes == []
    # its final reply is the message, so it runs under the scheduled-run system prompt
    assert systems == [background._SCHEDULED_SYSTEM]
    notice = background.pop_completed_notices()
    assert "NVDA opened at $231.20" in notice and "Already sent to Sam" in notice


def test_start_scheduled_starts_a_notifying_scheduled_job(monkeypatch):
    seen = {}

    async def start(task, brief, **kw):
        seen.update(task=task, brief=brief, **kw)
        return object(), ""
    monkeypatch.setattr(background, "_start", start)
    assert run(background.start_scheduled("label", "brief")) == ""
    assert (seen["kind"], seen["notify"]) == ("scheduled", True)


# --- the claim check ------------------------------------------------------------------

@pytest.mark.parametrize("reply", ["All six checks are scheduled.",
                                   "Done, I've set up both daily NVDA checks.",
                                   "I scheduled the six price checks for you."])
def test_a_claimed_schedule_needs_a_successful_call(reply):
    assert engine._unbacked_claims(reply, set(), 0)
    assert engine._unbacked_claims(reply, {"schedule_task"}, 1) == []


def test_a_reminder_claim_is_backed_by_a_scheduled_task():
    reply = "Your reminder is set: I'll message you the price at 15:30."
    assert engine._unbacked_claims(reply, {"schedule_task"}, 1) == []
    assert engine._unbacked_claims(reply, set(), 0)


def test_an_offer_is_not_a_claim():
    assert engine._unbacked_claims("Want me to schedule the checks? I'll set up the checks now.",
                                   set(), 0) == []


# --- wiring ---------------------------------------------------------------------------

def test_the_tool_call_reaches_the_scheduler():
    assert engine.TOOL_DISPATCH["schedule_task"] is chronos.schedule_task


def test_the_scheduler_loop_fires_tasks_even_while_a_turn_is_running(files, monkeypatch):
    fired = []

    async def nothing(now):
        pass

    async def fire(now):
        fired.append(now)
    monkeypatch.setattr(chronos, "_fire_due_reminders", nothing)
    monkeypatch.setattr(chronos, "_fire_due_tasks", fire)
    monkeypatch.setattr(chronos, "_busy_check", lambda: True)
    monkeypatch.setattr(chronos, "get_brief_config", lambda: (False, "08:00"))
    run(chronos._cycle())
    assert len(fired) == 1


def test_when_is_paris_time_even_with_another_clock(files, clock):
    """Live: with `when` read on the task's own clock, the model twice put the Paris open
    (15:30) on the New York clock. `when` is now always local time."""
    run(chronos.schedule_task("add", "2026-09-30 22:00", NVDA, "once", "", "America/New_York"))
    assert tasks()[0]["due"] == datetime.datetime(2026, 9, 30, 16, 0, tzinfo=NY)
    past = run(chronos.schedule_task("add", "2026-09-29 23:00", NVDA, "once", "", "America/New_York"))
    assert "local time is in the past" in past
