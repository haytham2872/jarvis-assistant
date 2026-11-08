"""Consolidation may close, hold and reword queue items, and is told what the tools confirm."""
import asyncio

from backend.brain import chronos, consolidation
from backend.storage import goals, skills
from backend.tools import registry


def _instruction() -> str:
    start = consolidation._PROMPT.index('"queue_update"')
    return consolidation._PROMPT[start:start + 1500]


def test_queue_instruction_allows_closing_and_hold():
    text = _instruction()
    assert "verbatim" not in text.lower()
    assert "hold" in text
    assert "tool facts" in text.lower()
    assert "resolved" in text


def test_tool_facts_block(monkeypatch):
    monkeypatch.setattr(skills, "use_counts_by_via",
                        lambda: {"stock-research": {"routine": 60, "model": 2}})
    monkeypatch.setattr(goals, "list_goals",
                        lambda *a, **k: [{"title": "Ship the app", "state": "active"}])
    monkeypatch.setattr(chronos, "_read_schedule",
                        lambda **k: ["[reminder] 2026-07-08 10:00 | once | x",
                                     "[night] 2026-07-07 | after= | y", "junk"])
    out = consolidation._tool_facts()
    assert "60 times as a scheduled routine" in out and "2 times opened by you" in out
    assert "Ship the app" in out and "active" in out
    assert "2 scheduled" in out
    assert len(out) < 800


def test_tool_facts_parts_empty_or_failing(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(skills, "use_counts_by_via", boom)
    monkeypatch.setattr(goals, "list_goals", lambda *a, **k: [])
    monkeypatch.setattr(chronos, "_read_schedule", boom)
    assert consolidation._tool_facts() == ""


def _render(monkeypatch, facts: str) -> str:
    seen = {}

    async def fake_call(prompt):
        seen["p"] = prompt
        return ""
    monkeypatch.setattr(consolidation, "_call_compat", fake_call)
    monkeypatch.setattr(consolidation, "_tool_facts", lambda: facts)
    asyncio.run(consolidation._generate_consolidation([], "", "", "[open] 2026-01-01 — x", None))
    return seen["p"]


def test_prompt_carries_tool_facts(monkeypatch):
    p = _render(monkeypatch, "Skills used: stock-research (routine 60)")
    assert "What the tools confirm right now:\nSkills used: stock-research (routine 60)" in p


def test_prompt_tool_facts_none_when_empty(monkeypatch):
    p = _render(monkeypatch, "")
    assert "What the tools confirm right now:\n(none)" in p


def test_registry_queue_statuses():
    desc = next(t["description"] for t in registry.TOOLS if t["name"] == "vault_write_queue")
    assert "open / in_progress / hold / resolved" in desc


def test_the_queue_rules_close_what_the_tools_already_confirm():
    """Live replay, 7 November: 'routine 60' never closed 'confirm first skill-driven
    firing'; with these rules it closed 2 of 3, and the portrait blocker 3 of 3."""
    prompt = " ".join(consolidation._PROMPT.split())
    assert "the tool record is the confirmation" in prompt
    assert "portrait is resolved" in prompt
