"""Tests for Step 1 of the JARVIS upgrade plan (2026-09-08): mid-turn steering.

1a (inbound) -- a steering message pushed while a tool-call loop is running
must be drained and folded into the running turn at the one clean boundary
that keeps DeepSeek's message-sequence validation happy: after a tool-call
iteration's results are appended, before the next model call. This file
drives the real `_chat_compat` loop (only the streaming
client is faked) across two tool-call iterations and inspects the SECOND
request's message list directly -- proving the injected correction actually
reaches the next model call in the right place, not just that some function
was called.

1b (outbound) -- `_ask_clarifying_question` is exercised against the plan's
own listed test cases: answer-arrives-in-time, timeout, the one-question
cap, the DM failing, and that it's unreachable from the autonomous paths.

No real network call, no real OpenAI/Anthropic client -- everything below
the streaming call is faked; `steering.py` itself is exercised for real
(not mocked), since it's the pure module under test for 1a and the timing
primitive 1b relies on.
"""

import asyncio
import types

import pytest

from backend.brain import engine
from backend.brain import steering
from backend.brain import background


def run(coro):
    return asyncio.run(coro)


async def _noop_send(msg):
    pass


@pytest.fixture(autouse=True)
def _reset_steering_and_turn_state():
    steering._reset_memory()
    engine._reset_turn_stats()
    yield
    steering._reset_memory()
    engine._reset_turn_stats()


# ---------------------------------------------------------------------------
# 1a -- _chat_compat (OpenAI-compatible path)
# ---------------------------------------------------------------------------

def test_chat_compat_injects_steering_message_at_the_clean_boundary(monkeypatch):
    """Push a correction while iteration 1 is 'in flight' (i.e. before the
    loop reaches its post-tool-results boundary); the SECOND request's
    message list must carry it as a new user-role message appearing AFTER
    every tool-role message from iteration 1 and BEFORE nothing else --
    the exact seam the DeepSeek trap note calls for."""
    captured_message_lists = []
    calls = {"n": 0}

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured_message_lists.append(list(messages))
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            # Queue the correction right as the first model call "returns" --
            # simulates it arriving while the loop was mid-iteration.
            steering.push("local", "actually, check Paris not London")
            return "", "", [{"id": "1", "name": "web_browse", "input": {"task": "x"}}]
        return "done — Paris it is", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    async def fake_exec_tool(tc, send):
        return tc, "tool result text", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    sent = []

    async def capture_send(msg):
        sent.append(msg)

    result = run(engine._chat_compat(
        engine._provider().standard, "system prompt", [{"role": "user", "content": "hi"}],
        capture_send, None, user_text="", channel="local",
    ))

    assert result == "done — Paris it is"
    assert calls["n"] == 2
    second_call_messages = captured_message_lists[1]

    # Well-formed: assistant(tool_calls) -> tool(result) -> user(steering) is
    # the tail of the message list handed to the second model call.
    assert second_call_messages[-3]["role"] == "assistant"
    assert second_call_messages[-2]["role"] == "tool"
    assert second_call_messages[-1]["role"] == "user"
    assert "actually, check Paris not London" in second_call_messages[-1]["content"]
    assert "mid-task" in second_call_messages[-1]["content"]  # format_interjection's framing

    absorbed = [m for m in sent if m.get("type") == "steering_absorbed"]
    assert len(absorbed) == 1
    assert absorbed[0]["channel"] == "local"
    assert "actually, check Paris not London" in absorbed[0]["text"]

    # Drained -- nothing left dangling in the queue after injection.
    assert steering.pending("local") is False


def test_chat_compat_no_steering_message_no_injection(monkeypatch):
    """Regression guard: the ordinary case (nothing queued) must be
    completely unaffected -- no extra message, no event."""
    calls = {"n": 0}

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            return "", "", [{"id": "1", "name": "web_browse", "input": {}}]
        return "final answer", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    async def fake_exec_tool(tc, send):
        return tc, "tool result", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    sent = []

    async def capture_send(msg):
        sent.append(msg)

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        capture_send, None, user_text="", channel="local",
    ))

    assert result == "final answer"
    assert not [m for m in sent if m.get("type") == "steering_absorbed"]


