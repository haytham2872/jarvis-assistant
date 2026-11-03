"""Tests for Step 4 (2026-09-09): the plan is authorized scope, not a
suggestion.

Covers: the "authorized scope" reframing in the system prompt, request_replan
being offered to the executor ONLY while a plan is active, a deliberate
request_replan actually revising the live plan and letting the turn continue,
the two-replan cap SHARED across both triggers (a tool error, or a deliberate
request_replan), the "plan changed" Discord notice firing once per replan
(never per step) and only for the "discord" channel, `plan_update` emitted
for both trigger types, and a genuine regression test for the parallel-group
step-pointer accounting this step's restructure touched (CLAUDE.md's own
"Traps" section calls this out as easy to desync -- no dedicated test for it
existed before this file).

Same discipline as test_engine_escalation.py: drives the REAL `_chat_compat`
loop with `_run_stream_openai`/`_plan_task`/`_replan`/`_exec_tool` faked,
inspecting what the real loop actually did -- not just what a unit test of
one helper function in isolation would suggest.
"""

import asyncio

import pytest

from backend.brain import engine
from backend.brain.router import OPENROUTER


def run(coro):
    return asyncio.run(coro)


def run_and_drain(coro):
    """Same as run(), but yields once more after the awaited coroutine
    returns -- gives a fire-and-forget asyncio.create_task() a real chance
    to run to completion before the loop closes. Needed for any test that
    inspects a side effect of _notify_plan_change_discord/_notify_plan_discord,
    both fired via create_task inside _chat_compat's loop, never awaited
    directly by it (the same asyncio.run()-tears-down-pending-tasks class of
    bug documented elsewhere in this test suite, e.g. test_threads_record.py).
    """
    async def wrapper():
        result = await coro
        await asyncio.sleep(0)
        return result
    return asyncio.run(wrapper())


async def _noop_send(msg):
    pass


@pytest.fixture(autouse=True)
def _reset_turn_state():
    engine._reset_turn_stats()
    yield
    engine._reset_turn_stats()
    engine.set_plan_notifier(None)


def _use_openrouter(monkeypatch):
    monkeypatch.setattr(engine, "_provider", lambda: OPENROUTER)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())


async def _ok_exec_tool(tc, send):
    return tc, "tool result", True


async def _failing_exec_tool(tc, send):
    return tc, "Error: boom", False


# ---------------------------------------------------------------------------
# The system-prompt reframing itself
# ---------------------------------------------------------------------------

_SAMPLE_PLAN = [
    {"id": 1, "label": "Search the web", "tool": "web_browse", "parallel_group": None},
    {"id": 2, "label": "Check email", "tool": "gmail_action", "parallel_group": "A"},
    {"id": 3, "label": "Fetch the article", "tool": "fetch_page", "parallel_group": "A"},
]


def test_format_plan_for_system_frames_it_as_authorized_scope():
    text = engine._format_plan_for_system(_SAMPLE_PLAN)
    assert "authorized scope" in text.lower()
    assert "request_replan" in text


def test_format_plan_change_for_discord_leads_with_the_reason():
    text = engine._format_plan_change_for_discord("the API rate-limited", _SAMPLE_PLAN)
    assert text.startswith("Plan changed: the API rate-limited")
    assert "Search the web" in text


# ---------------------------------------------------------------------------
# request_replan offered ONLY while a plan is active
# ---------------------------------------------------------------------------

def test_request_replan_offered_when_plan_active(monkeypatch):
    _use_openrouter(monkeypatch)

    async def fake_plan_task(client, user_text, tools):
        return _SAMPLE_PLAN
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)

    captured_tools = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured_tools.append(tools)
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="do something", channel="local", tools=[], use_thinking=True,
    ))

    names = [t["name"] for t in captured_tools[0]]
    assert "request_replan" in names


def test_request_replan_absent_without_a_plan(monkeypatch):
    _use_openrouter(monkeypatch)

    captured_tools = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        captured_tools.append(tools)
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    # user_text="" skips the plan phase entirely.
    run(engine._chat_compat(
        OPENROUTER.standard, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", channel="local", tools=[], use_thinking=True,
    ))

    names = [t.get("name") for t in captured_tools[0]]
    assert "request_replan" not in names


# ---------------------------------------------------------------------------
# A deliberate request_replan actually revises the plan and the turn continues
# ---------------------------------------------------------------------------

