"""Tests for the positions/predictions additions to backend/brain/consolidation.py.

Covers a truncation-risk class introduced by array-bearing output fields
(_loads_salvaged against a response cut off mid predictions_scored array),
prompt formatting with the new blocks, and an end-to-end consolidate_session
pass against a stubbed LLM call + a tmp vault (no real Qdrant/LLM touched).
positions.md's own patch/parse/render logic is covered by test_positions.py —
this file only exercises how consolidate_session wires a positions_patch
output into that module. Run with:
    .venv\\Scripts\\pytest backend/brain/test_consolidation_fields.py -v
"""

import asyncio
import json

import pytest

from backend.brain import consolidation
from backend.brain import positions as positions_mod
from backend.brain import predictions as predictions_mod
from backend.tools import vault as vault_mod


# ---------------------------------------------------------------------------
# _loads_salvaged / is_likely_truncated — new risk: truncation mid
# predictions_scored array (positions.md's own truncation risk is gone --
# see test_positions.py's module docstring)
# ---------------------------------------------------------------------------

def test_loads_salvaged_recovers_truncated_array_field():
    # Simulate a response cut off mid-way through the predictions_scored
    # array — earlier fields (letter, owner_update) must survive intact.
    raw = (
        '{"letter": "Dear future me, this session went well.", '
        '"owner_update": "## Situation\\nstill true", '
        '"predictions_scored": [{"n": 1, "verdict": "right", "evidence": "he loved i'
    )
    data = consolidation._loads_salvaged(raw)
    assert data is not None
    assert data["letter"] == "Dear future me, this session went well."
    assert data["owner_update"] == "## Situation\nstill true"
    # the truncated array itself may or may not survive depending on where
    # the cut landed -- what matters is the parse doesn't fail outright and
    # earlier fields are intact. Confirm data is a usable dict either way.
    assert isinstance(data, dict)


def test_loads_salvaged_still_handles_clean_json_with_new_fields():
    raw = json.dumps({
        "letter": "hi",
        "owner_update": None,
        "queue_update": None,
        "predictions_scored": [{"n": 1, "verdict": "right", "evidence": "x"}],
        "predictions_new": ["he'll bring up Y"],
        "positions_patch": [],
        "topic_filename": None, "topic_content": None,
        "significant": None, "creative_filename": None, "creative_content": None,
        "mood_patterns_update": None,
    })
    data = consolidation._loads_salvaged(raw)
    assert data["predictions_scored"] == [{"n": 1, "verdict": "right", "evidence": "x"}]
    assert data["predictions_new"] == ["he'll bring up Y"]


def test_call_compat_disables_thinking(monkeypatch):
    """_call_compat must pass its active provider's no_thinking_body as extra_body --
    both providers' standard tier thinks by default, and that reasoning shares
    max_tokens with the strict-JSON consolidation output (same live failure class
    as the 2026-09-01 research picker/synthesis budget starvation). A behavioral
    check against a fake streaming client rather than source-inspection, since the
    call site now reads provider.no_thinking_body rather than a fixed
    KIMI_NO_THINKING_BODY literal -- true for both the Kimi and OpenRouter
    provider descriptor."""
    import openai as openai_module
    from backend.brain import router as router_mod

    captured = []

    class _FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _FakeStream()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = _FakeChat()

    monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    for provider in (router_mod.OPENROUTER,):
        captured.clear()
        asyncio.run(consolidation._call_compat("prompt"))
        _assert_no_thinking(captured[0]["extra_body"], provider)
        assert captured[0]["model"] == provider.standard


# ---------------------------------------------------------------------------
# is_likely_truncated — the detection signal that gates a continuation call
# ---------------------------------------------------------------------------

def test_is_likely_truncated_true_for_unclosed_string():
    assert consolidation.is_likely_truncated('{"letter": "cut off mid-sentence')


def test_is_likely_truncated_true_for_unclosed_brace():
    assert consolidation.is_likely_truncated('{"letter": "fine", "owner_update": "also fine"')


def test_is_likely_truncated_false_for_complete_json():
    assert not consolidation.is_likely_truncated('{"letter": "a complete response", "queue_update": null}')


def test_is_likely_truncated_false_for_empty_string():
    assert not consolidation.is_likely_truncated("")


