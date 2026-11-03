"""Integration tests for Step 3 (2026-09-09): adaptive effort and mid-turn
model escalation, driving the REAL `_chat_compat` loop (only
`_run_stream_openai`, `_get_compat_client`, `_exec_tool`, and — where a plan
is involved — `_plan_task`/`_replan` are faked) and inspecting what each
successive request actually received. Same discipline as
test_engine_steering.py: proving the escalation reaches the real request,
not just that escalation.py's pure functions return the right thing in
isolation (that part is test_escalation.py).
"""

import asyncio

import pytest

from backend.brain import engine
from backend.brain import escalation
from backend.brain.router import OPENROUTER


def run(coro):
    return asyncio.run(coro)


async def _noop_send(msg):
    pass


@pytest.fixture(autouse=True)
def _reset_turn_state():
    engine._reset_turn_stats()
    yield
    engine._reset_turn_stats()


def _use_openrouter(monkeypatch):
    monkeypatch.setattr(engine, "_provider", lambda: OPENROUTER)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())


async def _ok_exec_tool(tc, send):
    return tc, "tool result", True


# ---------------------------------------------------------------------------
# Model escalation: standard (Flash) -> thinking_normal (Pro) when stuck.
# Below the iteration floor (_MIN_ITERATION=4) a repeated tool never
# escalates; past it, five identical calls in a row must promote the model
# actually sent on the sixth.
# ---------------------------------------------------------------------------

