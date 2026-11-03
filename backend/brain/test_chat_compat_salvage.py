"""Regression test for a real production incident: Discord (and the live
chat pipeline generally, in Kimi mode) showed JARVIS's leftover pre-tool-call
narration instead of his real answer.

Root cause: `_chat_compat`'s tool loop does `full_response += text` every
iteration. When a turn made a tool call, `text` is often just narration
("Let me check that…"). If the FINAL synthesis turn then has Moonshot put
the whole real answer into `reasoning_content` with `content` left empty --
a behavior this codebase already documents and salvages for in three other
Kimi call sites (`_plan_message_text`, `consolidation._call_compat`,
`research.py`'s picker) -- nothing ever overwrites that leftover narration,
so it becomes the entire reply. Fixed by salvaging from `reasoning_content`
on the final (no-tool-call) turn specifically, when `content` came back
empty. This file exercises `_chat_compat` directly with `_run_stream_openai`
and the compat client (`_get_compat_client`) mocked out -- no real Moonshot/OpenAI
call is made.
"""

import asyncio

import pytest

from backend.brain import engine


def run(coro):
    return asyncio.run(coro)


async def _noop_send(msg):
    pass


def _scripted_stream(monkeypatch, turns):
    """turns: list of (text, reasoning, tool_calls) tuples, one per
    _run_stream_openai call, returned in order."""
    calls = {"n": 0}

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue, temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = calls["n"]
        calls["n"] += 1
        return turns[i]

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    return calls


# ---------------------------------------------------------------------------
# The actual incident: narration-then-tool-call, then an empty-content final
# turn whose real answer landed in reasoning_content.
# ---------------------------------------------------------------------------

def test_no_salvage_needed_when_final_content_is_present(monkeypatch):
    """The normal case -- content already carries the real answer -- must be
    completely unaffected by the salvage branch."""
    turns = [("the real streamed answer", "some reasoning I shouldn't use", [])]
    _scripted_stream(monkeypatch, turns)

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assert result == "the real streamed answer"


def test_no_salvage_on_intermediate_tool_call_turn(monkeypatch):
    """A turn WITH tool_calls and empty content is the normal, expected
    shape (the model doesn't need to narrate before calling a tool) -- must
    never be salvaged, only the final no-tool-call turn is eligible."""
    turns = [
        ("", "internal reasoning about which tool to call", [{"id": "1", "name": "web_browse", "input": {}}]),
        ("final real answer", "", []),
    ]
    _scripted_stream(monkeypatch, turns)

    async def fake_exec_tool(tc, send):
        return tc, "tool result", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assert result == "final real answer"
    assert "internal reasoning" not in result


def test_both_empty_stays_empty_not_an_error(monkeypatch):
    """If content AND reasoning are both genuinely empty on the final turn,
    there is nothing to salvage -- must degrade to an empty string, not raise."""
    turns = [("", "", [])]
    _scripted_stream(monkeypatch, turns)

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assert result == ""


