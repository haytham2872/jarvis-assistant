"""P2: /activity -- what JARVIS is doing, waiting on, has scheduled and has finished.

Every line is read from what its owner already records, so these tests seed the real stores
(a throwaway database, throwaway files) and check the view against them.
"""
import asyncio
import datetime
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.brain import activity, background, chronos, research
from backend.brain import discord_bridge as db
from backend.brain.chronos import _TZ
from backend.storage import approvals, forecasts, operations, pauses, runs, watches

HOUR = 3600.0


def paris(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


NOW = paris(2026, 9, 30, 14, 0).timestamp()          # Wednesday


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    """Empty stores: a throwaway database (conftest) and throwaway files."""
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    monkeypatch.setattr(research, "_QUEUE_FILE", tmp_path / "research-queue.md")
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    monkeypatch.setattr(background, "_tasks", {})
    return tmp_path


def job(id, status, description="a job", *, started=NOW - 600, finished=None, **kw):
    background._tasks[id] = background.BgTask(id=id, description=description, status=status,
                                              started=started, finished=finished, **kw)


def request(preview="Send email to sarah@example.com: 'Running late'", *, context="background",
            at=NOW - HOUR):
    return approvals.create(context=context, tool="gmail_action", arguments={}, preview=preview,
                            risk="outward", why="a background job wanted to reply",
                            action="send_email", now=at)


def schedule(*lines):
    chronos._write_schedule(list(chronos._SCHEDULE_HEADER) + list(lines))


def section(text, title):
    """The lines under `title` (up to the next blank line)."""
    lines = text.splitlines()
    if title not in lines:
        return []
    out = []
    for line in lines[lines.index(title) + 1:]:
        if not line.strip():
            break
        out.append(line)
    return out


# --- an empty world -----------------------------------------------------------------------

def test_an_empty_world_says_so_in_every_section():
    text = activity.activity_text(NOW)
    assert text.startswith("Activity as of Wed 30 Sep 14:00 (local time)")
    assert "Running now: nothing is running." in text
    assert "Waiting for you: nothing is waiting on you." in text
    assert "Done in the last 24 hours: nothing finished." in text
    assert "PAUSED" not in text
    # what is always true of the schedule: the brief and my own research
    assert any("morning brief" in l for l in section(text, "Scheduled"))
    assert any("my own research: pace normal" in l for l in section(text, "Scheduled"))


# --- running now --------------------------------------------------------------------------

def test_running_now_lists_running_and_queued_jobs_and_not_finished_ones():
    job(1, "running", "Research the ECB's next meeting", started=NOW - 200)
    job(2, "queued", "Compare gyms near the office")
    job(3, "done", "Read the notary emails", finished=NOW - 5000, started=NOW - 5400)
    job(4, "running", "Look up the NVDA price", kind="scheduled", started=NOW - 20)
    job(5, "running", "What did the ECB decide", kind="web_research", depth="deep",
        started=NOW - 90)
    lines = section(activity.activity_text(NOW), "Running now")
    body = "\n".join(lines)
    assert "job #1 (3m): Research the ECB's next meeting" in body
    assert "job #2 queued: Compare gyms near the office" in body
    assert "scheduled run #4 (20s)" in body
    assert "web research #5, deep (1m): What did the ECB decide" in body
    assert "notary" not in body


def test_a_night_task_in_progress_and_an_approved_request_being_carried_out_show_up():
    rid = runs.begin_run("night_task", "research the ECB rate path")
    a = request()
    approvals.claim(a, "button", now=NOW - 5)
    body = "\n".join(section(activity.activity_text(NOW + 1), "Running now"))
    assert "night task" in body and "research the ECB rate path" in body
    assert f"request #{a} is being carried out" in body
    runs.finish_run(rid)
    assert "night task" not in "\n".join(section(activity.activity_text(NOW), "Running now"))


def test_a_long_list_is_capped_and_counted():
    for i in range(1, 10):
        job(i, "running", f"job number {i}")
    lines = section(activity.activity_text(NOW), "Running now")
    assert len(lines) == activity._MAX_RUNNING + 1
    assert lines[-1] == "- (+3 more)"


# --- waiting for him ----------------------------------------------------------------------

def test_a_pending_request_is_listed_with_the_words_that_answer_it():
    a = request(context="background", at=NOW - 3 * HOUR)
    body = "\n".join(section(activity.activity_text(NOW), "Waiting for you"))
    assert f"request #{a}: Send email to sarah@example.com: 'Running late'" in body
    assert "from a background job" in body
    assert "expires in 21h" in body
    assert f'Reply "yes {a}" or "no {a}"' in body


def test_requests_that_are_settled_or_expired_are_not_waiting():
    live = request(at=NOW - HOUR)
    old = request("Send email to old@example.com: 'late'", at=NOW - 30 * HOUR)      # 24 h passed
    done = request("Send email to done@example.com: 'ok'", at=NOW - HOUR)
    approvals.deny(done, "button", now=NOW - 60)
    body = "\n".join(section(activity.activity_text(NOW), "Waiting for you"))
    assert f"request #{live}" in body
    assert f"request #{old}" not in body and f"request #{done}" not in body


def test_a_request_cut_off_mid_run_stays_visible_for_three_days_only():
    a = request(at=NOW - 2 * HOUR)
    approvals.claim(a, "button", now=NOW - HOUR)
    approvals.finish(a, approvals.UNCERTAIN, "cut off", now=NOW - HOUR)
    assert "may or may not have happened" in "\n".join(
        section(activity.activity_text(NOW), "Waiting for you"))
    later = NOW + 4 * 24 * HOUR
    assert "may or may not" not in "\n".join(section(activity.activity_text(later), "Waiting for you"))


def test_a_forecast_that_could_not_settle_waits_for_his_word():
    fid = forecasts.add(claim="The ECB holds rates on 24 Oct", criteria="press release",
                        domain="macro", raw_p=0.7, stated_p=0.7, method="web",
                        check_query="ECB decision", resolve_by=NOW - 10 * HOUR, now=NOW - 20 * HOUR)
    forecasts.update(fid, state=forecasts.UNRESOLVABLE)
    body = "\n".join(section(activity.activity_text(NOW), "Waiting for you"))
    assert f"forecast #{fid} could not be settled on its own" in body


def test_things_i_could_not_message_are_counted_as_waiting_for_the_next_conversation():
    schedule()
    chronos._save_state({"pending_mentions": ["call the bank", "email Sarah"]})
    research._save_state({"deferred_notifies": [{"text": "x"}]})
    wid = watches.add(subject="NVDA", condition="below 180", method="web", query="NVDA",
                      every_s=HOUR, expires_at=NOW + 86400, now=NOW - HOUR)
    watches.update(wid, alert_state="parked")
    body = "\n".join(section(activity.activity_text(NOW), "Waiting for you"))
    assert "to tell you in our next conversation: 2 reminders, 1 research finding, 1 alert" in body


# --- scheduled ----------------------------------------------------------------------------

def test_scheduled_is_one_list_in_time_order_across_sources():
    chronos._save_state({"last_brief_date": "2026-09-30"})     # today's brief is out: tomorrow's is next
    schedule(
        chronos._format_reminder_line(paris(2026, 10, 1, 9, 0), "once", "email the notary"),
        chronos._format_reminder_line(paris(2026, 9, 30, 18, 0), "daily", "stretch"),
        chronos._format_task_line(paris(2026, 9, 30, 15, 30), "weekdays", "2026-10-02",
                                  "Europe/Paris", "report NVDA's price at the open"),
        chronos._format_task_line(datetime.datetime(2026, 9, 30, 16, 0,
                                                    tzinfo=chronos.ZoneInfo("America/New_York")),
                                  "once", "", "America/New_York", "report the close"),
    )
    lines = section(activity.activity_text(NOW), "Scheduled")
    order = [l for l in lines if any(k in l for k in ("NVDA", "stretch", "close", "notary",
                                                      "morning brief"))]
    assert len(order) == 5
    assert order[0].startswith("- today 15:30") and "on weekdays until 2026-10-02" in order[0]
    assert order[1].startswith("- today 18:00") and "stretch" in order[1] and "every day" in order[1]
    assert order[2].startswith("- today 22:00") and "16:00 America/New_York" in order[2]
    assert order[3].startswith("- tomorrow 08:00") and "morning brief" in order[3]
    assert order[4].startswith("- tomorrow 09:00") and "email the notary" in order[4]


def test_the_brief_is_next_tomorrow_once_today_s_is_out_and_overdue_when_it_was_missed():
    text = activity.activity_text(NOW)                       # 14:00, none sent today
    assert "overdue: goes out as soon as I am free" in text
    chronos._save_state({"last_brief_date": "2026-09-30"})
    assert "tomorrow 08:00: morning brief" in activity.activity_text(NOW)
    chronos._save_state({"brief_enabled": False})
    assert "morning brief" not in activity.activity_text(NOW)
    chronos._save_state({"last_brief_date": "2026-09-30", "brief_time": "18:30"})
    assert "tomorrow 18:30: morning brief" in activity.activity_text(NOW)


def test_night_tasks_watches_forecasts_and_research_have_their_lines():
    schedule(chronos._format_night_line("2026-09-29", "", "research the ECB rate path"),
             chronos._format_night_line("2026-09-29", "2026-10-05", "report the meeting result"))
    watches.add(subject="NVDA", condition="closes below $180", method="web", query="NVDA close",
                every_s=HOUR, expires_at=NOW + 20 * 86400, now=NOW - 60)
    forecasts.add(claim="The ECB holds rates", criteria="release says unchanged", domain="macro",
                  raw_p=0.7, stated_p=0.7, method="web", check_query="ECB",
                  resolve_by=NOW + 3 * 86400, now=NOW - HOUR)
    research._QUEUE_FILE.write_text(
        "[queued] 2026-09-29 | Albert Camus | perspectives | asked\n", encoding="utf-8")
    body = "\n".join(section(activity.activity_text(NOW), "Scheduled"))
    assert "2 night tasks queued for the quiet hours (02:00 to 07:00)" in body
    assert "(1 not before its date)" in body
    assert "watch: NVDA -- closes below $180 (every hour" in body
    assert "1 open forecast; next one falls due Sat 03 Oct 14:00: The ECB holds rates" in body
    assert "1 subject you queued: Albert Camus" in body


def test_research_that_is_off_says_how_to_change_it():
    research._save_state({"pace": "off"})
    assert "my own research is off (/pace to change)" in activity.activity_text(NOW)


def test_research_says_when_its_next_pass_is_due():
    research._save_state({"last_tick": NOW - 30 * 60})       # pace normal: a pass every 90 minutes
    assert "my own research: pace normal, next pass today 15:00" in activity.activity_text(NOW)
    research._save_state({"last_tick": NOW - 30 * 60, "pace": "low"})
    assert "my own research: pace low, next pass today 17:30" in activity.activity_text(NOW)
    research._save_state({})                                  # never ran: no time to promise
    lines = section(activity.activity_text(NOW), "Scheduled")
    assert [l for l in lines if "my own research" in l] == ["- my own research: pace normal"]


# --- done in the last day ------------------------------------------------------------------

def test_done_covers_the_last_24_hours_newest_first_and_no_further_back():
    job(1, "done", "Read the notary emails", started=NOW - 8000, finished=NOW - 7000,
        summary="The deed is ready to sign on Friday.", note_file="drafts/bg-1-notary.md")
    job(2, "failed", "Old job", started=NOW - 40 * HOUR, finished=NOW - 39 * HOUR, summary="Failed: x")
    job(3, "cancelled", "Compare gyms", started=NOW - 900, finished=NOW - 800,
        summary="Stopped by /pause before it finished.")
    a = request("Send email to marc@example.com: 'Thanks'")
    approvals.claim(a, "button", now=NOW - 3000)
    approvals.finish(a, approvals.EXECUTED, "sent", now=NOW - 2990)
    schedule("[done] 2026-09-30 12:00 | reminder fired: stretch",
             "[done] 2026-09-28 12:00 | reminder fired: last week")
    research._QUEUE_FILE.write_text(
        "[done] 2026-09-30 10:00 | Viktor Frankl [deepen] (auto) | wrote topics/viktor-frankl.md\n",
        encoding="utf-8")
    fid = forecasts.add(claim="Apple holds its September event", criteria="c", domain="tech",
                        raw_p=0.9, stated_p=0.9, method="web", check_query="q",
                        resolve_by=NOW - 30 * HOUR, now=NOW - 40 * HOUR)
    forecasts.resolve(fid, outcome=1, resolver="judge", quote="held it", notice="n", now=NOW - 2 * HOUR)

    lines = section(activity.activity_text(NOW), "Done in the last 24 hours")
    body = "\n".join(lines)
    assert "job #1 done (16m): Read the notary emails -- The deed is ready to sign on Friday. [drafts/bg-1-notary.md]" in body
    assert "job #3 cancelled" in body and "Stopped by /pause" in body
    assert f"request #{a} approved and carried out: Send email to marc@example.com: 'Thanks'" in body
    assert "reminder fired: stretch" in body and "last week" not in body
    assert "research: Viktor Frankl [deepen] (auto) -- wrote topics/viktor-frankl.md" in body
    assert f"forecast #{fid} settled yes: Apple holds its September event" in body
    assert "Old job" not in body
    stamps = [l for l in lines if l.startswith("- today")]
    hours = [l.split()[2].rstrip(":") for l in stamps]
    assert hours == sorted(hours, reverse=True), "newest first"


def test_a_request_denied_or_expired_says_it_never_ran():
    d = request("Send email to a@example.com: 'x'")
    approvals.deny(d, "reply", now=NOW - 600)
    e = request("Send email to b@example.com: 'y'", at=NOW - 25 * HOUR)
    approvals.expire_due(now=NOW - 30)
    body = "\n".join(section(activity.activity_text(NOW), "Done in the last 24 hours"))
    assert f"request #{d} denied, so it never ran" in body
    assert f"request #{e} expired unanswered and never ran" in body


def test_a_fired_watch_is_listed_only_when_it_finished_in_the_window():
    fresh = watches.add(subject="NVDA", condition="c", method="web", query="q", every_s=HOUR,
                        expires_at=NOW + 86400, now=NOW - 5 * HOUR)
    old = watches.add(subject="TSLA", condition="c", method="web", query="q", every_s=HOUR,
                      expires_at=NOW + 86400, now=NOW - 50 * HOUR)
    watches.update(fresh, state=watches.FIRED, last_checked_at=NOW - HOUR)
    watches.update(old, state=watches.FIRED, last_checked_at=NOW - 30 * HOUR)
    body = "\n".join(section(activity.activity_text(NOW), "Done in the last 24 hours"))
    assert "watch fired: NVDA" in body and "TSLA" not in body


def test_done_is_capped_but_the_counts_are_not():
    for i in range(1, 20):
        job(i, "done", f"job number {i}", started=NOW - 5000 - i, finished=NOW - 4000 - i)
    r = runs.begin_run("discord", "hi")
    runs.finish_run(r)
    r2 = runs.begin_run("discord", "hi again")
    runs.finish_run(r2, "interrupted")
    runs.finish_run(runs.begin_run("night_task", "overnight work"))      # not a conversation turn
    runs.finish_run(runs.begin_run("background", "a job"))
    operations.record_operation(tool="web_browse", outcome="ok", started_at=NOW - 60)
    operations.record_operation(tool="web_browse", outcome="ok", started_at=NOW - 50)
    operations.record_operation(tool="gmail_action", outcome="unknown", started_at=NOW - 40)
    lines = section(activity.activity_text(NOW), "Done in the last 24 hours")
    assert len(lines) == activity._MAX_DONE + 1 + 2
    assert "- (+7 more)" in lines
    assert "- conversation turns: 2 (1 interrupted)" in lines
    assert "- tool calls: 2 ok, 1 uncertain" in lines


def test_a_pause_that_ended_is_listed_and_one_in_force_is_shown_at_the_top():
    p = pauses.start(pauses.ALL, reason="travelling", now=NOW - 5 * HOUR)
    pauses.end("resume", now=NOW - 3 * HOUR)
    assert "pause ended after 2h (all, resume): travelling" in activity.activity_text(NOW)
    pauses.start(pauses.WORK, now=NOW - 600)
    pauses.add_skip(pauses.active(now=NOW)["id"], "scheduled_task", "NVDA price at the open", now=NOW - 300)
    text = activity.activity_text(NOW)
    assert text.splitlines()[2].startswith("PAUSED since Wed 13:50")
    assert "1 thing skipped or stopped so far; /resume lists them." in text.splitlines()[2]


# --- robustness and the acceptance check --------------------------------------------------

def test_a_source_that_cannot_be_read_only_costs_its_own_section(monkeypatch):
    request()
    job(1, "running", "still visible")

    def boom(**kw):
        raise RuntimeError("table locked")
    monkeypatch.setattr(approvals, "pending", boom)
    text = activity.activity_text(NOW)
    assert "(waiting items could not be read just now)" in text
    assert "job #1" in text and "morning brief" in text


def test_the_view_matches_the_tables_it_is_built_from():
    """The roadmap's acceptance: jobs, a pending approval and scheduled tasks are all present
    in the view, and the view says exactly what the stores hold."""
    job(1, "running", "Research the ECB's next meeting", started=NOW - 120)
    job(2, "queued", "Compare gyms")
    job(3, "running", "Look up NVDA", kind="scheduled", started=NOW - 15)
    ids = [request(f"Send email number {i} to x{i}@example.com", at=NOW - i * HOUR) for i in (1, 2)]
    schedule(
        chronos._format_reminder_line(paris(2026, 9, 30, 18, 0), "once", "call the bank"),
        chronos._format_task_line(paris(2026, 9, 30, 15, 30), "daily", "", "Europe/Paris",
                                  "report NVDA's price at the open"),
        chronos._format_night_line("2026-09-29", "", "research the ECB rate path"),
    )
    text = activity.activity_text(NOW)

    running = section(text, "Running now")
    assert len(running) == len(background.snapshot()) == 3
    for t in background.snapshot():
        assert any(f"#{t['id']}" in l for l in running)

    waiting = section(text, "Waiting for you")
    assert len(waiting) == len(approvals.pending(now=NOW)) == 2
    for a in approvals.pending(now=NOW):
        assert any(f"request #{a['id']}" in l for l in waiting)

    scheduled = "\n".join(section(text, "Scheduled"))
    agenda = chronos.agenda()
    for r in agenda["reminders"]:
        assert r["message"] in scheduled
    for t in agenda["tasks"]:
        assert t["task"] in scheduled
    for n in agenda["night"]:
        assert n["task"] in scheduled


def test_check_background_tasks_is_the_same_view(monkeypatch):
    job(1, "running", "Research the ECB", started=NOW - 60)
    monkeypatch.setattr(activity.time, "time", lambda: NOW)
    assert run(background.check_background_tasks()) == activity.activity_text()
    from backend.brain.engine import TOOL_DISPATCH
    assert TOOL_DISPATCH["check_background_tasks"] is background.check_background_tasks


# --- the Discord commands -----------------------------------------------------------------

class Channel:
    def __init__(self):
        self.parts = []

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, part):
        self.parts.append(part)


