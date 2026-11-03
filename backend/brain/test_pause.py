"""P2: /pause and /resume (backend/brain/pause.py) -- the switch itself.

What each gate does with it is in test_pause_gates.py; the store is test_pauses.py.
"""
import asyncio
import time

import pytest

from backend.brain import background, pacing, pause
from backend.storage import db, pauses


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_notifier():
    yield
    pause.set_notifier(None)


# --- is_paused ---------------------------------------------------------------------------

def test_nothing_is_paused_by_default():
    assert not pause.is_paused("work") and not pause.is_paused("all")


def test_a_work_pause_holds_work_but_not_reminders_and_the_brief():
    pauses.start(pauses.WORK)
    assert pause.is_paused("work")
    assert not pause.is_paused("all")


def test_an_all_pause_holds_everything():
    pauses.start(pauses.ALL)
    assert pause.is_paused("work") and pause.is_paused("all")


def test_a_pause_that_has_run_out_no_longer_holds():
    pauses.start(pauses.ALL, until_at=time.time() - 1, now=time.time() - 3600)
    assert not pause.is_paused("work") and not pause.is_paused("all")


def test_a_broken_database_can_never_leave_jarvis_switched_off(monkeypatch):
    pauses.start(pauses.ALL)
    monkeypatch.setattr(pauses, "active", lambda **kw: (_ for _ in ()).throw(RuntimeError("disk")))
    assert pause.is_paused("work") is False and pause.is_paused("all") is False
    assert pause.status_line() == ""
    pause.record_skip("scheduled_task", "x")           # and no error either