def test_stuck_flash_turn_escalates_to_pro_past_the_iteration_floor(monkeypatch):
    _use_openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        calls.append({"model": model, "extra_body": extra_body})
        if len(calls) <= 6:
            return "", "", [{"id": str(len(calls)), "name": "web_browse", "input": {"query": "same thing"}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    result = run(engine._chat_compat(
        OPENROUTER.standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local",
    ))

    assert result == "done"
    # Calls 1-5 (iterations 0-4) are still on Flash -- escalation only
    # decides at the boundary AFTER iteration 4 runs (index >= _MIN_ITERATION).
    for c in calls[:5]:
        assert c["model"] == OPENROUTER.standard
    # Call 6 onward must be on the promoted model.
    assert calls[5]["model"] == OPENROUTER.thinking


def test_flash_turn_that_finishes_quickly_never_escalates(monkeypatch):
    _use_openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        calls.append(model)
        if len(calls) <= 2:
            return "", "", [{"id": str(len(calls)), "name": "web_browse", "input": {"query": "x"}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    run(engine._chat_compat(
        OPENROUTER.standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local",
    ))

    assert all(m == OPENROUTER.standard for m in calls)


# ---------------------------------------------------------------------------
# Effort escalation: thinking_normal -> thinking_max when a turn that was
# already correctly routed to Pro gets stuck (no model to promote TO --
# the lever here is reasoning.effort, not the model string).
# ---------------------------------------------------------------------------

def test_stuck_pro_turn_escalates_effort_to_max(monkeypatch):
    _use_openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        calls.append({"model": model, "extra_body": extra_body})
        if len(calls) <= 6:
            return "", "", [{"id": str(len(calls)), "name": "web_browse", "input": {"query": "same"}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    # user_text="" skips the plan phase -- this test is scoped to the effort
    # lever alone, not Plan-and-Execute's own replan trigger (see the
    # replan-specific test below).
    run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local", use_thinking=True,
    ))

    for c in calls[:5]:
        assert c["model"] == OPENROUTER.thinking
        _assert_body(c["extra_body"], OPENROUTER.thinking_body)
    assert calls[5]["model"] == OPENROUTER.thinking
    _assert_body(calls[5]["extra_body"], escalation.MAX_EFFORT_BODY)


# ---------------------------------------------------------------------------
# De-escalation: once the loop stops repeating the same tool, it steps back
# down -- immediately, no hysteresis (the plan's own framing: de-escalating
# is the low-risk direction).
# ---------------------------------------------------------------------------

def test_deescalates_one_step_as_soon_as_progress_resumes(monkeypatch):
    _use_openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    calls = []
    tool_sequence = (
        ["web_browse"] * 5      # calls 1-5 (iterations 0-4): stuck
        + ["code_graph"]        # call 6 (iteration 5): a genuinely different tool
        + [None]                # call 7 (iteration 6): finish (no tool calls)
    )

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = len(calls)
        calls.append(model)
        name = tool_sequence[i]
        if name is None:
            return "done", "", []
        return "", "", [{"id": str(i), "name": name, "input": {"query": "x"}}]

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    run(engine._chat_compat(
        OPENROUTER.standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local",
    ))

    # Escalation only decides at the boundary AFTER iteration 4 runs, so
    # calls 1-5 (indices 0-4, the stuck run) are still all on Flash.
    for c in calls[:5]:
        assert c == OPENROUTER.standard
    # Call 6 (index 5, the differing "code_graph" call) runs under the
    # escalation decided at the end of iteration 4 -- promoted to Pro.
    assert calls[5] == OPENROUTER.thinking
    # That same call's result (no longer stuck) triggers de-escalation at
    # ITS boundary -- call 7 (index 6, the finishing call) is back on Flash,
    # one step down, exactly as far as one non-repeating call earns back.
    assert calls[6] == OPENROUTER.standard


# ---------------------------------------------------------------------------
# Kimi: documented no-op, even when stuck.
# ---------------------------------------------------------------------------

def test_replan_escalates_immediately_even_below_the_iteration_floor(monkeypatch):
    _use_openrouter(monkeypatch)

    plan = [
        {"id": 1, "label": "first step", "tool": "web_browse", "parallel_group": None},
        {"id": 2, "label": "second step", "tool": "code_graph", "parallel_group": None},
    ]

    async def fake_plan_task(client, user_text, tools):
        return plan

    async def fake_replan(client, user_text, completed_steps, errors, remaining):
        return remaining   # revise-in-place is irrelevant to this test

    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)
    monkeypatch.setattr(engine, "_replan", fake_replan)

    # 2026-10-09: the replan has to be one the model asked for (request_replan). A replan
    # forced by a failed tool no longer reaches max effort (test_escalation_evidence.py).
    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        calls.append({"model": model, "extra_body": extra_body})
        if len(calls) == 1:
            return "", "", [{"id": "1", "name": "request_replan",
                             "input": {"reason": "the first step cannot work"}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="do the thing", channel="local", use_thinking=True,
    ))

    # Iteration 0 (the very first call) is where the replan was requested
    # -- well below _MIN_ITERATION=4. The SECOND call must
    # already be at the escalated effort, proving the floor was bypassed.
    assert calls[0]["model"] == OPENROUTER.thinking
    _assert_body(calls[0]["extra_body"], OPENROUTER.thinking_body)
    _assert_body(calls[1]["extra_body"], escalation.MAX_EFFORT_BODY)


# ---------------------------------------------------------------------------
# Well-formed message list across a mid-turn tier switch -- reasoning_content
# echo correctness is exactly the F5 concern, now exercised by a REAL
# escalation rather than the 404 fallback.
# ---------------------------------------------------------------------------

def test_message_list_stays_well_formed_across_an_escalation(monkeypatch):
    _use_openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    captured = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured.append(list(messages))
        i = len(captured)
        if i <= 6:
            return "reasoning text", "some reasoning", [
                {"id": str(i), "name": "web_browse", "input": {"query": "same"}}
            ]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    run(engine._chat_compat(
        OPENROUTER.standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local",
    ))

    # Call 6's message list (built from call 5's response) must still carry
    # a well-formed assistant/tool_calls message with the reasoning echo --
    # OpenRouter's own reasoning_echo_required contract -- even though the
    # PROVIDER TIER changes between when that message was produced (still
    # Flash, thinking off) and when it's sent again (now Pro, thinking on).
    sixth_call_messages = captured[5]
    assistant_msgs = [m for m in sixth_call_messages if m.get("role") == "assistant"]
    assert assistant_msgs, "no assistant message found in the request history"
    last_assistant = assistant_msgs[-1]
    assert last_assistant["tool_calls"][0]["function"]["name"] == "web_browse"


def _assert_body(sent, expected_reasoning_body):
    """Reasoning half must match exactly; since 2026-09-21 the outgoing body also
    carries the OpenRouter routing preference merged in by router.with_provider_prefs.
    Asserted separately so an escalation bug can't hide behind a whole-dict compare."""
    assert sent["reasoning"] == expected_reasoning_body["reasoning"]
    assert sent["provider"] == OPENROUTER.provider_prefs


# ---------------------------------------------------------------------------
# The floor: de-escalation only undoes an escalation (H0 benchmark, 2026-09-28).
# ---------------------------------------------------------------------------

def test_a_thinking_turn_keeps_thinking_through_varied_tool_calls(monkeypatch):
    """Found by the H0 benchmark: a turn routed to the thinking tier dropped to reasoning
    off after its third varied tool call, and ran the rest -- synthesis included --
    without thinking (9 of 12 calls at effort "none" on a rebuilt research turn)."""
    _use_openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)
    bodies = []
    sequence = ["web_browse", "vault_read", "memory_search", "fetch_page", None]

    async def fake(client, model, messages, send, sentence_queue, temperature=None,
                   extra_body=None, tools=None, *, purpose="chat", iteration=None,
                   plan_active=False, max_tokens=None):
        i = len(bodies)
        bodies.append(extra_body)
        name = sequence[i]
        if name is None:
            return "the conclusion", "", []
        return "", "", [{"id": str(i), "name": name, "input": {"query": "x"}}]

    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    out = run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local", use_thinking=True,
    ))
    assert out == "the conclusion"
    assert [b["reasoning"] for b in bodies] == [OPENROUTER.thinking_body["reasoning"]] * 5


def test_step_never_deescalates_below_the_floor():
    progressing = ["web_browse(a)", "vault_read(b)", "memory_search(c)"]
    assert escalation.step(OPENROUTER, "thinking_normal", 10, progressing, False,
                           floor="thinking_normal") == "thinking_normal"
    assert escalation.step(OPENROUTER, "thinking_max", 10, progressing, False,
                           floor="thinking_normal") == "thinking_normal"
    assert escalation.step(OPENROUTER, "standard", 10, progressing, False,
                           floor="thinking_normal") == "standard"