def test_openrouter_mode_echoes_reasoning_under_reasoning_content_key(monkeypatch):
    """In openrouter mode the tool-call assistant echo must use "reasoning_content" --
    the same key Moonshot uses, for an unrelated reason: DeepSeek V4's own API contract
    (not OpenRouter's generic third-party-model docs) 400s a thinking-mode tool-call turn
    without it ("The reasoning_content in the thinking mode must be passed back to the
    API"), confirmed via DeepSeek's own error text reproduced across multiple independent
    integrations. provider().reasoning_echo_key picks the key; _chat_compat's
    assistant-message construction must actually use it."""
    from backend.brain import router as router_mod

    turns = [
        ("", "deciding which tool to call", [{"id": "1", "name": "web_browse", "input": {}}]),
        ("final answer", "", []),
    ]
    _scripted_stream(monkeypatch, turns)

    captured_messages = []

    async def fake_exec_tool(tc, send):
        return tc, "tool result", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue, temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured_messages.append(messages)
        i = len(captured_messages) - 1
        return turns[i]

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    result = run(engine._chat_compat(
        engine._provider().standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assert result == "final answer"
    # The second call's message list must contain the assistant turn built
    # from the first, carrying reasoning under "reasoning_content".
    second_call_messages = captured_messages[1]
    assistant_msgs = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert assistant_msgs, "no assistant message was appended after the tool-call turn"
    assert assistant_msgs[-1].get("reasoning_content") == "deciding which tool to call"


def test_openrouter_thinking_tier_echoes_empty_reasoning_rather_than_omitting_it(monkeypatch):
    """DeepSeek V4's 400 fires on the field being ABSENT, not just on the wrong key (one
    live report traced it to an undefined reasoning_content on a tool-call turn) --
    so when the thinking tier happens to stream no reasoning text for a tool-call turn,
    the echo must still be "" rather than omitted entirely. Only applies when this turn's
    model is the provider's thinking tier; the standard tier never enables thinking, so
    an empty/no reasoning there is normal and must NOT get a spurious empty key."""
    from backend.brain import router as router_mod

    turns = [
        ("", "", [{"id": "1", "name": "web_browse", "input": {}}]),  # no reasoning streamed
        ("final answer", "", []),
    ]
    _scripted_stream(monkeypatch, turns)

    async def fake_exec_tool(tc, send):
        return tc, "tool result", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    captured_messages = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue, temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured_messages.append(messages)
        return turns[len(captured_messages) - 1]

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    result = run(engine._chat_compat(
        router_mod.OPENROUTER_THINKING, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", use_thinking=True,
    ))

    assert result == "final answer"
    assistant_msgs = [m for m in captured_messages[1] if m.get("role") == "assistant"]
    assert assistant_msgs[-1].get("reasoning_content") == ""


def test_openrouter_standard_tier_never_gets_spurious_empty_reasoning_key(monkeypatch):
    """The empty-string fallback is gated on the THINKING tier specifically -- a
    standard-tier (Flash) tool-call turn with no reasoning is the ordinary case and
    must not grow a reasoning_content key at all."""
    from backend.brain import router as router_mod

    turns = [
        ("", "", [{"id": "1", "name": "web_browse", "input": {}}]),
        ("final answer", "", []),
    ]
    _scripted_stream(monkeypatch, turns)

    async def fake_exec_tool(tc, send):
        return tc, "tool result", True

    monkeypatch.setattr(engine, "_exec_tool", fake_exec_tool)

    captured_messages = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue, temperature=None, tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured_messages.append(messages)
        return turns[len(captured_messages) - 1]

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    run(engine._chat_compat(
        router_mod.OPENROUTER_STANDARD, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assistant_msgs = [m for m in captured_messages[1] if m.get("role") == "assistant"]
    assert "reasoning_content" not in assistant_msgs[-1]


def test_standard_tier_leaked_reasoning_is_not_read_as_the_answer(monkeypatch):
    """The exact live bug: OpenRouter's standard (non-thinking) tier streams
    real reasoning with empty content despite thinking being disabled. The
    leaked reasoning must never reach the response/TTS channel verbatim."""
    from backend.brain import router as router_mod

    turns = [("", "internal chain-of-thought that leaked through", [])]
    _scripted_stream(monkeypatch, turns)

    sent = []

    async def capture_send(msg):
        sent.append(msg)

    queue = asyncio.Queue()
    result = run(engine._chat_compat(
        router_mod.OPENROUTER_STANDARD, "system", [{"role": "user", "content": "hi"}],
        capture_send, queue, user_text="",
    ))

    assert "internal chain-of-thought that leaked through" not in result
    response_msgs = [m for m in sent if m.get("type") == "response"]
    assert not any("internal chain-of-thought" in m["delta"] for m in response_msgs)
    assert queue.get_nowait() != "internal chain-of-thought that leaked through"


def test_standard_tier_leaked_reasoning_gets_a_plain_fallback_instead(monkeypatch):
    from backend.brain import router as router_mod

    turns = [("", "internal chain-of-thought that leaked through", [])]
    _scripted_stream(monkeypatch, turns)

    result = run(engine._chat_compat(
        router_mod.OPENROUTER_STANDARD, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assert result == engine._REASONING_LEAK_FALLBACK


def test_openrouter_thinking_tier_never_sends_its_reasoning(monkeypatch):
    """Live (2026-09-28): a long Discord task made the thinking tier reason until the
    output budget ran out -- empty content, no tool call -- and this salvage sent the raw
    reasoning to Sam. This test used to assert exactly that behaviour. DeepSeek has no
    answer-in-reasoning quirk, so the reasoning is never the reply: one direct call on the
    standard tier asks for the answer instead."""
    from backend.brain import router as router_mod
    seen = []

    async def fake(client, model, messages, send, sentence_queue, temperature=None,
                   tools=None, extra_body=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        seen.append((model, extra_body, messages[-1]["content"]))
        if len(seen) == 1:
            return engine._StreamResult(("", "Hmm wait, let me re-read the injected history. "
                                             "Let me just call X and Y", []), "length")
        return ("The real conclusion.", "", [])
    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    result = run(engine._chat_compat(
        router_mod.OPENROUTER_THINKING, "system", [{"role": "user", "content": "compile it"}],
        _noop_send, None, user_text="", use_thinking=True,
    ))
    assert result == "The real conclusion." and "re-read" not in result
    model, body, last = seen[1]
    assert model == router_mod.OPENROUTER_STANDARD
    assert body["reasoning"] == router_mod.OPENROUTER_NO_THINKING_BODY["reasoning"]
    assert last == engine._DIRECT_ANSWER_NUDGE


def test_when_the_recovery_also_fails_a_plain_line_is_sent_not_the_reasoning(monkeypatch):
    from backend.brain import router as router_mod
    turns = [engine._StreamResult(("", "internal scratch work", []), "length"),
             ("", "more scratch", [])]
    _scripted_stream(monkeypatch, turns)
    result = run(engine._chat_compat(
        router_mod.OPENROUTER_THINKING, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", use_thinking=True,
    ))
    assert result == engine._NO_ANSWER_FALLBACK and "scratch" not in result


def test_the_stream_result_still_unpacks_as_three_and_carries_finish_reason():
    r = engine._StreamResult(("t", "r", []), "length")
    text, reasoning, calls = r
    assert (text, reasoning, calls, r.finish_reason) == ("t", "r", [], "length")


def test_standard_tier_with_no_reasoning_at_all_stays_empty_not_fallback(monkeypatch):
    """A standard-tier turn with genuinely empty content AND empty reasoning
    (the ordinary, expected shape when nothing leaked) must not trigger the
    fallback message -- that's reserved for an actual leak."""
    from backend.brain import router as router_mod

    turns = [("", "", [])]
    _scripted_stream(monkeypatch, turns)

    result = run(engine._chat_compat(
        router_mod.OPENROUTER_STANDARD, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="",
    ))

    assert result == ""
