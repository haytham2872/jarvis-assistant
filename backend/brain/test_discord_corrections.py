"""A direct correction outlives the turn and hides superseded assertions."""
import asyncio

import pytest

from backend.brain import discord_context, memory, research
from backend.storage import db, fact_corrections
from backend.tools import vault


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "runtime.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def test_correction_suppresses_fact_and_survives_restart(monkeypatch):
    old = "User commented on No Clean Singing in 2014 and likes Hank3"
    cid = fact_corrections.add(memory.USER_ID, old,
                               "I never wrote that comment or heard Hank3",
                               ["No Clean Singing", "Hank3"])
    assert cid
    db._reset_for_tests()
    corrections = fact_corrections.list_for_user(memory.USER_ID)
    assert fact_corrections.suppresses(old, corrections)
    assert fact_corrections.suppresses("Hank3 is Sam's favorite", corrections)
    assert not fact_corrections.suppresses("Tesla Q2 operating margin", corrections)

    class Mem:
        def search(self, *_args, **_kwargs):
            return {"results": [{"id": "bad", "memory": old, "score": 0.9},
                                {"id": "good", "memory": "User likes Fairuz", "score": 0.8}]}

    monkeypatch.setattr(memory, "_get_mem", lambda: Mem())
    monkeypatch.setattr(memory, "_get_consolidated_ids", lambda: set())
    monkeypatch.setattr(memory, "_get_assistant_attributed_ids", lambda: set())
    active = memory._search_facts_sync("music", 5)
    assert all("No Clean Singing" not in h["text"] for h in active)
    assert any("Fairuz" in h["text"] for h in active)


def test_discord_fact_ingest_does_not_promote_bot_claim(monkeypatch):
    captured = []

    class Mem:
        def add(self, messages, user_id):
            captured.append(messages)

    monkeypatch.setattr(memory, "_get_mem", lambda: Mem())
    async def scenario():
        token = discord_context.current.set(discord_context.TurnContext("1"))
        try:
            await memory._do_ingest("I like Fairuz", "You commented on a metal blog")
        finally:
            discord_context.current.reset(token)
    asyncio.run(scenario())
    assert captured == [[{"role": "user", "content": "I like Fairuz"}]]


def test_correction_tool_suppresses_active_search_across_stores(monkeypatch):
    async def search(kind, *_args):
        return [{"_kind": kind, "text": "Sam wrote on No Clean Singing in 2014",
                 "score": 0.9},
                {"_kind": kind, "text": "Sam likes Fairuz", "score": 0.8}]

    monkeypatch.setattr(vault, "_search_one_kind", search)
    result = asyncio.run(memory.correct_memory(
        "Sam wrote on No Clean Singing in 2014",
        "Sam says he never wrote there",
        ["No Clean Singing"],
    ))
    assert result.startswith("Confirmed correction")
    for kind in ("fact", "vault", "exchange", "skill"):
        found = asyncio.run(vault.memory_search("Sam", kind=kind, top_k=2))
        assert "No Clean Singing" not in found
        assert "Fairuz" in found


def test_research_rejects_new_personal_testimony_and_requires_message_source():
    note = "A stranger said he liked Coltrane."
    message = "Your own 2014 comment on No Clean Singing proves your taste."
    assert research._new_personal_assertion("", note, message)
    assert not research._new_personal_assertion("", "Tesla operating margin fell.",
                                                "The quarter was weak.")
    findings = "Source https://example.com/tesla-q2 and another snippet"
    assert research._message_sources(
        {"message_sources": ["https://example.com/tesla-q2"]}, findings
    ) == ["https://example.com/tesla-q2"]
    assert research._message_sources(
        {"message_sources": ["https://fabricated.example/q2"]}, findings
    ) == []
    assert research._note_has_traceable_source(
        "Quarter margin: https://example.com/tesla-q2", findings)
    assert not research._note_has_traceable_source(
        "Quarter margin: https://fabricated.example/q2", findings)
