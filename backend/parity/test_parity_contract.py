"""A00 parity contract: the capability surface a refactor must not silently change.

The architecture roadmap's first work package asks for exactly this before any module
moves: "Capture the 22 tools plus dynamic replan, their schemas/action variants, command
behaviors, workflow outputs, background permissions". The point is that a later slice
which drops a tool, renames a parameter, quietly widens the background worker's
authority, or loses a Discord command fails HERE rather than in production.

`tool_registry.json` is a snapshot, not a specification. Changing the capability surface
on purpose is fine -- regenerate it in the same commit as the change so the diff shows
what moved:

    python -m backend.parity.snapshot

The roadmap also warns that fixtures can preserve a bug. This file therefore locks the
capability *surface* (names, parameter names/types/enums, required fields, authority)
and deliberately does not assert anything about behavior that is known to be wrong.
"""
import json
from pathlib import Path

from backend.parity.snapshot import build_snapshot

_FIXTURE = Path(__file__).parent / "tool_registry.json"


def _saved():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_tool_surface_matches_the_recorded_contract():
    """Names, parameters, types, action enums and required fields are all unchanged."""
    current, saved = build_snapshot(), _saved()
    assert [t["name"] for t in current["tools"]] == [t["name"] for t in saved["tools"]], (
        "A tool was added, removed or renamed. If deliberate, regenerate the fixture: "
        "python -m backend.parity.snapshot"
    )
    by_name = {t["name"]: t for t in saved["tools"]}
    for tool in current["tools"]:
        assert tool == by_name[tool["name"]], (
            f"Schema drift in {tool['name']!r}. Parameter names, types, action enums and "
            "required fields are part of the parity contract -- preserving only the tool "
            "name is insufficient."
        )


def test_tool_count_is_still_thirty_three():
    """The roadmap's 22, plus three later tools, correct_memory, person_profile,
    vault_send (H4), web_research (TinyFish), schedule_task and week_plan (P3) and goal
    (G1a). The lab's three tools (thread, lab_show, lab_python) were removed with the lab on
    24 October 2026. A drop means a lost capability."""
    assert build_snapshot()["tool_count"] == 33 == _saved()["tool_count"]


def test_background_authority_has_not_widened():
    """Background workers get an explicitly restricted subset -- the roadmap calls for
    'explicit inherited authorization rather than automatically giving every worker
    every tool'. Widening it silently is a privilege escalation."""
    current, saved = build_snapshot(), _saved()
    assert current["background_allowed_tools"] == saved["background_allowed_tools"]
    granted = set(current["background_allowed_tools"])
    all_tools = {t["name"] for t in current["tools"]}
    assert granted < all_tools, "background must remain a strict subset of the registry"
    # Deliberate exclusions, each with a stated reason in CLAUDE.md. A self-contained
    # overnight task has no conversation context to justify these judgment calls.
    for denied in ("run_in_background", "vault_write_queue",
                   "notion_action", "ask_clarifying_question", "schedule_reminder",
                   "schedule_night_task"):
        assert denied not in granted, f"{denied} must stay denied to background workers"


def test_discord_commands_are_all_still_present():
    current, saved = build_snapshot(), _saved()
    assert current["discord_commands"] == saved["discord_commands"]
    for cmd in ("/new", "/pace", "/status", "/cost", "/about", "/brief", "/commands"):
        assert cmd in current["discord_commands"]


def test_commands_help_text_is_locked_too():
    """`/commands` is the live, authoritative answer to "what can you do?" -- CLAUDE.md
    added it precisely because that question used to be unanswerable. A command silently
    losing its documented behavior while keeping its name is a real regression."""
    assert build_snapshot()["discord_command_help"] == _saved()["discord_command_help"]


def test_request_replan_exists_but_is_never_globally_registered():
    """The dynamic control the roadmap asks us to preserve. It is offered only while a
    plan is active and must NOT leak into the global registry -- a turn without a plan
    must never see it."""
    from backend.brain import engine
    from backend.tools.registry import TOOLS

    assert engine._REQUEST_REPLAN_TOOL["name"] == "request_replan"
    assert "request_replan" not in {t["name"] for t in TOOLS}


def test_fixture_is_in_sync_with_the_generator():
    """Guards the fixture itself: a stale snapshot would silently stop protecting
    anything while still passing the comparisons above."""
    assert build_snapshot() == _saved(), (
        "tool_registry.json is stale. Regenerate: python -m backend.parity.snapshot"
    )
