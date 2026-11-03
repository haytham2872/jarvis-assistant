"""A06 (slice 2): nightly inputs survive a failed brief, and jobs inherit plan scope.

Three more acceptance criteria:

- *"brief generation failure leaves nightly inputs available"* — `compose_brief`
  cleared `night_results` **before** composing (the roadmap's "pop-before-work queue"),
  while `last_brief_date` is only set after a successful delivery. A failed DM therefore
  retried later with the night's findings already gone.
- *"a job gets originating constraints"* — A03 slice 3 built the brief but left
  constraints empty, noting the field "exists for when A04's plan scope supplies real
  ones". A04 slice 5 made the plan the turn's authorized scope, so it now does.
- *"due reminders and nightly work are not dropped on failed attempts"* and *"no
  duplicate schedule occurrence after restart"* were already satisfied by A04 slice 4's
  reordering; they are pinned here so a future change cannot quietly undo them.
"""
import asyncio
import datetime
import pathlib

import pytest

from backend.brain import chronos
from backend.context import origin


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def chrono_files(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    return tmp_path


# ------------------------------------- nightly inputs survive a failed brief
NIGHT = [{"date": "2026-09-22", "task": "research the FOMC decision",
          "summary": "The Fed held rates; the statement flagged two risks.",
          "file": ""}]


@pytest.fixture
def brief_env(chrono_files, monkeypatch):
    chronos._save_state({"night_results": list(NIGHT)})

    async def llm(prompt, max_tokens=None, **k):
        return "Morning. Overnight: the Fed held rates."

    async def no_calendar():
        return ""

    from backend.brain import research
    monkeypatch.setattr(research, "_llm", llm)
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    return chrono_files


def test_a_failed_delivery_keeps_the_night_findings(brief_env, monkeypatch):
    """The reproduced incident."""
    async def dead_discord(text):
        return False

    monkeypatch.setattr(chronos, "_notifier", dead_discord)
    assert run(chronos._send_brief(chronos.now_local())) is False

    state = chronos._load_state()
    assert state.get("night_results"), "the night's findings were consumed anyway"
    assert not state.get("last_brief_date"), "a failed brief was recorded as delivered"


def test_the_retry_reports_the_findings_and_only_then_consumes(brief_env, monkeypatch):
    delivered = {}

    async def dead(text):
        return False

    async def alive(text):
        delivered["text"] = text
        return True

    monkeypatch.setattr(chronos, "_notifier", dead)
    run(chronos._send_brief(chronos.now_local()))

    monkeypatch.setattr(chronos, "_notifier", alive)
    assert run(chronos._send_brief(chronos.now_local())) is True

    assert "Fed held rates" in delivered["text"] or "Overnight" in delivered["text"]
    assert chronos._load_state().get("night_results") == []


def test_composing_alone_never_consumes(brief_env):
    run(chronos.compose_brief())
    assert chronos._load_state().get("night_results"), (
        "composing consumed the findings before anything was delivered")


def test_mark_briefed_records_and_consumes_together(brief_env):
    """Marking without consuming repeats yesterday's findings; consuming without
    marking loses them. They belong in one step."""
    chronos._mark_briefed(chronos.now_local())
    state = chronos._load_state()
    assert state.get("last_brief_date")
    assert state.get("night_results") == []


def test_mark_briefed_does_not_clobber_a_concurrent_night_result(brief_env):
    """It applies a delta to fresh state -- a night task finishing during the brief's
    LLM call must not be wiped by a stale write (A04 slice 4's lesson)."""
    import inspect
    assert "_update_state" in inspect.getsource(chronos._mark_briefed)


def test_brief_now_consumes_only_after_confirmed_delivery(brief_env):
    """Returning text to Discord is not proof that Discord sent it."""
    text = run(chronos.brief_now())
    assert text
    assert chronos._load_state().get("night_results") == NIGHT

    async def failed(_text):
        return False

    run(chronos.brief_now(deliver=failed))
    assert chronos._load_state().get("night_results") == NIGHT

    async def delivered(_text):
        return True

    run(chronos.brief_now(deliver=delivered))
    assert chronos._load_state().get("night_results") == []


def test_new_night_result_during_delivery_survives_the_brief(brief_env, monkeypatch):
    late = {"date": "2026-09-22", "task": "late task", "summary": "Finished after compose", "file": ""}

    async def deliver(_text):
        chronos._update_state(lambda state: state.setdefault("night_results", []).append(late))
        return True

    monkeypatch.setattr(chronos, "_notifier", deliver)
    assert run(chronos._send_brief(chronos.now_local())) is True
    assert chronos._load_state().get("night_results") == [late]


# ------------------------------- reminders: not dropped, not duplicated (A04 s4)
def _seed_due_reminders(tmp):
    now = datetime.datetime.now(chronos._TZ)
    due = now - datetime.timedelta(minutes=5)
    chronos._SCHEDULE_FILE.write_text("\n".join(list(chronos._SCHEDULE_HEADER) + [
        chronos._format_reminder_line(due, "once", "take the bins out"),
        chronos._format_reminder_line(due, "daily", "take your vitamins"),
    ]) + "\n", encoding="utf-8")
    return now


def test_a_failed_dm_parks_the_reminder_instead_of_dropping_it(chrono_files, monkeypatch):
    now = _seed_due_reminders(chrono_files)

    async def dead(text):
        return False

    monkeypatch.setattr(chronos, "_notifier", dead)
    run(chronos._fire_due_reminders(now))

    parked = chronos._load_state().get("pending_mentions", [])
    assert len(parked) == 2
    assert any("bins" in p for p in parked)


def test_no_duplicate_occurrence_after_a_restart(chrono_files, monkeypatch):
    """The schedule is written before the DMs (A04 s4), so a second pass over the same
    file re-fires nothing."""
    now = _seed_due_reminders(chrono_files)

    async def dead(text):
        return False

    monkeypatch.setattr(chronos, "_notifier", dead)
    run(chronos._fire_due_reminders(now))
    first = len(chronos._load_state().get("pending_mentions", []))

    run(chronos._fire_due_reminders(now))          # as a restart would
    assert len(chronos._load_state().get("pending_mentions", [])) == first


def test_a_daily_reminder_is_rescheduled_not_left_due(chrono_files, monkeypatch):
    now = _seed_due_reminders(chrono_files)

    async def dead(text):
        return False

    monkeypatch.setattr(chronos, "_notifier", dead)
    run(chronos._fire_due_reminders(now))

    remaining = [l for l in chronos._SCHEDULE_FILE.read_text(encoding="utf-8").splitlines()
                 if l.startswith("[reminder]")]
    assert len(remaining) == 1 and "vitamins" in remaining[0]
    assert "bins" not in "\n".join(remaining)


# ------------------------------------------- a job inherits the plan as constraints
PLAN = [{"id": 1, "label": "Search for recent NVDA analysis", "tool": "web_browse"},
        {"id": 2, "label": "Update the topics note", "tool": "vault_write"}]


@pytest.fixture(autouse=True)
def _clean_ctx():
    origin.current_plan.set(None)
    origin.clear_origin()
    yield
    origin.current_plan.set(None)
    origin.clear_origin()


def test_a_job_spawned_mid_plan_inherits_it_as_a_constraint():
    origin.set_origin("look into my NVDA thesis", queue="[open] review positions")
    origin.set_plan(PLAN)

    brief = origin.brief_for("research NVDA sentiment")
    assert "CONSTRAINTS ACCEPTED" in brief
    assert "Search for recent NVDA analysis" in brief
    assert "leave the other steps to the turn that owns them" in brief


def test_a_turn_with_no_plan_gets_no_constraints_section():
    """A simple turn and a chronos night task genuinely have no plan --
    inventing one would be worse than omitting it."""
    origin.set_origin("hey what time is it")
    assert "CONSTRAINTS ACCEPTED" not in origin.brief_for("a task")


def test_a_stale_plan_does_not_leak_into_the_next_turn():
    """The plan is per-turn authorized scope; it must not outlive its turn."""
    origin.set_origin("first turn")
    origin.set_plan(PLAN)
    assert "CONSTRAINTS ACCEPTED" in origin.brief_for("job a")

    origin.set_origin("second, simpler turn")
    assert "CONSTRAINTS ACCEPTED" not in origin.brief_for("job b"), (
        "a superseded plan was handed to a job from a later turn")


def test_a_replan_updates_the_inherited_scope():
    origin.set_origin("do the thing")
    origin.set_plan(PLAN)
    origin.set_plan([{"id": 2, "label": "Save to the vault instead", "tool": "vault_write"}])

    brief = origin.brief_for("a job")
    assert "Save to the vault instead" in brief
    assert "Search for recent NVDA analysis" not in brief, (
        "a job inherited a superseded plan")


def test_an_empty_or_malformed_plan_adds_no_section():
    origin.set_origin("x")
    for bad in ([], None, [{"id": 1}], [{"label": "   "}]):
        origin.set_plan(bad)
        assert "CONSTRAINTS ACCEPTED" not in origin.brief_for("job")


def test_chat_compat_publishes_the_plan_and_every_revision():
    """Publishing only the initial plan would hand a job a superseded scope after a
    replan -- the same staleness A04 slice 5 fixed for the model itself."""
    import inspect
    from backend.brain import engine

    src = inspect.getsource(engine._chat_compat)
    assert src.count("_origin.set_plan(plan)") == 3, (
        "expected the initial plan plus both replan paths to publish the scope")