def test_deliberate_replan_revises_the_plan_and_continues(monkeypatch):
    _use_openrouter(monkeypatch)
    initial_plan = [
        {"id": 1, "label": "email", "tool": "gmail_action", "parallel_group": None},
        {"id": 2, "label": "finish", "tool": None, "parallel_group": None},
    ]
    revised_plan = [{"id": 2, "label": "check calendar instead", "tool": "calendar_action", "parallel_group": None}]

    async def fake_plan_task(client, user_text, tools):
        return initial_plan
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)

    replan_calls = []

    async def fake_replan(client, user_text, completed, errors, remaining):
        replan_calls.append({"errors": list(errors), "remaining": list(remaining)})
        return revised_plan
    monkeypatch.setattr(engine, "_replan", fake_replan)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        calls.append(list(messages))
        i = len(calls)
        if i == 1:
            return "", "", [{"id": "r1", "name": "request_replan",
                              "input": {"reason": "user actually wants calendar, not email"}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    sent = []

    async def capture_send(msg):
        sent.append(msg)

    result = run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        capture_send, None, user_text="email someone", channel="local", tools=[], use_thinking=True,
    ))

    assert result == "done"
    # _chat_compat never treated the turn as finished just because the model's
    # only call that iteration was request_replan, not a real plan-step action.
    assert len(calls) == 2

    assert len(replan_calls) == 1
    assert "Deliberate replan requested" in replan_calls[0]["errors"][0]
    assert "user actually wants calendar" in replan_calls[0]["errors"][0]
    # The un-executed original remaining plan (from step_pointer onward) is
    # what got handed to the replanner -- step 1 hadn't run yet either.
    assert replan_calls[0]["remaining"] == initial_plan

    plan_updates = [m for m in sent if m.get("type") == "plan_update"]
    assert len(plan_updates) == 1
    assert plan_updates[0]["steps"][0]["label"] == "check calendar instead"

    # The SECOND request's message list must carry a well-formed tool result
    # for the replan call, matching its tool_call_id.
    second_call_messages = calls[1]
    tool_msgs = [m for m in second_call_messages
                 if m.get("role") == "tool" and m.get("tool_call_id") == "r1"]
    assert len(tool_msgs) == 1
    assert "Plan revised" in tool_msgs[0]["content"]
    # And the assistant message that preceded it must still declare the
    # request_replan call itself (every tool_call_id needs a matching
    # assistant-side declaration or the API rejects the request).
    assistant_msgs = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert assistant_msgs[-1]["tool_calls"][0]["function"]["name"] == "request_replan"


# ---------------------------------------------------------------------------
# The two-replan cap, SHARED across both triggers
# ---------------------------------------------------------------------------

def test_replan_cap_shared_across_both_triggers(monkeypatch):
    _use_openrouter(monkeypatch)
    plan = [
        {"id": 1, "label": "a", "tool": "web_browse", "parallel_group": None},
        {"id": 2, "label": "b", "tool": "web_browse", "parallel_group": None},
        {"id": 3, "label": "c", "tool": "web_browse", "parallel_group": None},
    ]

    async def fake_plan_task(client, user_text, tools):
        return plan
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)

    replan_count = {"n": 0}

    async def fake_replan(client, user_text, completed, errors, remaining):
        replan_count["n"] += 1
        return remaining
    monkeypatch.setattr(engine, "_replan", fake_replan)
    monkeypatch.setattr(engine, "_exec_tool", _failing_exec_tool)

    calls = []
    replan_results = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = len(calls)
        calls.append(model)
        # Capture whatever tool result the PREVIOUS request_replan call got,
        # by reading the tail of this request's own message list.
        if messages and messages[-1].get("role") == "tool":
            replan_results.append(messages[-1]["content"])
        if i == 0:
            # Real tool call for step 1 -> fails -> error-triggered replan #1.
            return "", "", [{"id": "1", "name": "web_browse", "input": {}}]
        if i == 1:
            # Model deliberately asks to replan -> replan #2 (cap now reached).
            return "", "", [{"id": "2", "name": "request_replan", "input": {"reason": "rethink"}}]
        if i == 2:
            # Tries again -> must be refused, no third _replan call.
            return "", "", [{"id": "3", "name": "request_replan", "input": {"reason": "again"}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    result = run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="do something", channel="local", tools=[], use_thinking=True,
    ))

    assert result == "done"
    assert replan_count["n"] == 2   # capped at _MAX_REPLANS_PER_TURN, not 3
    # The third attempt's tool result is the refusal, not another "Plan revised".
    assert "already used your 2 replans" in replan_results[-1]


# ---------------------------------------------------------------------------
# "Plan changed" Discord notice: fires once per replan, never per step, and
# only for channel == "discord" -- mirroring the initial plan DM's own gating.
# ---------------------------------------------------------------------------