def test_no_database_means_not_paused(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert pause.is_paused("work") is False


# --- /pause ------------------------------------------------------------------------------

def test_a_bare_pause_holds_until_resume():
    reply = pause.handle_pause_command("")
    row = pauses.active()
    assert row["scope"] == "work" and row["until_at"] is None
    assert "until you /resume" in reply and "Reminders and the morning brief still arrive" in reply


@pytest.mark.parametrize("arg,hours", [("3", 3), ("3h", 3), ("1.5", 1.5), ("0,5", 0.5), ("2H", 2)])
def test_hours_set_an_end_time(arg, hours):
    before = time.time()
    reply = pause.handle_pause_command(arg)
    row = pauses.active()
    assert before + hours * 3600 - 2 <= row["until_at"] <= time.time() + hours * 3600 + 2
    assert "local time" in reply


def test_all_widens_the_pause_and_the_reply_says_what_it_holds():
    reply = pause.handle_pause_command("all")
    assert pauses.active()["scope"] == "all"
    assert "including reminders and the morning brief" in reply


def test_the_reason_is_kept_wherever_the_number_and_scope_come():
    pause.handle_pause_command("2 all travelling to Lyon")
    row = pauses.active()
    assert row["scope"] == "all" and row["reason"] == "travelling to Lyon"
    assert row["until_at"] is not None
    pause.handle_pause_command("on holiday 4")
    assert pauses.active()["reason"] == "on holiday" and pauses.active()["until_at"] is not None


@pytest.mark.parametrize("arg", ["0", "-2", "0h", "999", "100000"])
def test_absurd_hours_are_refused_and_nothing_is_paused(arg):
    reply = pause.handle_pause_command(arg)
    assert "between 0 and" in reply
    assert pauses.active() is None


def test_a_second_pause_replaces_the_first_and_says_so():
    pause.handle_pause_command("")
    reply = pause.handle_pause_command("all")
    assert reply.startswith("Changed the pause:")
    assert pauses.active()["scope"] == "all"
    assert [r["ended_how"] for r in pauses.recent()] == [None, "replaced"]


def test_when_the_pause_cannot_be_recorded_nothing_is_paused_and_he_is_told(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    reply = pause.handle_pause_command("")
    assert "nothing is paused" in reply


def test_pausing_stops_the_running_jobs_that_have_no_steps(monkeypatch):
    monkeypatch.setattr(background, "pause_running", lambda: 2)
    reply = pause.handle_pause_command("")
    assert "2 running research job(s) were cancelled" in reply


def test_a_failure_stopping_jobs_does_not_undo_the_pause(monkeypatch):
    def boom():
        raise RuntimeError("no jobs module")
    monkeypatch.setattr(background, "pause_running", boom)
    pause.handle_pause_command("")
    assert pause.is_paused("work")


# --- /resume -----------------------------------------------------------------------------

def test_resume_when_not_paused():
    assert pause.handle_resume_command() == "I wasn't paused."


def test_resume_ends_the_pause_and_lists_what_it_cost(monkeypatch):
    resumed = []
    monkeypatch.setattr(background, "resume_queued", lambda: resumed.append(1))
    pause.handle_pause_command("")
    pause.record_skip("scheduled_task", "NVDA price at the open (Wed 15:30)")
    pause.record_skip("job_stopped", "#3 compare gyms")
    pause.record_skip("night_task_stopped", "research the ECB")
    text = pause.handle_resume_command("")
    assert not pause.is_paused("work")
    assert text.startswith("Resumed.")
    assert "skipped scheduled check: NVDA price at the open (Wed 15:30)" in text
    assert "stopped job: #3 compare gyms" in text
    assert "night task stopped (kept for the next quiet window): research the ECB" in text
    assert resumed == [1], "queued jobs must start again on /resume"


def test_resume_says_so_when_nothing_was_missed():
    pause.handle_pause_command("")
    text = pause.handle_resume_command("")
    assert "Nothing was missed" in text and "While paused" not in text


def test_a_long_list_is_capped_and_counted():
    pause.handle_pause_command("")
    for i in range(20):
        pause.record_skip("scheduled_task", f"check {i}")
    text = pause.handle_resume_command("")
    assert "check 14" in text and "check 15" not in text
    assert "(and 5 more)" in text


def test_resume_text_words_a_timed_pause_ending_differently():
    row = {"started_at": 1000.0, "ended_at": 1000.0 + 2 * 3600 + 300, "scope": "all"}
    ended = pause.resume_text(row, [], expired=True)
    assert ended.startswith("The pause ended (2h 05m, everything).")
    assert pause.resume_text(row, []).startswith("Resumed. I was paused for 2h 05m (everything).")


# --- a timed pause ending on its own ------------------------------------------------------

def _expired_pause():
    return pauses.start(pauses.WORK, until_at=time.time() - 5, now=time.time() - 3600)


def test_a_timed_pause_ending_is_announced_exactly_once(monkeypatch):
    sent, resumed = [], []
    monkeypatch.setattr(background, "resume_queued", lambda: resumed.append(1))

    async def notifier(text):
        sent.append(text)
        return True
    pause.set_notifier(notifier)
    pid = _expired_pause()
    pauses.add_skip(pid, "scheduled_task", "NVDA price at the close")

    assert run(pause.sweep()) == 1
    assert len(sent) == 1
    assert sent[0].startswith("The pause ended") and "NVDA price at the close" in sent[0]
    assert resumed == [1]
    assert pauses.get(pid)["ended_how"] == "expired"

    assert run(pause.sweep()) == 0 and len(sent) == 1, "an announced pause must not be announced again"


def test_a_pause_still_running_is_left_alone():
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    pause.set_notifier(notifier)
    pauses.start(pauses.WORK, until_at=time.time() + 3600)
    assert run(pause.sweep()) == 0 and sent == []
    assert pause.is_paused("work")


def test_a_failing_notifier_does_not_break_the_sweep_or_reopen_the_pause():
    async def notifier(text):
        raise RuntimeError("discord down")
    pause.set_notifier(notifier)
    pid = _expired_pause()
    assert run(pause.sweep()) == 1
    assert pauses.get(pid)["ended_how"] == "expired"
    assert not pause.is_paused("work")


def test_the_expiry_loop_never_pauses_itself(monkeypatch):
    seen = {}

    async def fake(**kw):
        seen.update(kw)
    monkeypatch.setattr(pacing, "run_periodic", fake)
    run(pause.run_loop())
    assert seen["name"] == "pause expiry" and seen["on_cycle"] is pause.sweep
    assert not seen.get("pausable"), "a paused expiry loop could never announce the end of a pause"


# --- words --------------------------------------------------------------------------------

def test_status_line_is_empty_unless_paused_and_then_says_what_is_held():
    assert pause.status_line() == ""
    pause.handle_pause_command("3 travelling")
    line = pause.status_line()
    assert line.startswith("PAUSED since") and "until " in line and "travelling" in line
    assert "reminders and the brief still arrive" in line
    pause.handle_pause_command("all")
    assert "everything" in pause.status_line()


def test_tool_note_only_appears_when_the_thing_just_set_up_is_held():
    assert pause.tool_note() == "" and pause.tool_note(pause.ALL) == ""
    pauses.start(pauses.WORK)
    assert "paused my own work" in pause.tool_note()
    assert pause.tool_note(pause.ALL) == "", "a work pause does not hold reminders"
    pauses.start(pauses.ALL)
    assert "paused everything I do on my own" in pause.tool_note(pause.ALL)


def test_a_skip_is_recorded_only_while_a_pause_is_in_force():
    pause.record_skip("scheduled_task", "before")
    pid = pauses.start(pauses.WORK)
    pause.record_skip("scheduled_task", "during")
    pauses.end("resume")
    pause.record_skip("scheduled_task", "after")
    assert [s["label"] for s in pauses.skips(pid)] == ["during"]


# --- one stretch, and jobs that stay stopped -------------------------------------------------

def test_resume_after_a_widened_pause_lists_both_and_counts_from_the_first(monkeypatch):
    monkeypatch.setattr(background, "resume_queued", lambda: None)
    now = time.time()
    a = pauses.start(pauses.WORK, now=now - 2 * 3600)
    pauses.add_skip(a, "scheduled_task", "the first one skipped", now=now - 3600)
    b = pauses.start(pauses.ALL, now=now - 3600 + 60)
    pauses.add_skip(b, "job_stopped", "#2 the second one stopped", now=now - 600)
    text = pause.handle_resume_command()
    assert "the first one skipped" in text and "#2 the second one stopped" in text
    assert text.startswith("Resumed. I was paused for 2h (everything).")


def test_resume_says_stopped_jobs_are_not_restarted():
    pause.handle_pause_command("")
    pause.record_skip("job_stopped", "#4 compare gyms")
    assert "Stopped jobs are not restarted on their own" in pause.handle_resume_command()
    pause.handle_pause_command("")
    pause.record_skip("scheduled_task", "NVDA at the open")
    assert "Stopped jobs" not in pause.handle_resume_command()


def test_the_end_of_a_timed_pause_counts_the_whole_stretch_too(monkeypatch):
    monkeypatch.setattr(background, "resume_queued", lambda: None)
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    pause.set_notifier(notifier)
    now = time.time()
    a = pauses.start(pauses.WORK, now=now - 7200)
    pauses.add_skip(a, "scheduled_task", "held in the first", now=now - 6000)
    pauses.start(pauses.WORK, until_at=now - 5, now=now - 3600)
    assert run(pause.sweep()) == 1
    assert "held in the first" in sent[0] and "The pause ended (2h" in sent[0]


# --- a pause nobody ends ------------------------------------------------------------------

def _notifier_spy():
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    pause.set_notifier(notifier)
    return sent


def test_a_pause_that_has_gone_a_day_is_mentioned_once_and_then_again_a_day_later():
    sent = _notifier_spy()
    now = time.time()
    pid = pauses.start(pauses.WORK, now=now - 25 * 3600)
    pauses.add_skip(pid, "scheduled_task", "NVDA at the open", now=now - 3600)
    run(pause.sweep())
    assert len(sent) == 1
    assert "still paused" in sent[0] and "until you /resume" in sent[0]
    assert "1 thing skipped or stopped so far" in sent[0] and "Send /resume" in sent[0]
    run(pause.sweep())
    assert len(sent) == 1, "at most once a day"
    pauses.mark_nudged(pid, now=now - 25 * 3600)          # as if that reminder was yesterday's
    run(pause.sweep())
    assert len(sent) == 2


def test_a_fresh_pause_is_not_mentioned():
    sent = _notifier_spy()
    pauses.start(pauses.ALL)
    run(pause.sweep())
    assert sent == []


def test_a_timed_pause_days_long_says_when_it_ends():
    sent = _notifier_spy()
    now = time.time()
    pauses.start(pauses.ALL, until_at=now + 3 * 86400, now=now - 25 * 3600)
    run(pause.sweep())
    assert "everything I do on my own" in sent[0] and "until " in sent[0] and "/resume" in sent[0]


def test_the_reminder_is_recorded_even_when_discord_is_down_and_never_raises():
    async def notifier(text):
        raise RuntimeError("discord down")
    pause.set_notifier(notifier)
    pid = pauses.start(pauses.WORK, now=time.time() - 25 * 3600)
    run(pause.sweep())
    assert pauses.get(pid)["nudged_at"] is not None, "one attempt a day, not one a minute"


def test_no_notifier_means_nothing_is_marked_so_the_reminder_is_not_lost():
    pid = pauses.start(pauses.WORK, now=time.time() - 25 * 3600)
    run(pause.sweep())
    assert pauses.get(pid)["nudged_at"] is None


def test_a_resumed_pause_sends_no_more_reminders():
    sent = _notifier_spy()
    pauses.start(pauses.WORK, now=time.time() - 25 * 3600)
    pause.handle_resume_command()
    run(pause.sweep())
    assert sent == []


# --- what he is told on every turn while paused ---------------------------------------------
# Live (2026-09-30): during a pause, "are you still doing your own research and checks?" was
# answered "same as always" in 2 of 5 trials, because the pause reached him only through tools
# he had not called.

def test_prompt_line_is_empty_unless_paused():
    assert pause.prompt_line() == ""
    pauses.start(pauses.WORK)
    line = pause.prompt_line()
    assert line.startswith("PAUSED since") and "Reminders and the morning brief still arrive" in line
    assert "Never say or imply that I am researching, watching or checking on my own" in line
    pauses.end("test")
    assert pause.prompt_line() == ""


def test_prompt_line_for_an_all_pause_says_reminders_are_held_too():
    pauses.start(pauses.ALL, reason="travelling")
    line = pause.prompt_line()
    assert "including reminders and the morning brief" in line and "travelling" in line
    assert "Reminders and the morning brief still arrive" not in line


def test_prompt_line_survives_a_broken_database(monkeypatch):
    pauses.start(pauses.WORK)
    monkeypatch.setattr(pauses, "active", lambda **kw: (_ for _ in ()).throw(RuntimeError("disk")))
    assert pause.prompt_line() == ""


def test_every_turn_carries_the_pause_and_an_unpaused_turn_carries_nothing_extra():
    from backend.brain import runtime_status
    plain = runtime_status.runtime_identity_text()
    assert plain.startswith("[Runtime identity]\nHost: ") and "PAUSED" not in plain
    pauses.start(pauses.WORK)
    held = runtime_status.runtime_identity_text()
    assert held.startswith(plain) and "PAUSED since" in held
    pauses.end("test")
    assert runtime_status.runtime_identity_text() == plain


def test_the_pause_reaches_the_prompt_the_model_reads():
    from backend.brain import personality
    _, calm = personality.build_system_prompt("hello")
    assert "PAUSED" not in calm
    pauses.start(pauses.WORK)
    _, held = personality.build_system_prompt("hello")
    assert "PAUSED since" in held
    # volatile block, so a pause never busts the cached prefix
    stable, _ = personality.build_system_prompt("hello")
    assert "PAUSED" not in stable


def test_a_failing_pause_module_never_breaks_the_identity_block(monkeypatch):
    from backend.brain import runtime_status
    monkeypatch.setattr(pause, "prompt_line", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    text = runtime_status.runtime_identity_text()
    assert text.startswith("[Runtime identity]") and "boom" not in text and "PAUSED" not in text