@pytest.fixture
def bridge(monkeypatch):
    class FakeClient:
        def __init__(self, intents):
            self._connection = SimpleNamespace(user=None)

        @property
        def user(self):
            return self._connection.user

    monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        Client=FakeClient))
    monkeypatch.setattr(db, "_ALLOWED_ID", "123")
    client = db._build_client()
    client._connection.user = SimpleNamespace(id=999)
    monkeypatch.setattr(db, "_claim_ingress", lambda message: True)
    for name in ("_pause_handler", "_resume_handler", "_activity_handler", "_approval_reply_handler"):
        monkeypatch.setattr(db, name, None)
    return client


def say(client, text, user=123):
    channel = Channel()
    message = SimpleNamespace(id=1, author=SimpleNamespace(id=user), content=text,
                              channel=channel, attachments=[])
    run(client.on_message(message))
    return channel.parts


def test_pause_resume_and_activity_reach_their_handlers(bridge):
    seen = []
    db.configure(pause_handler=lambda arg: seen.append(("pause", arg)) or "paused ok",
                 resume_handler=lambda arg: seen.append(("resume", arg)) or "resumed ok",
                 activity_handler=lambda: seen.append(("activity",)) or "the view")
    assert say(bridge, "/pause") == ["paused ok"]
    assert say(bridge, "/pause 3 all travelling") == ["paused ok"]
    assert say(bridge, "/resume") == ["resumed ok"]
    assert say(bridge, "/activity") == ["the view"]
    assert seen == [("pause", ""), ("pause", "3 all travelling"), ("resume", ""), ("activity",)]