def test_chat_compat_steering_only_applies_to_its_own_channel(monkeypatch):
    """A message queued for "discord" must never leak into a "local" turn's
    context -- per-channel isolation, same as conversation_histories."""
    calls = {"n": 0}

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            steering.push("discord", "unrelated phone message")
            return "working", "", [{"id": "1", "name": "web_browse", "input": {}}]
        return "final", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    async def fake_exec_tool(tc, send):
        return tc, "tool result", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local",
    ))

    # Untouched -- the "local" turn never drained the "discord" queue.
    assert steering.pending("discord") is True


# ---------------------------------------------------------------------------
# 1b -- the clarifying question
# ---------------------------------------------------------------------------

def test_clarifying_question_uses_the_answer_when_it_arrives_in_time(monkeypatch):
    async def fake_notifier(text):
        # Simulate the reply landing on the "discord" steering channel shortly
        # after the DM is (successfully) sent.
        steering.push("discord", "the desk one", conversation=steering.DM)
        return True

    monkeypatch.setattr(engine, "_clarify_notifier", fake_notifier)
    monkeypatch.setattr(engine, "_CLARIFY_WAIT_SECONDS", 5.0)

    result = run(engine._ask_clarifying_question("which PC did you mean?"))
    assert "the desk one" in result
    assert "Sam replied" in result


def test_clarifying_question_times_out_and_surfaces_the_assumption(monkeypatch):
    async def fake_notifier(text):
        return True  # sent successfully, but nobody replies

    monkeypatch.setattr(engine, "_clarify_notifier", fake_notifier)
    monkeypatch.setattr(engine, "_CLARIFY_WAIT_SECONDS", 0.05)

    result = run(engine._ask_clarifying_question("which PC did you mean?"))
    assert "proceed with your best assumption" in result
    assert "No reply within" in result


def test_clarifying_question_capped_at_one_per_turn(monkeypatch):
    calls = {"n": 0}

    async def fake_notifier(text):
        calls["n"] += 1
        return True

    monkeypatch.setattr(engine, "_clarify_notifier", fake_notifier)
    monkeypatch.setattr(engine, "_CLARIFY_WAIT_SECONDS", 0.05)

    first = run(engine._ask_clarifying_question("first question"))
    second = run(engine._ask_clarifying_question("second question"))

    assert "No reply within" in first
    assert second.startswith("Error:")
    assert "already asked" in second
    # The DM was only ever sent once -- the cap short-circuits before notifying.
    assert calls["n"] == 1


def test_clarifying_question_degrades_when_dm_fails_rather_than_raising(monkeypatch):
    async def failing_notifier(text):
        raise RuntimeError("discord is down")

    monkeypatch.setattr(engine, "_clarify_notifier", failing_notifier)
    monkeypatch.setattr(engine, "_CLARIFY_WAIT_SECONDS", 5.0)

    result = run(engine._ask_clarifying_question("does it matter?"))  # must not raise
    assert "proceed with your best assumption" in result


def test_clarifying_question_degrades_when_notifier_returns_false(monkeypatch):
    async def declining_notifier(text):
        return False

    monkeypatch.setattr(engine, "_clarify_notifier", declining_notifier)
    monkeypatch.setattr(engine, "_CLARIFY_WAIT_SECONDS", 5.0)

    result = run(engine._ask_clarifying_question("does it matter?"))
    assert "proceed with your best assumption" in result


def test_clarifying_question_degrades_when_no_notifier_configured(monkeypatch):
    monkeypatch.setattr(engine, "_clarify_notifier", None)

    result = run(engine._ask_clarifying_question("does it matter?"))
    assert "proceed with your best assumption" in result


def test_clarifying_question_unreachable_from_autonomous_paths():
    """Research, chronos night tasks, and run_in_background all share
    background._ALLOWED_TOOLS -- consolidation.py/research.py/rumination.py
    are single-prompt completions with no tools param at all, so they're
    excluded by construction and don't need a runtime check."""
    assert "ask_clarifying_question" not in background._ALLOWED_TOOLS
    names = {t["name"] for t in background._bg_tools()}
    assert "ask_clarifying_question" not in names


def test_clarifying_question_is_registered_and_dispatched():
    from backend.tools.registry import TOOLS
    assert "ask_clarifying_question" in engine.TOOL_DISPATCH
    assert "ask_clarifying_question" in {t["name"] for t in TOOLS}