def test_is_likely_truncated_true_for_trailing_backslash_escape():
    # ends mid-escape-sequence inside a string -- still an open string
    assert consolidation.is_likely_truncated('{"letter": "ends with a backslash\\')


# ---------------------------------------------------------------------------
# _continue_call — one follow-up call to recover truncated output
# ---------------------------------------------------------------------------

def test_continue_call_concatenates_partial_and_continuation(monkeypatch):
    async def fake_call_compat(prompt):
        # The continuation prompt must carry the partial text forward.
        assert "cut off mid" in prompt
        return '-sentence but now finished"}'
    monkeypatch.setattr(consolidation, "_call_compat", fake_call_compat)

    result = asyncio.run(consolidation._continue_call(
        "original prompt", '{"letter": "cut off mid', max_tokens=100,
    ))
    assert result == '{"letter": "cut off mid-sentence but now finished"}'
    parsed = consolidation._loads_salvaged(result)
    assert parsed == {"letter": "cut off mid-sentence but now finished"}


def test_continue_call_returns_partial_unchanged_when_continuation_fails(monkeypatch):
    async def empty_call(prompt):
        return ""
    monkeypatch.setattr(consolidation, "_call_compat", empty_call)

    partial = '{"letter": "cut off mid'
    result = asyncio.run(consolidation._continue_call(
        "original prompt", partial, max_tokens=100,
    ))
    assert result == partial


# ---------------------------------------------------------------------------
# _generate_consolidation — continuation is attempted on a truncated raw
# response before falling through to the existing salvage/reject path
# ---------------------------------------------------------------------------