def test_a_command_that_only_starts_like_pause_is_an_ordinary_message(bridge, monkeypatch):
    called = []
    db.configure(pause_handler=lambda arg: called.append(arg) or "paused")
    turns = []

    async def turn(text):
        turns.append(text)
        return "reply"

    async def ctx(message, text):
        return SimpleNamespace()
    monkeypatch.setattr(db, "_turn_handler", turn)
    monkeypatch.setattr(db, "_run_turn_with_progress", lambda message, text: turn(text))
    monkeypatch.setattr(db, "_context_for_message", ctx)
    say(bridge, "/pausefoo now")
    assert called == [] and turns == ["/pausefoo now"]


def test_a_stranger_cannot_pause_me(bridge):
    called = []
    db.configure(pause_handler=lambda arg: called.append(arg) or "paused")
    assert say(bridge, "/pause", user=666) == [] and called == []


def test_a_failing_handler_is_reported_not_raised(bridge):
    def boom(arg):
        raise RuntimeError("database is locked")
    db.configure(pause_handler=boom, activity_handler=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert say(bridge, "/pause") == ["Couldn't pause -- check the backend log."]
    assert say(bridge, "/activity") == ["Couldn't read my activity -- check the backend log."]


def test_the_commands_are_listed_in_slash_commands():
    listed = [cmd.split()[0] for cmd, _ in db._COMMAND_HELP]
    assert {"/pause", "/resume", "/activity"} <= set(listed)
    text = db._commands_text()
    assert "/pause [hours] [all]" in text and "/activity" in text


def test_the_pause_line_counts_everything_skipped_since_he_first_paused():
    a = pauses.start(pauses.WORK, now=NOW - 2 * HOUR)
    pauses.add_skip(a, "scheduled_task", "first", now=NOW - HOUR)
    b = pauses.start(pauses.ALL, now=NOW - 30 * 60)
    pauses.add_skip(b, "job_stopped", "second", now=NOW - 600)
    assert "2 things skipped or stopped so far" in activity.activity_text(NOW)


def test_main_wires_the_switch_and_the_view():
    """main.py cannot be started in a test; this pins that the three commands are handed to the
    bridge, that the expiry loop is started, and that the notifier reaches pause."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "pause_handler       = pause.handle_pause_command" in src
    assert "resume_handler      = pause.handle_resume_command" in src
    assert "activity_handler    = activity.activity_text" in src
    assert '"pause":      pause.run_loop' in src
    import re
    notified = src.split("Phone slice 4: every message")[1].split("discord_bridge.send_to_owner")[0]
    assert re.search(r'\(pause, "pause"\)', re.sub(r"#.*", "", notified)), "pause gets no notifier"


def test_the_phone_view_leaves_out_a_section_that_has_nothing_and_no_empty_wording():
    # Goals has no "nothing here" line: with no goals it is not a section at all, while
    # the four sections that say "nothing ..." are always there.
    titles = [s["title"] for s in activity.activity_view(NOW)["sections"]]
    assert titles == ["Running now", "Waiting for you", "Scheduled", "Done in the last 24 hours"]
