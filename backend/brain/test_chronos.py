"""Tests for backend/brain/chronos.py — reminders, night tasks, morning brief.

Pure-function + file/state tests, plus firing/brief flows with the notifier
and LLM monkeypatched out (no network, no Discord, no real vault). Run with:
    .venv\\Scripts\\pytest backend/brain/test_chronos.py -v
"""

import asyncio
import datetime

import pytest

from backend.brain import chronos
from backend.brain.chronos import _TZ


def paris(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


@pytest.fixture
def tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    return tmp_path


# ---------------------------------------------------------------------------
# time — the [Now] line and parsing
# ---------------------------------------------------------------------------

def test_now_line_mentions_paris_and_current_date():
    line = chronos.now_line()
    assert "[Now]" in line
    assert "Europe/Paris" in line
    assert chronos.now_local().strftime("%Y-%m-%d") in line


def test_parse_when_space_and_t_separators():
    a = chronos.parse_when("2026-07-08 10:00")
    b = chronos.parse_when("2026-07-08T10:00")
    assert a == b == paris(2026, 7, 8, 10, 0)
    assert a.tzinfo is _TZ


def test_parse_when_garbage_returns_none():
    assert chronos.parse_when("tomorrow at ten") is None
    assert chronos.parse_when("") is None
    assert chronos.parse_when("2026-13-45 99:99") is None


# ---------------------------------------------------------------------------
# reminder line round trip
# ---------------------------------------------------------------------------

def test_reminder_line_round_trip():
    due = paris(2026, 7, 8, 10, 0)
    line = chronos._format_reminder_line(due, "once", "call the bank")
    parsed = chronos._parse_reminder_line(line)
    assert parsed["due"] == due
    assert parsed["repeat"] == "once"
    assert parsed["message"] == "call the bank"


def test_reminder_line_rejects_other_shapes():
    assert chronos._parse_reminder_line("[night] 2026-07-07 | research X") is None
    assert chronos._parse_reminder_line("[done] 2026-07-07 10:00 | fired") is None
    assert chronos._parse_reminder_line("# Schedule") is None


def test_night_line_round_trip():
    parsed = chronos._parse_night_line("[night] 2026-07-07 | after= | research Schopenhauer tonight")
    assert parsed == {"date": "2026-07-07", "after": "", "task": "research Schopenhauer tonight"}


def test_night_line_round_trip_with_after_date():
    line = chronos._format_night_line("2026-09-08", "2026-09-16", "report the FOMC decision")
    parsed = chronos._parse_night_line(line)
    assert parsed == {"date": "2026-09-08", "after": "2026-09-16", "task": "report the FOMC decision"}


def test_night_line_legacy_two_field_shape_still_parses():
    """A line written before the `after=` field existed (2-field: date | task)
    must keep parsing -- with no gating date -- not be silently dropped."""
    parsed = chronos._parse_night_line("[night] 2026-07-07 | research Schopenhauer tonight")
    assert parsed == {"date": "2026-07-07", "after": "", "task": "research Schopenhauer tonight"}


# ---------------------------------------------------------------------------
# night_task_due — the date gate
# ---------------------------------------------------------------------------

def test_night_task_due_empty_after_is_always_due():
    assert chronos.night_task_due("", paris(2026, 9, 9, 3, 0)) is True


def test_night_task_due_before_gate_date_is_not_due():
    assert chronos.night_task_due("2026-09-16", paris(2026, 9, 9, 3, 0)) is False


def test_night_task_due_on_or_after_gate_date_is_due():
    assert chronos.night_task_due("2026-09-16", paris(2026, 9, 16, 3, 0)) is True
    assert chronos.night_task_due("2026-09-16", paris(2026, 9, 20, 3, 0)) is True


def test_night_task_due_malformed_after_fails_open():
    """A malformed gate must not strand the task in the queue forever."""
    assert chronos.night_task_due("not-a-date", paris(2026, 9, 9, 3, 0)) is True


# ---------------------------------------------------------------------------
# daily recurrence — must skip missed days, not refire per cycle
# ---------------------------------------------------------------------------

def test_next_daily_occurrence_advances_one_day():
    due = paris(2026, 7, 7, 8, 0)
    now = paris(2026, 7, 7, 8, 1)
    assert chronos.next_daily_occurrence(due, now) == paris(2026, 7, 8, 8, 0)


def test_next_daily_occurrence_skips_days_backend_was_off():
    due = paris(2026, 7, 1, 8, 0)   # a week stale
    now = paris(2026, 7, 7, 12, 0)
    nxt = chronos.next_daily_occurrence(due, now)
    assert nxt == paris(2026, 7, 8, 8, 0)   # next FUTURE 8am, not 6 catch-up fires


def test_next_daily_occurrence_preserves_time_of_day():
    due = paris(2026, 7, 7, 22, 30)
    now = paris(2026, 7, 8, 6, 0)
    assert chronos.next_daily_occurrence(due, now) == paris(2026, 7, 8, 22, 30)


# ---------------------------------------------------------------------------
# night window
# ---------------------------------------------------------------------------

def test_night_window_boundaries():
    assert not chronos.in_night_window(paris(2026, 7, 7, 1, 59))
    assert chronos.in_night_window(paris(2026, 7, 7, 2, 0))
    assert chronos.in_night_window(paris(2026, 7, 7, 6, 59))
    assert not chronos.in_night_window(paris(2026, 7, 7, 7, 0))
    assert not chronos.in_night_window(paris(2026, 7, 7, 14, 0))


# ---------------------------------------------------------------------------
# brief_due — once per day, fires late rather than skipping
# ---------------------------------------------------------------------------

def test_brief_not_due_before_time():
    assert not chronos.brief_due(True, "08:00", "", paris(2026, 7, 7, 7, 59))


def test_brief_due_at_time():
    assert chronos.brief_due(True, "08:00", "", paris(2026, 7, 7, 8, 0))


def test_brief_fires_late_if_backend_was_off_at_the_hour():
    assert chronos.brief_due(True, "08:00", "2026-07-06", paris(2026, 7, 7, 11, 30))


def test_brief_not_due_twice_same_day():
    assert not chronos.brief_due(True, "08:00", "2026-07-07", paris(2026, 7, 7, 9, 0))


def test_brief_not_due_when_disabled():
    assert not chronos.brief_due(False, "08:00", "", paris(2026, 7, 7, 9, 0))


def test_brief_garbage_time_never_fires():
    assert not chronos.brief_due(True, "not-a-time", "", paris(2026, 7, 7, 9, 0))


# ---------------------------------------------------------------------------
# schedule_reminder tool
# ---------------------------------------------------------------------------

def _future_when(hours=2):
    return (chronos.now_local() + datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def test_reminder_add_then_list(tmp_files):
    reply = asyncio.run(chronos.schedule_reminder("add", when=_future_when(), message="call the bank"))
    assert "Reminder set" in reply and "local time" in reply
    listing = asyncio.run(chronos.schedule_reminder("list"))
    assert "call the bank" in listing


def test_reminder_add_fails_loudly_when_write_does_not_verify(tmp_files, monkeypatch):
    monkeypatch.setattr(chronos, "_read_schedule", lambda: [])
    reply = asyncio.run(chronos.schedule_reminder("add", when=_future_when(), message="call the bank"))
    assert reply.startswith("Error:")
    assert "did not persist" in reply


def test_reminder_rejects_past_time_and_names_current_time(tmp_files):
    past = (chronos.now_local() - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    reply = asyncio.run(chronos.schedule_reminder("add", when=past, message="too late"))
    assert "Error" in reply and "past" in reply
    # The error tells the model the actual current local time so it can retry correctly
    assert chronos.now_local().strftime("%Y-%m-%d") in reply


def test_reminder_rejects_unparseable_when(tmp_files):
    reply = asyncio.run(chronos.schedule_reminder("add", when="tomorrow at 10", message="call the bank"))
    assert "Error" in reply and "YYYY-MM-DD" in reply


def test_reminder_rejects_bad_repeat(tmp_files):
    reply = asyncio.run(chronos.schedule_reminder("add", when=_future_when(), message="x y", repeat="hourly"))
    assert "Error" in reply


def test_reminder_cancel_by_substring(tmp_files):
    asyncio.run(chronos.schedule_reminder("add", when=_future_when(), message="call the bank"))
    reply = asyncio.run(chronos.schedule_reminder("cancel", message="bank"))
    assert "Cancelled" in reply
    assert "Nothing scheduled" in asyncio.run(chronos.schedule_reminder("list"))


def test_reminder_cancel_no_match(tmp_files):
    reply = asyncio.run(chronos.schedule_reminder("cancel", message="nonexistent"))
    assert "No reminder matching" in reply


def test_reminder_cap(tmp_files):
    for i in range(chronos._MAX_PENDING_REMINDERS):
        r = asyncio.run(chronos.schedule_reminder("add", when=_future_when(i + 1), message=f"task {i}"))
        assert "Error" not in r
    reply = asyncio.run(chronos.schedule_reminder("add", when=_future_when(99), message="one too many"))
    assert "Error" in reply


# ---------------------------------------------------------------------------
# schedule_night_task tool
# ---------------------------------------------------------------------------

def test_night_task_add(tmp_files):
    reply = asyncio.run(chronos.schedule_night_task("research Schopenhauer's influence on Camus"))
    assert "Confirmed written to jarvis-vault/schedule.md" in reply
    lines = chronos._read_schedule()
    assert any(l.startswith("[night]") for l in lines)


def test_night_task_add_with_after_date_is_stored_and_confirmed(tmp_files):
    reply = asyncio.run(chronos.schedule_night_task(
        "report the FOMC decision results from the September meeting", after="2026-09-16"
    ))
    assert "Confirmed written" in reply
    assert "not fire before 2026-09-16" in reply
    parsed = [n for n in (chronos._parse_night_line(l) for l in chronos._read_schedule()) if n]
    assert parsed and parsed[0]["after"] == "2026-09-16"


def test_night_task_rejects_malformed_after_date(tmp_files):
    reply = asyncio.run(chronos.schedule_night_task("research something in depth tonight", after="16 sept"))
    assert "Error" in reply
    assert chronos._read_schedule() == [] or not any(
        l.startswith("[night]") for l in chronos._read_schedule()
    )


def test_night_task_add_fails_loudly_when_write_does_not_verify(tmp_files, monkeypatch):
    """If the read-back after writing doesn't find the new line (disk error,
    permissions, anything), the tool must return an Error, never a false
    'queued' claim -- this is the direct fix for the 2026-09-08 incident."""
    monkeypatch.setattr(chronos, "_read_schedule", lambda: [])  # write "succeeds", read-back finds nothing
    reply = asyncio.run(chronos.schedule_night_task("research something in depth tonight"))
    assert reply.startswith("Error:")
    assert "did not persist" in reply


def test_night_task_rejects_too_short(tmp_files):
    reply = asyncio.run(chronos.schedule_night_task("stuff"))
    assert "Error" in reply


# ---------------------------------------------------------------------------
# _run_night_task — respects the `after` gate when picking which task to pop
# ---------------------------------------------------------------------------

def test_run_night_task_skips_not_yet_due_and_runs_the_due_one(tmp_files, monkeypatch):
    now = paris(2026, 9, 9, 3, 0)   # inside the quiet window, before Sept 16
    lines = [
        chronos._format_night_line("2026-09-08", "2026-09-16", "report the FOMC decision"),
        chronos._format_night_line("2026-09-08", "", "summarize today's news"),
    ]
    chronos._write_schedule(lines)

    ran = {}

    async def fake_execute_task(prompt):
        ran["prompt"] = prompt
        return "done"

    async def fake_vault_write(filename, content, folder):
        return f"Confirmed written to jarvis-vault/{folder}/{filename}"

    monkeypatch.setattr("backend.brain.background.execute_task", fake_execute_task)
    monkeypatch.setattr("backend.tools.vault.vault_write", fake_vault_write)

    asyncio.run(chronos._run_night_task(now))

    assert "summarize today's news" in ran["prompt"]
    remaining = [n for n in (chronos._parse_night_line(l) for l in chronos._read_schedule()) if n]
    assert len(remaining) == 1
    assert remaining[0]["after"] == "2026-09-16"   # the gated task is still queued, untouched


def test_run_night_task_noop_when_nothing_is_due(tmp_files, monkeypatch):
    now = paris(2026, 9, 9, 3, 0)
    chronos._write_schedule([chronos._format_night_line("2026-09-08", "2026-09-16", "report the FOMC decision")])
    called = {"n": 0}

    async def fake_execute_task(prompt):
        called["n"] += 1
        return "done"

    monkeypatch.setattr("backend.brain.background.execute_task", fake_execute_task)
    asyncio.run(chronos._run_night_task(now))
    assert called["n"] == 0
    remaining = [n for n in (chronos._parse_night_line(l) for l in chronos._read_schedule()) if n]
    assert len(remaining) == 1


def test_night_task_rejects_duplicate(tmp_files):
    asyncio.run(chronos.schedule_night_task("research Schopenhauer's influence on Camus"))
    reply = asyncio.run(chronos.schedule_night_task("research schopenhauer's influence on camus"))
    assert "already queued" in reply


def test_night_task_cap(tmp_files):
    for i in range(chronos._MAX_PENDING_NIGHT):
        r = asyncio.run(chronos.schedule_night_task(f"research subject number {i} in depth tonight"))
        assert "Error" not in r
    reply = asyncio.run(chronos.schedule_night_task("research one more subject tonight please"))
    assert "Error" in reply


# ---------------------------------------------------------------------------
# reminder firing — DM path, parked-mention path, daily reschedule
# ---------------------------------------------------------------------------

def test_due_reminder_fires_dm_and_moves_to_done(tmp_files):
    due = chronos.now_local() - datetime.timedelta(minutes=1)
    chronos._write_schedule([chronos._format_reminder_line(due, "once", "call the bank")])
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    chronos.set_notifier(fake_notifier)
    try:
        asyncio.run(chronos._fire_due_reminders(chronos.now_local()))
    finally:
        chronos.set_notifier(None)

    assert sent == ["Reminder: call the bank"]
    lines = chronos._read_schedule()
    assert not any(l.startswith("[reminder]") for l in lines)
    assert any(l.startswith("[done]") and "call the bank" in l for l in lines)


def test_undue_reminder_does_not_fire(tmp_files):
    due = chronos.now_local() + datetime.timedelta(hours=1)
    chronos._write_schedule([chronos._format_reminder_line(due, "once", "later")])
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    chronos.set_notifier(fake_notifier)
    try:
        asyncio.run(chronos._fire_due_reminders(chronos.now_local()))
    finally:
        chronos.set_notifier(None)
    assert sent == []
    assert any(l.startswith("[reminder]") for l in chronos._read_schedule())


def test_failed_dm_parks_reminder_for_conversation(tmp_files):
    due = chronos.now_local() - datetime.timedelta(minutes=1)
    chronos._write_schedule([chronos._format_reminder_line(due, "once", "call the bank")])

    async def broken_notifier(text):
        return False   # Discord down

    chronos.set_notifier(broken_notifier)
    try:
        asyncio.run(chronos._fire_due_reminders(chronos.now_local()))
    finally:
        chronos.set_notifier(None)

    mention = chronos.pop_pending_mentions()
    assert "call the bank" in mention
    assert chronos.pop_pending_mentions() == ""   # delivered exactly once


def test_daily_reminder_reschedules_after_firing(tmp_files):
    due = chronos.now_local() - datetime.timedelta(minutes=1)
    chronos._write_schedule([chronos._format_reminder_line(due, "daily", "morning stretch")])

    async def fake_notifier(text):
        return True

    chronos.set_notifier(fake_notifier)
    try:
        asyncio.run(chronos._fire_due_reminders(chronos.now_local()))
    finally:
        chronos.set_notifier(None)

    reminders = [chronos._parse_reminder_line(l) for l in chronos._read_schedule()]
    reminders = [r for r in reminders if r]
    assert len(reminders) == 1
    assert reminders[0]["due"] > chronos.now_local()   # rescheduled to tomorrow
    assert reminders[0]["repeat"] == "daily"


# ---------------------------------------------------------------------------
# /brief command
# ---------------------------------------------------------------------------

def test_brief_command_bare_reports_config(tmp_files):
    reply = chronos.handle_brief_command("")
    assert "08:00" in reply   # default


def test_brief_command_set_time(tmp_files):
    reply = chronos.handle_brief_command("07:30")
    assert "07:30" in reply
    enabled, t = chronos.get_brief_config()
    assert enabled and t == "07:30"


def test_brief_command_off_on(tmp_files):
    chronos.handle_brief_command("off")
    assert chronos.get_brief_config()[0] is False
    chronos.handle_brief_command("on")
    assert chronos.get_brief_config()[0] is True


def test_brief_command_rejects_garbage(tmp_files):
    reply = chronos.handle_brief_command("noon-ish")
    assert "isn't something I understand" in reply


def test_brief_command_single_digit_hour_normalized(tmp_files):
    reply = chronos.handle_brief_command("7:30")
    assert "07:30" in reply


# ---------------------------------------------------------------------------
# brief composition — LLM stubbed, consumes night results exactly once
# ---------------------------------------------------------------------------

def test_compose_brief_includes_night_findings_and_consumes_them(tmp_files, monkeypatch):
    state = {"night_results": [
        {"date": "2026-07-07", "task": "research X", "summary": "X turned out to be Y.", "file": "night-x.md"}
    ]}
    chronos._save_state(state)

    captured_prompts = []

    async def fake_llm(prompt, max_tokens=0):
        captured_prompts.append(prompt)
        return "Morning. Here's what I found about X overnight: it's Y."

    import backend.brain.research as research
    monkeypatch.setattr(research, "_llm", fake_llm)

    async def no_calendar():
        return ""
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)

    text = asyncio.run(chronos.compose_brief())
    assert "overnight" in text.lower()
    assert "research X" in captured_prompts[0]
    assert "X turned out to be Y." in captured_prompts[0]

    # A06: composing does NOT consume. This test used to assert the opposite, which
    # pinned the defect: `last_brief_date` is only set after a SUCCESSFUL delivery, so
    # clearing here meant a failed DM lost the night's findings before the retry could
    # report them. Consumption belongs with the delivery that reported them.
    assert chronos._load_state().get("night_results"), (
        "night results were consumed before the brief was delivered")

    chronos._mark_briefed(chronos.now_local())
    assert chronos._load_state().get("night_results") == [], (
        "night results were not consumed once the brief counted as delivered")


def test_compose_brief_quiet_morning_needs_no_llm(tmp_files, monkeypatch):
    async def no_calendar():
        return ""
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    monkeypatch.setattr(chronos, "_collect_research_trail", lambda now: "")
    monkeypatch.setattr(chronos, "_collect_open_intents", lambda: "")

    text = asyncio.run(chronos.compose_brief())
    assert "Quiet night" in text


def test_compose_brief_falls_back_to_plain_digest_when_llm_fails(tmp_files, monkeypatch):
    chronos._save_state({"night_results": [
        {"date": "2026-07-07", "task": "research X", "summary": "Found Y.", "file": ""}
    ]})

    async def broken_llm(prompt, max_tokens=0):
        raise RuntimeError("api down")

    import backend.brain.research as research
    monkeypatch.setattr(research, "_llm", broken_llm)

    async def no_calendar():
        return ""
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    monkeypatch.setattr(chronos, "_collect_research_trail", lambda now: "")
    monkeypatch.setattr(chronos, "_collect_open_intents", lambda: "")

    text = asyncio.run(chronos.compose_brief())
    assert "Found Y." in text   # degraded but not silent


def test_delivered_brief_now_marks_today_briefed(tmp_files, monkeypatch):
    async def no_calendar():
        return ""
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    monkeypatch.setattr(chronos, "_collect_research_trail", lambda now: "")
    monkeypatch.setattr(chronos, "_collect_open_intents", lambda: "")

    async def delivered(_text):
        return True

    asyncio.run(chronos.brief_now(deliver=delivered))
    today = chronos.now_local().strftime("%Y-%m-%d")
    assert chronos._load_state().get("last_brief_date") == today
    # scheduled path now suppressed for the rest of the day
    assert not chronos.brief_due(True, "08:00", today, chronos.now_local())
