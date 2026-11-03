"""Tests for the 2026-09-09 fix: what _chat_compat RETURN must
be only the final synthesis turn's text, not every intermediate tool-call
iteration's own narration concatenated together.

Live incident this fixes: a Discord reply to "what happens when rumination
launches at night?" came back as a wall of "Let me check X... I found Y...
let me dig deeper" tool-planning narration glued in front of (and at times
instead of) the actual answer. Root cause: both loops did
`full_response += text` unconditionally on every iteration, including
iterations that produced only pre-tool-call narration, and returned that
whole accumulation -- which becomes the Discord reply verbatim
(main.py's run_pipeline_from_discord returns chat()'s result untouched),
the stored conversation-history turn, and the mem0/exchange-index text.
Because that narration then got stored as the "assistant" side of the
exchange, it also resurfaced in later passive exchange recall, compounding
the problem it caused.

The fix tracks the CURRENT iteration's text separately (`final_text`,
overwritten every pass, not accumulated) and returns that on normal
completion -- while the cap-hit path still returns the full accumulated
trail on purpose, since there's no clean final answer to fall back to there.

Same discipline as test_engine_steering.py/test_engine_plan_scope.py: drives
the REAL _chat_compat loops with only the streaming
client/_exec_tool faked.
"""

import asyncio

import pytest

from backend.brain import engine


def run(coro):
    return asyncio.run(coro)


async def _noop_send(msg):
    pass


@pytest.fixture(autouse=True)
def _reset_turn_state():
    engine._reset_turn_stats()
    yield
    engine._reset_turn_stats()


async def _ok_exec_tool(tc, send):
    return tc, "tool result", True


# ---------------------------------------------------------------------------
# _chat_compat (OpenAI-compatible / Kimi / openrouter path)
# ---------------------------------------------------------------------------

def test_chat_compat_normal_completion_returns_only_final_text(monkeypatch):
    calls = {"n": 0}

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            return ("Let me check both the evolution file and the code graph.",
                     "", [{"id": "1", "name": "vault_read", "input": {}}])
        if i == 1:
            return ("Let me look at the actual source files now.",
                     "", [{"id": "2", "name": "code_graph", "input": {}}])
        return "Rumination runs on a 60s loop once a channel is idle 30+ minutes.", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="discord",
    ))

    assert result == "Rumination runs on a 60s loop once a channel is idle 30+ minutes."
    assert "Let me check" not in result
    assert "Let me look at" not in result


def test_chat_compat_cap_hit_still_returns_the_full_narration_trail(monkeypatch):
    """Unlike normal completion, a cap-hit turn never produces a clean final
    answer -- the accumulated trail is the only real content, so it must
    stay in the return value alongside the cap message (unchanged
    behavior, deliberately not touched by this fix)."""
    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        return ("Still searching for that.",
                "", [{"id": "1", "name": "web_browse", "input": {}}])

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="discord",
    ))

    assert "Still searching for that." in result
    assert engine._LOOP_CAP_MESSAGE in result


def test_chat_compat_single_turn_unaffected(monkeypatch):
    """A plain turn with no tool calls at all -- final_text and the old
    full_response accumulation are identical, so this must behave exactly
    as before."""
    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        return "Hey, what's up?", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="discord",
    ))

    assert result == "Hey, what's up?"
