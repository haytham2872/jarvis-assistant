"""Tests for engine.py's anti-confabulation guard (2026-09-08).

Live incident: asked to schedule a night task and record it in the queue,
JARVIS said "Both done" with no tool call backing either claim that turn.
Streaming TTS means an already-spoken false claim can't be un-said, so this
guard is two-sided: a pre-turn nudge (personality.is_persistence_request,
tested in test_personality.py) and this module's post-turn detection --
_turn_tools_called tracks which tool NAMES actually ran, and a persistence
request with none of _PERSISTENCE_TOOLS among them queues a same-conversation
correction for the next turn via _pending_confab_check /
pop_pending_confab_check, the same delivered-exactly-once contract as
_pending_progress / pop_pending_progress. Run with:
    .venv\\Scripts\\pytest backend/brain/test_engine_confab_guard.py -v
"""

import asyncio

import pytest

from backend.brain import engine


@pytest.fixture(autouse=True)
def _reset_turn_state():
    engine._reset_turn_stats()
    engine._pending_confab_check.clear()
    yield
    engine._reset_turn_stats()
    engine._pending_confab_check.clear()


# ---------------------------------------------------------------------------
# _turn_tools_called — tracked by _exec_tool alongside the existing counters
# ---------------------------------------------------------------------------

def test_reset_turn_stats_clears_tools_called():
    engine._turn_tools_called.add("vault_write")
    engine._reset_turn_stats()
    assert engine._turn_tools_called == set()


def test_exec_tool_records_the_tool_name(monkeypatch):
    async def fake_vault_write(**kwargs):
        return "Confirmed written to jarvis-vault/topics/note.md"

    monkeypatch.setitem(engine.TOOL_DISPATCH, "vault_write", fake_vault_write)

    async def fake_send(msg):
        pass

    asyncio.run(engine._exec_tool({"name": "vault_write", "input": {}}, fake_send))
    assert "vault_write" in engine._turn_tools_called


# ---------------------------------------------------------------------------
# _PERSISTENCE_TOOLS — scope
# ---------------------------------------------------------------------------

def test_persistence_tools_set_matches_the_writers_plus_skill():
    assert engine._PERSISTENCE_TOOLS == frozenset({
        "schedule_night_task", "schedule_reminder", "schedule_task", "vault_write",
        "vault_write_queue", "skill", "notion_action", "watch_action",
        "standing_instruction", "forecast", "correct_memory", "person_profile", "week_plan", "portfolio",
        "goal",                       # G1a: "I've started a goal" must have a start behind it
    })


# ---------------------------------------------------------------------------
# pop_pending_confab_check — delivered exactly once, same as pop_pending_progress
# ---------------------------------------------------------------------------

def test_pop_pending_confab_check_empty_when_nothing_pending():
    assert engine.pop_pending_confab_check("local") == ""


def test_pop_pending_confab_check_formats_and_clears():
    engine._pending_confab_check["local"] = "schedule that FOMC task"
    note = engine.pop_pending_confab_check("local")
    assert "System check" in note
    assert "schedule that FOMC task" in note
    # Delivered exactly once.
    assert engine.pop_pending_confab_check("local") == ""


def test_pop_pending_confab_check_is_scoped_per_channel():
    engine._pending_confab_check["discord"] = "queue this"
    assert engine.pop_pending_confab_check("local") == ""
    assert "queue this" in engine.pop_pending_confab_check("discord")
