"""H4 (decision 3): continue_in_background moves a long turn to the background only when
JARVIS calls it; the job continues with what was gathered and DMs its result."""
import asyncio
import types

import pytest

from backend.brain import background, engine, router
from backend.brain.router import OPENROUTER


def run(coro):
    return asyncio.run(coro)


async def _noop(msg):
    pass


def _openrouter(monkeypatch):
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    async def ok_exec(tc, send):
        return tc, f"RESULT OF {tc['name']}: NVDA Q2 revenue $96.2B", True
    monkeypatch.setattr(engine, "_exec_tool", ok_exec)


def _script(monkeypatch, replies):
    seen = []

    async def fake(client, model, messages, send, sq, temperature=None, extra_body=None,
                   tools=None, *, purpose="chat", iteration=None, plan_active=False,
                   max_tokens=None):
        seen.append(types.SimpleNamespace(tools=[t["name"] for t in tools or []],
                                          messages=list(messages)))
        text, calls = replies[len(seen) - 1]
        return engine._StreamResult((text, "", calls), "stop", 10)
    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    return seen


# --- the live side ------------------------------------------------------------------------

def test_a_handoff_carries_the_turn_and_ends_it_with_an_acknowledgement(monkeypatch):
    _openrouter(monkeypatch)
    started = {}

    async def start(request, brief):
        started.update(request=request, brief=brief)
        return "Handed off as background task #1. Tell Sam..."
    monkeypatch.setattr(background, "start_handoff", start)
    seen = _script(monkeypatch, [
        ("", [{"id": "a", "name": "web_browse", "input": {"task": "NVDA revenue"}}]),
        ("", [{"id": "b", "name": "continue_in_background",
               "input": {"reason": "nine more filings to read"}}]),
        ("I'll keep going on this in the background and message you.", []),
    ])
    out = run(engine._chat_compat(OPENROUTER.thinking, "s",
                                  [{"role": "user", "content": "compile the NVDA research"}],
                                  _noop, None, user_text="compile the NVDA research",
                                  use_thinking=True))
    assert out == "I'll keep going on this in the background and message you."
    assert started["request"] == "compile the NVDA research"
    assert "nine more filings to read" in started["brief"]
    assert "RESULT OF web_browse: NVDA Q2 revenue $96.2B" in started["brief"]
    assert "continue_in_background" in seen[0].tools and seen[2].tools == []
    assert seen[2].messages[-1]["content"].startswith("Handed off")


def test_a_refused_handoff_leaves_the_turn_running(monkeypatch):
    _openrouter(monkeypatch)

    async def refuse(request, brief):
        return "Error: this task needs a shell"
    monkeypatch.setattr(background, "start_handoff", refuse)
    seen = _script(monkeypatch, [
        ("", [{"id": "b", "name": "continue_in_background", "input": {"reason": "x"}}]),
        ("", [{"id": "c", "name": "web_browse", "input": {"task": "y"}}]),
        ("answer", []),
    ])
    out = run(engine._chat_compat(OPENROUTER.thinking, "s", [{"role": "user", "content": "q"}],
                                  _noop, None, user_text="q", use_thinking=True))
    assert out == "answer" and seen[1].tools and "web_browse" in seen[1].tools


def test_the_tool_list_is_the_same_on_both_tiers(monkeypatch):
    """The tools sit right after the system prompt; a list that changed with the tier
    would break the cached prefix whenever a conversation switched tiers."""
    _openrouter(monkeypatch)
    lists = []
    for thinking in (False, True):
        seen = _script(monkeypatch, [("ok", [])])
        run(engine._chat_compat(OPENROUTER.thinking, "s", [{"role": "user", "content": "q"}],
                                _noop, None, user_text="", use_thinking=thinking))
        lists.append(seen[0].tools)
    assert lists[0] == lists[1] and "continue_in_background" in lists[0]


def test_the_brief_is_bounded_and_skips_the_context_block():
    msgs = [{"role": "assistant", "tool_calls": [
        {"id": f"c{i}", "function": {"name": "web_browse", "arguments": '{"task": "q"}'}}]}
        for i in range(20)]
    msgs = [m for i, m in enumerate(msgs) for m in (m, {"role": "tool", "tool_call_id": f"c{i}",
                                                     "content": "x" * 5000})]
    brief = engine._handoff_brief("req", "why", [{"label": "Search"}, {"label": "Write"}], 1, msgs)
    assert len(brief) < engine._HANDOFF_BRIEF_CHARS + 3000
    assert "1. Search (done)" in brief and "2. Write" in brief and "omitted" in brief
    user = [{"role": "user", "content": "compile it\n\n" + engine._TURN_CONTEXT_HEADER + "\n\n[Now] ..."}]
    assert engine._request_text("", user) == "compile it"


# --- the background side ------------------------------------------------------------------

def test_a_shell_request_is_refused_before_a_job_starts(monkeypatch):
    started = []
    monkeypatch.setattr(background, "_start", lambda *a, **k: started.append(a))
    out = run(background.start_handoff("run git log and summarise", "brief"))
    assert out.startswith("Error:") and started == []