def test_plan_change_notice_fires_once_per_replan_on_discord(monkeypatch):
    _use_openrouter(monkeypatch)
    plan = [
        {"id": 1, "label": "a", "tool": "web_browse", "parallel_group": None},
        {"id": 2, "label": "b", "tool": "web_browse", "parallel_group": None},
    ]

    async def fake_plan_task(client, user_text, tools):
        return plan
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)

    async def fake_replan(client, user_text, completed, errors, remaining):
        return remaining
    monkeypatch.setattr(engine, "_replan", fake_replan)
    monkeypatch.setattr(engine, "_exec_tool", _failing_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = len(calls)
        calls.append(model)
        if i == 0:
            return "", "", [{"id": "1", "name": "web_browse", "input": {}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    notified = []

    async def fake_notifier(text):
        notified.append(text)
        return True

    engine.set_plan_notifier(fake_notifier)

    run_and_drain(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="do something", channel="discord", tools=[], use_thinking=True,
    ))

    plan_change_msgs = [t for t in notified if t.startswith("Plan changed:")]
    assert len(plan_change_msgs) == 1   # once for the one replan, not once per step


def test_plan_change_notice_absent_on_local_channel(monkeypatch):
    """Matches the initial plan DM's own channel gating -- a desk/local turn
    already shows the revision via plan_update in the UI's own timeline; a
    Discord DM would be redundant noise there."""
    _use_openrouter(monkeypatch)
    plan = [{"id": 1, "label": "a", "tool": "web_browse", "parallel_group": None}]

    async def fake_plan_task(client, user_text, tools):
        return plan
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)

    async def fake_replan(client, user_text, completed, errors, remaining):
        return remaining
    monkeypatch.setattr(engine, "_replan", fake_replan)
    monkeypatch.setattr(engine, "_exec_tool", _failing_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = len(calls)
        calls.append(model)
        if i == 0:
            return "", "", [{"id": "1", "name": "web_browse", "input": {}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    notified = []

    async def fake_notifier(text):
        notified.append(text)
        return True

    engine.set_plan_notifier(fake_notifier)

    run_and_drain(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="do something", channel="local", tools=[], use_thinking=True,
    ))

    assert notified == []


# ---------------------------------------------------------------------------
# plan_update emitted for both trigger types (error and deliberate) -- a
# lighter-weight check than the fuller tests above, pinned on its own since
# it's explicitly named in the plan's own test list.
# ---------------------------------------------------------------------------

def test_plan_update_emitted_on_error_triggered_replan(monkeypatch):
    _use_openrouter(monkeypatch)
    plan = [
        {"id": 1, "label": "a", "tool": "web_browse", "parallel_group": None},
        {"id": 2, "label": "b", "tool": "web_browse", "parallel_group": None},
    ]

    async def fake_plan_task(client, user_text, tools):
        return plan
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)

    async def fake_replan(client, user_text, completed, errors, remaining):
        return remaining
    monkeypatch.setattr(engine, "_replan", fake_replan)
    monkeypatch.setattr(engine, "_exec_tool", _failing_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = len(calls)
        calls.append(model)
        if i == 0:
            return "", "", [{"id": "1", "name": "web_browse", "input": {}}]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    sent = []

    async def capture_send(msg):
        sent.append(msg)

    run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        capture_send, None, user_text="do something", channel="local", tools=[], use_thinking=True,
    ))

    assert any(m.get("type") == "plan_update" for m in sent)


# ---------------------------------------------------------------------------
# Regression: parallel-group step-pointer accounting, unchanged by this
# step's restructure. CLAUDE.md's own "Traps" section names this as subtle
# and easy to desync -- no dedicated test for it existed before this file.
# ---------------------------------------------------------------------------

def test_parallel_group_consumes_every_step_in_the_group(monkeypatch):
    _use_openrouter(monkeypatch)
    plan = [
        {"id": 1, "label": "search", "tool": "web_browse", "parallel_group": None},
        {"id": 2, "label": "email", "tool": "gmail_action", "parallel_group": "A"},
        {"id": 3, "label": "fetch", "tool": "fetch_page", "parallel_group": "A"},
        {"id": 4, "label": "synthesize", "tool": None, "parallel_group": None},
    ]

    async def fake_plan_task(client, user_text, tools):
        return plan
    monkeypatch.setattr(engine, "_plan_task", fake_plan_task)
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)

    calls = []

    async def fake_run_stream_openai(client, model, messages, send, sentence_queue,
                                      temperature=None, extra_body=None, tools=None, *, purpose="chat", iteration=None, plan_active=False, max_tokens=None):
        i = len(calls)
        calls.append(model)
        if i == 0:
            return "", "", [{"id": "1", "name": "web_browse", "input": {}}]
        if i == 1:
            # Both group-A steps called together in one turn, as the plan's
            # own system-prompt instructions ask for.
            return "", "", [
                {"id": "2", "name": "gmail_action", "input": {}},
                {"id": "3", "name": "fetch_page", "input": {}},
            ]
        return "done", "", []

    monkeypatch.setattr(engine, "_run_stream_openai", fake_run_stream_openai)

    sent = []

    async def capture_send(msg):
        sent.append(msg)

    run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
        capture_send, None, user_text="do something", channel="local", tools=[], use_thinking=True,
    ))

    # After the group-A turn, the timeline must show step_done for steps 2 AND
    # 3 (the group), plus the synthetic start/done pair for the extra step
    # consumed in the same turn -- not just one step advanced.
    step_done_ns = [m["n"] for m in sent if m.get("type") == "step_done"]
    assert 2 in step_done_ns
    assert 3 in step_done_ns
    # Step 4 (the final synthesis step) must be the one prompted for next --
    # proving step_pointer landed at index 3 (0-based), not 2, after
    # consuming both group members from a single model turn.
    step_start_labels = [m["label"] for m in sent if m.get("type") == "step_start"]
    assert "synthesize" in step_start_labels