def test_generate_consolidation_recovers_truncated_response_via_continuation(monkeypatch):
    from backend.brain import router as router_mod

    calls = {"n": 0}

    async def fake_call_compat(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call: a response cut off mid-way through the letter field.
            return '{"letter": "Dear future me, this session went really'
        # Second call: the continuation, asked for via _CONTINUATION_INSTRUCTION.
        assert "cut off" in prompt or "Continue EXACTLY" in prompt
        return (
            ' well and I learned something.", "owner_update": null, '
            '"queue_update": null, "predictions_scored": [], "predictions_new": null, '
            '"positions_patch": [], "topic_filename": null, "topic_content": null, '
            '"significant": null, "creative_filename": null, "creative_content": null, '
            '"mood_patterns_update": null}'
        )
    monkeypatch.setattr(consolidation, "_call_compat", fake_call_compat)

    monkeypatch.setattr(consolidation, "_facts_sample_sync", lambda: "")
    monkeypatch.setattr(consolidation, "_listening_log_tail", lambda: "")

    data = asyncio.run(consolidation._generate_consolidation(
        threads=[{"timestamp": 1000.0, "high": True, "user": "hi", "assistant": "hi"}],
        prior_letter="", owner_md="", queue_raw="", sleep=None,
    ))

    assert calls["n"] == 2   # exactly one continuation attempt, not a retry loop
    assert data is not None
    assert data["letter"] == "Dear future me, this session went really well and I learned something."


# ---------------------------------------------------------------------------
# Prompt formatting with the new input blocks
# ---------------------------------------------------------------------------

def test_prompt_formats_with_empty_new_blocks():
    out = consolidation._PROMPT.format(
        prior_letter_block="x", owner_block="x", queue_block="x", threads_block="x",
        facts_block="x", listening_block="x", sleep_block="x",
        positions_block="(none yet)", predictions_block="(none — output an empty predictions_scored array)",
        mood_register="x", date_today="2026-07-16", people_block="(none)",
        tool_record_block="(none)", tool_facts="(none)",
        portrait_field=consolidation._PORTRAIT_PATCH_FIELD,
        portrait_rules=consolidation._PORTRAIT_PATCH_RULES,
    )
    assert "(none yet)" in out
    assert "predictions_scored" in out
    assert "positions_patch" in out


def test_prompt_formats_with_full_new_blocks():
    out = consolidation._PROMPT.format(
        prior_letter_block="x", owner_block="x", queue_block="x", threads_block="x",
        facts_block="x", listening_block="x", sleep_block="x",
        positions_block="## nietzsche\n**Stance:** ...",
        predictions_block="1. (made 2026-07-14) He'll bring up X.",
        mood_register="x", date_today="2026-07-16", people_block="(none)",
        tool_record_block="(none)", tool_facts="(none)",
        portrait_field=consolidation._PORTRAIT_PATCH_FIELD,
        portrait_rules=consolidation._PORTRAIT_PATCH_RULES,
    )
    assert "## nietzsche" in out
    assert "He'll bring up X." in out


# ---------------------------------------------------------------------------
# consolidate_session end-to-end (stubbed LLM, tmp vault, no real Qdrant)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    v = tmp_path / "jarvis-vault"
    for folder in vault_mod.FOLDERS:
        (v / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vault_mod, "_VAULT", v)
    monkeypatch.setattr(consolidation, "_VAULT", v)
    monkeypatch.setattr(predictions_mod, "_PRED_FILE", v / "people" / "predictions.md")

    async def no_dedup():
        return 0
    monkeypatch.setattr("backend.brain.memory.dedup_facts", no_dedup, raising=False)

    async def no_threads_stub(*a, **kw):
        return []
    monkeypatch.setattr(consolidation, "_snapshot_unconsolidated_ids_sync", lambda *a, **kw: [])

    async def no_sleep():
        return None
    monkeypatch.setattr(consolidation, "latest_sleep", no_sleep)

    return v


def _fake_threads(n: int, sig: float = 0.6):
    return [
        {
            "id": f"t{i}", "session_id": "abcd1234-0000-0000-0000-000000000000",
            "timestamp": 1000.0 + i, "significance": sig, "high": True,
            "user": f"user msg {i}", "assistant": f"jarvis reply {i}",
            "summary": "", "channel": "local",
        }
        for i in range(n)
    ]


def test_consolidate_session_skips_predictions_below_min_threads(tmp_vault, monkeypatch):
    threads = _fake_threads(2)  # below _MIN_THREADS_FOR_PREDICTIONS

    async def fake_threads_for_session(sid):
        return threads
    monkeypatch.setattr(consolidation, "threads_for_session", fake_threads_for_session)

    captured = {}

    async def fake_generate(*args, **kwargs):
        captured["pending_preds_block"] = kwargs.get("pending_preds_block")
        return {
            "letter": "a short letter",
            "owner_update": None, "queue_update": None,
            "predictions_scored": [{"n": 1, "verdict": "right", "evidence": "should be ignored"}],
            "predictions_new": ["should also be ignored"],
            "positions_patch": [],
            "topic_filename": None, "topic_content": None, "significant": None,
            "creative_filename": None, "creative_content": None, "mood_patterns_update": None,
        }
    monkeypatch.setattr(consolidation, "_generate_consolidation", fake_generate)

    asyncio.run(consolidation.consolidate_session("abcd1234-0000-0000-0000-000000000000"))

    assert captured["pending_preds_block"] == ""
    # predictions.md must not have been created -- surgery was skipped entirely
    assert not (tmp_vault / "people" / "predictions.md").exists()


def test_consolidate_session_scores_and_appends_predictions(tmp_vault, monkeypatch):
    threads = _fake_threads(4)

    async def fake_threads_for_session(sid):
        return threads
    monkeypatch.setattr(consolidation, "threads_for_session", fake_threads_for_session)

    # Seed an existing pending prediction
    existing = predictions_mod.render_file(
        {"right": 0, "wrong": 0, "unclear": 0},
        [predictions_mod.Prediction(status="pending", date="2026-07-10", sid="9f00aa11", text="He'll push back on X.")],
    )
    (tmp_vault / "people" / "predictions.md").write_text(existing, encoding="utf-8")

    async def fake_generate(*args, **kwargs):
        assert "He'll push back on X." in kwargs.get("pending_preds_block", "")
        return {
            "letter": "a short letter",
            "owner_update": None, "queue_update": None,
            "predictions_scored": [{"n": 1, "verdict": "wrong", "evidence": "he agreed"}],
            "predictions_new": ["He'll bring up Y next time."],
            "positions_patch": [],
            "topic_filename": None, "topic_content": None, "significant": None,
            "creative_filename": None, "creative_content": None, "mood_patterns_update": None,
        }
    monkeypatch.setattr(consolidation, "_generate_consolidation", fake_generate)

    asyncio.run(consolidation.consolidate_session("abcd1234-0000-0000-0000-000000000000"))

    final_text = (tmp_vault / "people" / "predictions.md").read_text(encoding="utf-8")
    counters, preds = predictions_mod.parse_file(final_text)
    assert counters["wrong"] == 1
    statuses = {x.text.split(" —")[0]: x.status for x in preds}
    assert statuses["He'll push back on X."] == "wrong"
    assert statuses["He'll bring up Y next time."] == "pending"


def test_consolidate_session_applies_positions_patch(tmp_vault, monkeypatch):
    threads = _fake_threads(4)

    async def fake_threads_for_session(sid):
        return threads
    monkeypatch.setattr(consolidation, "threads_for_session", fake_threads_for_session)

    async def fake_generate(*args, **kwargs):
        return {
            "letter": "a short letter",
            "owner_update": None, "queue_update": None,
            "predictions_scored": [], "predictions_new": None,
            "positions_patch": [{
                "topic": "nietzsche", "action": "add",
                "stance": "Eternal recurrence is best read as a practical test, not a metaphysics.",
                "sources": "[[topics/nietzsche]]",
            }],
            "topic_filename": None, "topic_content": None, "significant": None,
            "creative_filename": None, "creative_content": None, "mood_patterns_update": None,
        }
    monkeypatch.setattr(consolidation, "_generate_consolidation", fake_generate)

    asyncio.run(consolidation.consolidate_session("abcd1234-0000-0000-0000-000000000000"))

    written = (tmp_vault / "self" / "positions.md").read_text(encoding="utf-8")
    parsed = positions_mod.parse_file(written)
    assert "nietzsche" in parsed
    assert "practical test" in parsed["nietzsche"].stance
    assert parsed["nietzsche"].sources == "[[topics/nietzsche]]"


def test_consolidate_session_ignores_invalid_positions_patch(tmp_vault, monkeypatch):
    """A revise patch for a topic that doesn't exist yet is invalid -- no
    full-rewrite truncation risk remains to reject, but a malformed patch
    must still be a no-op rather than corrupting or blanking the file."""
    threads = _fake_threads(4)

    async def fake_threads_for_session(sid):
        return threads
    monkeypatch.setattr(consolidation, "threads_for_session", fake_threads_for_session)

    existing_positions = "# Positions\n\n## nietzsche\n**Stance:** original stance text.\n**Formed:** 2026-07-01\n"
    (tmp_vault / "self" / "positions.md").write_text(existing_positions, encoding="utf-8")

    async def fake_generate(*args, **kwargs):
        return {
            "letter": "a short letter",
            "owner_update": None, "queue_update": None,
            "predictions_scored": [], "predictions_new": None,
            "positions_patch": [{
                "topic": "camus", "action": "revise",  # no existing "camus" position -- ignored
                "stance": "should never be applied",
            }],
            "topic_filename": None, "topic_content": None, "significant": None,
            "creative_filename": None, "creative_content": None, "mood_patterns_update": None,
        }
    monkeypatch.setattr(consolidation, "_generate_consolidation", fake_generate)

    asyncio.run(consolidation.consolidate_session("abcd1234-0000-0000-0000-000000000000"))

    # existing file must be untouched -- the only patch entry was invalid
    still_there = (tmp_vault / "self" / "positions.md").read_text(encoding="utf-8")
    assert still_there == existing_positions


def _assert_no_thinking(extra_body, provider):
    """The cheap tier must disable reasoning; since 2026-09-21 the same body also
    carries the OpenRouter routing preference (router.with_provider_prefs), so the
    disable half is asserted exactly and the provider half alongside it.

    Provider-agnostic on purpose: Moonshot's disable key is `thinking`, OpenRouter's is
    `reasoning`, so this compares against whatever that provider's own no_thinking_body
    actually contains rather than hardcoding either spelling.
    """
    for k, v in provider.no_thinking_body.items():
        assert extra_body[k] == v, (k, extra_body.get(k), v)
    if provider.provider_prefs:
        assert extra_body["provider"] == provider.provider_prefs
    else:
        assert set(extra_body) == set(provider.no_thinking_body)
