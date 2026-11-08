"""Session-end profile edits (consolidation's people_patch).

Live (2026-09-26): asked to record lasting facts about a profiled friend in the moment,
the model did so for 3 of 12. Consolidation reads the whole session anyway, so it
proposes the edits there, and code keeps one only when its quote is Sam's own words
from that session. These tests pin that rule end to end, with the LLM stubbed.
"""
import asyncio

import pytest

from backend.brain import consolidation, people as P
from backend.brain import predictions as predictions_mod
from backend.tools import vault as vault_mod

SID = "feed1234-0000-0000-0000-000000000000"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    v = tmp_path / "jarvis-vault"
    for folder in vault_mod.FOLDERS:
        (v / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vault_mod, "_VAULT", v)
    monkeypatch.setattr(consolidation, "_VAULT", v)
    monkeypatch.setattr(vault_mod, "_index_note_async", lambda *a, **k: None)
    monkeypatch.setattr(predictions_mod, "_PRED_FILE", v / "people" / "predictions.md")
    monkeypatch.setattr(P, "_today", lambda: "2026-09-27")

    async def no_dedup():
        return 0
    monkeypatch.setattr("backend.brain.memory.dedup_facts", no_dedup, raising=False)
    monkeypatch.setattr(consolidation, "_snapshot_unconsolidated_ids_sync", lambda *a, **kw: [])
    monkeypatch.setattr(consolidation, "_mark_facts_consolidated_sync", lambda *a, **kw: None)

    async def no_sleep():
        return None
    monkeypatch.setattr(consolidation, "latest_sleep", no_sleep)
    return v


USER = [
    "Karim texted me, his bakery had a really good first week",
    "also Karim told me he's allergic to hazelnuts which is funny for a baker",
    "anyway what should I cook tonight",
]


def _threads():
    return [{"id": f"t{i}", "session_id": SID, "timestamp": 1000.0 + i, "significance": 0.2,
             "high": False, "user": u, "assistant": "ok",
             "summary": f"User: {u[:40]} | JARVIS: ok", "channel": "discord"}
            for i, u in enumerate(USER)]


PATCH = [
    # his words, verbatim (case and punctuation aside)
    {"name": "Karim", "section": "Things to remember", "text": "Allergic to hazelnuts",
     "quote": "he's allergic to hazelnuts"},
    {"name": "karim", "section": "Current situation", "text": "The bakery had a good first week",
     "quote": "his bakery had a really good first week"},
    # a paraphrase is not his words
    {"name": "Karim", "section": "Personality and temperament", "text": "Resilient",
     "quote": "he is a resilient guy"},
    # someone not offered to this session
    {"name": "Samir", "section": "Current situation", "text": "Moved", "quote": "anyway what should I cook"},
    "not an entry",
]


def _stub(monkeypatch, patch, captured):
    async def fake_threads(sid):
        return _threads()
    monkeypatch.setattr(consolidation, "threads_for_session", fake_threads)

    async def fake_generate(*args, **kwargs):
        captured["people_block"] = kwargs.get("people_block")
        return {"letter": "a letter", "owner_update": None, "queue_update": None,
                "predictions_scored": [], "predictions_new": [], "positions_patch": [],
                "people_patch": patch}
    monkeypatch.setattr(consolidation, "_generate_consolidation", fake_generate)


def test_only_his_own_words_reach_the_profile(tmp_vault, monkeypatch):
    run(P.person_profile("create", name="Karim", kind="known", relation="best friend"))
    run(P.person_profile("create", name="Samir", kind="known"))
    captured = {}
    _stub(monkeypatch, PATCH, captured)
    run(consolidation.consolidate_session(SID))

    karim = (tmp_vault / "people" / "Karim.md").read_text(encoding="utf-8")
    assert "Allergic to hazelnuts — Sam, 2026-09-27" in karim
    assert "The bakery had a good first week — Sam" in karim
    assert "Resilient" not in karim                              # paraphrased quote
    assert "Moved" not in (tmp_vault / "people" / "Samir.md").read_text(encoding="utf-8")
    # the prompt named who came up, with their sections and what is already known
    assert "Karim (known, best friend). Sections:" in captured["people_block"]
    assert "    Things to remember: Practical details" in captured["people_block"]
    assert "Samir" not in captured["people_block"]
    from backend.storage import consolidation_state as cs
    assert "people" in cs.completed_outputs(SID)


def test_a_replay_does_not_add_the_entries_twice(tmp_vault, monkeypatch):
    run(P.person_profile("create", name="Karim", kind="known"))
    _stub(monkeypatch, PATCH, {})
    run(consolidation.consolidate_session(SID))
    first = (tmp_vault / "people" / "Karim.md").read_text(encoding="utf-8")
    run(consolidation.consolidate_session(SID))          # done: skipped entirely
    from backend.storage import consolidation_state as cs
    cs.record_output(SID, "people", False, "forced retry")
    run(P.apply_session_patch(PATCH, ["Karim"], USER))   # and a forced reapply
    assert (tmp_vault / "people" / "Karim.md").read_text(encoding="utf-8").count(
        "Allergic to hazelnuts") == first.count("Allergic to hazelnuts") == 1


def test_no_profile_named_means_no_people_block(tmp_vault, monkeypatch):
    captured = {}
    _stub(monkeypatch, [], captured)
    run(consolidation.consolidate_session(SID))
    assert captured["people_block"] == ""


def test_a_failed_profile_write_leaves_the_output_to_retry(tmp_vault, monkeypatch):
    run(P.person_profile("create", name="Karim", kind="known"))
    _stub(monkeypatch, PATCH, {})

    async def down(*a, **k):
        return "Error: disk full"
    monkeypatch.setattr(P, "person_profile", down)
    run(consolidation.consolidate_session(SID))
    from backend.storage import consolidation_state as cs
    assert "people" in cs.failed_outputs(SID)


def test_verify_is_pure_and_strict():
    by, rejected = P.verify_session_patch(PATCH, ["Karim"], USER)
    assert [e["text"] for e in by["Karim"]] == ["Allergic to hazelnuts",
                                                "The bakery had a good first week"]
    assert all(e["source"] == "owner" for e in by["Karim"]) and rejected == 3
    short = [{"name": "Karim", "section": "Snapshot", "text": "x", "quote": "Karim"}]
    assert P.verify_session_patch(short, ["Karim"], USER) == ({}, 1)


def test_low_significance_exchanges_show_his_full_words():
    block = consolidation._format_threads_block(_threads())
    assert "  U: also Karim told me he's allergic to hazelnuts which is funny for a baker" in block


def test_the_prompt_asks_for_quotes_of_his_words():
    assert "people_patch" in consolidation._PROMPT and "HIS exact" in consolidation._PROMPT
    out = consolidation._PROMPT.format(
        prior_letter_block="x", owner_block="x", queue_block="x", threads_block="x",
        facts_block="x", listening_block="x", sleep_block="x", positions_block="x",
        predictions_block="x", mood_register="x", date_today="2026-09-27",
        people_block="- Karim (known)", tool_record_block="(none)", tool_facts="(none)",
        portrait_field="x", portrait_rules="x")
    assert "- Karim (known)" in out