class _Completions:
    def __init__(self, replies):
        self.calls, self._replies = [], list(replies)
        self.chat = types.SimpleNamespace(completions=self)

    async def create(self, **kw):
        self.calls.append(kw)
        content, tool = self._replies.pop(0)
        calls = [types.SimpleNamespace(id="t", function=types.SimpleNamespace(
            name=tool, arguments='{"task": "q"}'))] if tool else None
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(
                content=content, tool_calls=calls, reasoning_content=None, reasoning=None))],
            usage=None, provider="Together")


def test_a_deep_job_keeps_thinking_and_its_last_step_answers(monkeypatch):
    monkeypatch.setattr(background, "_provider", lambda: OPENROUTER)
    client = _Completions([("", "web_browse")] * (background._MAX_BG_ITERATIONS - 1)
                          + [("The full answer.", None)])
    monkeypatch.setattr(engine, "_get_compat_client", lambda: client)

    async def bg_exec(name, tool_input, invalid=None):
        return "page text"
    monkeypatch.setattr(background, "_bg_exec", bg_exec)
    out = run(background._run_compat_loop("brief", deep=True))
    assert (out.status, out.text) == (background.COMPLETE, "The full answer.")
    first, last = client.calls[0], client.calls[-1]
    assert first["model"] == OPENROUTER.thinking and first["max_tokens"] == 64000
    assert first["extra_body"]["reasoning"] == OPENROUTER.thinking_body["reasoning"]
    assert "tools" in first and "tools" not in last
    assert last["messages"][-1]["content"] == background._DEEP_FINAL_NUDGE
    # the reasoning echo key must exist on tool-call turns while thinking is on
    assert first is not last and "reasoning_content" in client.calls[1]["messages"][2]


def test_an_ordinary_job_is_unchanged(monkeypatch):
    monkeypatch.setattr(background, "_provider", lambda: OPENROUTER)
    client = _Completions([("done", None)])
    monkeypatch.setattr(engine, "_get_compat_client", lambda: client)
    assert run(background._run_compat_loop("task")).text == "done"
    call = client.calls[0]
    assert call["model"] == OPENROUTER.standard and call["max_tokens"] == 4096
    assert call["extra_body"]["reasoning"] == OPENROUTER.no_thinking_body["reasoning"]


def test_a_finished_handoff_is_messaged_and_not_repeated(monkeypatch):
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(background, "_notifier", notifier)
    t = background.BgTask(id=9, description="compile the NVDA research", status="done",
                          notify=True, deep=True, result="It holds.", summary="It holds.")
    run(background._notify_result(t))
    assert sent == ['Done: "compile the NVDA research"\n\nIt holds.'] and t.dm_sent
    monkeypatch.setitem(background._tasks, 9, t)
    assert "Already sent to Sam" in background.pop_completed_notices()


def test_a_long_handoff_result_is_summarised_with_its_note(monkeypatch):
    from backend.brain import long_reply
    sent = []

    async def notifier(text):
        sent.append(text)
        return True

    async def summary(text):
        return "Short version."
    monkeypatch.setattr(background, "_notifier", notifier)
    monkeypatch.setattr(long_reply, "_summarise", summary)
    t = background.BgTask(id=3, description="d", status="done", notify=True,
                          result="x" * 9000, note_file="drafts/bg-3-d.md")
    run(background._notify_result(t))
    assert "Short version." in sent[0] and "drafts/bg-3-d.md" in sent[0]


def test_an_ordinary_job_sends_no_dm(monkeypatch):
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(background, "_notifier", notifier)
    run(background._notify_result(background.BgTask(id=1, description="d", status="done")))
    assert sent == []


def test_deep_and_notify_survive_a_restart():
    t = background.BgTask(id=4, description="d", deep=True, notify=True)
    payload = background._job_payload(t)
    assert payload["deep"] is True and payload["notify"] is True


def test_the_worker_messages_a_handed_off_result(monkeypatch):
    sent, deep_flags = [], []

    async def runner(text, deep=False):
        deep_flags.append(deep)
        return "It holds."

    async def note(t):
        t.note_file = "drafts/bg-1.md"

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(background, "_run_compat_loop", runner)
    monkeypatch.setattr(background, "_write_result_note", note)
    monkeypatch.setattr(background, "_notifier", notifier)
    t = background.BgTask(id=1, description="compile it", brief="b", deep=True, notify=True)
    run(background._worker(t))
    assert t.status == "done" and deep_flags == [True] and sent and "It holds." in sent[0]


def test_a_task_started_from_discord_messages_its_result(monkeypatch):
    from backend.brain import discord_context
    started = []

    async def start(task, brief, *, deep=False, notify=False, origin=""):
        started.append(notify)
        return types.SimpleNamespace(id=len(started)), ""
    monkeypatch.setattr(background, "_start", start)
    out_desk = run(background.run_in_background("research X"))
    token = discord_context.current.set(discord_context.TurnContext("m1"))
    try:
        out_discord = run(background.run_in_background("research X"))
    finally:
        discord_context.current.reset(token)
    assert started == [False, True]
    assert "on Discord" in out_discord and "on Discord" not in out_desk
