"""H1: real output headroom, and limits that end a turn with an answer.

Drives the real `_chat_compat` loop with only the streaming call faked, and inspects what
each call actually asked for: its tools, its effort, its `max_tokens` and its messages.
"""
import asyncio
import types

import pytest

from backend.brain import engine, escalation, router
from backend.brain.router import OPENROUTER
from backend.storage import llm_calls


def run(coro):
    return asyncio.run(coro)


async def _noop_send(msg):
    pass


async def _ok_exec_tool(tc, send):
    return tc, "result", True


def _openrouter(monkeypatch):
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    monkeypatch.setattr(engine, "_exec_tool", _ok_exec_tool)


def _script(monkeypatch, replies, same_args=False):
    """replies: one (text, tool_name_or_None, output_tokens, finish) per call. same_args:
    every call repeats identical arguments (the evidence that reaches max effort)."""
    seen = []

    async def fake(client, model, messages, send, sentence_queue, temperature=None,
                   extra_body=None, tools=None, *, purpose="chat", iteration=None,
                   plan_active=False, max_tokens=None):
        i = len(seen)
        seen.append(types.SimpleNamespace(model=model, messages=messages, body=extra_body,
                                          tools=tools, purpose=purpose,
                                          max_tokens=max_tokens))
        text, tool, out, finish = replies[min(i, len(replies) - 1)]
        args = {"q": "same"} if same_args else {"q": str(i)}
        calls = [{"id": str(i), "name": tool, "input": args}] if tool else []
        return engine._StreamResult((text, "", calls), finish, out)

    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    return seen


def _turn(**kw):
    return run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "compile it"}],
        _noop_send, None, user_text="", channel="local", use_thinking=True, **kw))


def _effort(body):
    return (body or {}).get("reasoning", {}).get("effort")


# --- output limit by effort ---------------------------------------------------------------

def test_the_limit_follows_the_effort_actually_sent():
    body = lambda b: router.with_provider_prefs(b, OPENROUTER)
    assert engine._output_limit(body(OPENROUTER.no_thinking_body)) == 8000
    assert engine._output_limit(body({"reasoning": {"effort": "low"}})) == 32000
    assert engine._output_limit(body(OPENROUTER.thinking_body)) == 64000
    assert engine._output_limit(escalation.MAX_EFFORT_BODY) == 128000
    assert engine._output_limit(None) == 16000          # Kimi's live path: unchanged


class _Stream:
    def __init__(self, chunks):
        self._c = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._c:
            raise StopAsyncIteration
        return self._c.pop(0)


class _Client:
    def __init__(self):
        self.kwargs = []
        self.chat = types.SimpleNamespace(completions=self)

    async def create(self, **kw):
        self.kwargs.append(kw)
        delta = types.SimpleNamespace(content="ok", reasoning=None, reasoning_content=None,
                                      tool_calls=None)
        return _Stream([types.SimpleNamespace(
            choices=[types.SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)])


def test_the_request_carries_the_limit_and_omits_an_empty_tool_list():
    client = _Client()
    body = router.with_provider_prefs(OPENROUTER.thinking_body, OPENROUTER)
    run(engine._run_stream_openai(client, "m", [], _noop_send, None, extra_body=body))
    run(engine._run_stream_openai(client, "m", [], _noop_send, None, extra_body=body,
                                  tools=[], max_tokens=9000))
    assert client.kwargs[0]["max_tokens"] == 64000 and client.kwargs[0]["tools"]
    assert client.kwargs[1]["max_tokens"] == 9000 and "tools" not in client.kwargs[1]


# --- a cut-off answer is continued --------------------------------------------------------

def test_a_cut_off_answer_is_finished_by_one_continuation(monkeypatch):
    _openrouter(monkeypatch)
    seen = _script(monkeypatch, [("The conclusion is that the the", None, 64000, "length"),
                                 (" thesis holds.", None, 10, "stop")])
    assert _turn() == "The conclusion is that the the thesis holds."
    cont = seen[1]
    assert cont.purpose == "continuation" and cont.tools == []
    assert cont.model == OPENROUTER.standard and _effort(cont.body) is None
    assert cont.messages[-2] == {"role": "assistant",
                                 "content": "The conclusion is that the the"}
    assert cont.messages[-1]["content"] == engine._CONTINUE_ANSWER_NUDGE


def test_a_cut_off_thought_goes_to_recovery_not_continuation(monkeypatch):
    _openrouter(monkeypatch)
    seen = []

    async def fake(client, model, messages, send, sq, temperature=None, extra_body=None,
                   tools=None, *, purpose="chat", iteration=None, plan_active=False,
                   max_tokens=None):
        seen.append(purpose)
        if len(seen) == 1:
            return engine._StreamResult(("", "thinking...", []), "length", 64000)
        return engine._StreamResult(("Direct answer.", "", []), "stop", 20)
    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    assert _turn() == "Direct answer."
    assert seen == ["chat", llm_calls.RECOVERY]


def test_a_complete_answer_is_not_continued(monkeypatch):
    _openrouter(monkeypatch)
    seen = _script(monkeypatch, [("All done.", None, 300, "stop")])
    assert _turn() == "All done." and len(seen) == 1


# --- the per-turn output budget -----------------------------------------------------------

def test_the_turn_winds_down_at_eighty_percent_of_its_budget(monkeypatch):
    _openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_TURN_OUTPUT_BUDGET", 100_000)
    seen = _script(monkeypatch, [("", "web_browse", 45_000, "stop"),
                                 ("", "vault_read", 40_000, "stop"),
                                 ("The answer.", None, 2_000, "stop")])
    assert _turn() == "The answer."
    assert [bool(c.tools) for c in seen] == [True, True, False]
    assert [_effort(c.body) for c in seen] == ["high", "high", "low"]
    last = seen[2]
    assert last.messages[-1]["content"] == engine._SYNTHESIZE_NOW_NUDGE
    assert last.max_tokens == 15_000                        # what is left of the budget


def test_the_remaining_budget_caps_each_call(monkeypatch):
    _openrouter(monkeypatch)
    monkeypatch.setattr(engine, "_TURN_OUTPUT_BUDGET", 100_000)
    seen = _script(monkeypatch, [("", "web_browse", 50_000, "stop"),
                                 ("Answer.", None, 100, "stop")])
    _turn()
    assert seen[0].max_tokens == 64_000 and seen[1].max_tokens == 50_000


def test_the_last_allowed_step_is_a_synthesis_not_a_cap_message(monkeypatch):
    """H0 benchmark: the rebuilt research task hit the 12-step cap at effort high and
    ended with _LOOP_CAP_MESSAGE and no conclusion."""
    _openrouter(monkeypatch)
    replies = [("", "web_browse", 100, "stop")] * (engine._MAX_TOOL_ITERATIONS - 1)
    seen = _script(monkeypatch, replies + [("Here is what I found.", None, 500, "stop")])
    out = _turn()
    assert out == "Here is what I found." and engine._LOOP_CAP_MESSAGE not in out
    assert seen[-1].tools == [] and seen[-1].messages[-1]["content"] == engine._SYNTHESIZE_NOW_NUDGE
    assert all(c.tools for c in seen[:-1])


def test_max_effort_runs_once_per_turn(monkeypatch):
    _openrouter(monkeypatch)
    # The same call five times escalates to max at the iteration-4 boundary; later
    # iterations stay at that level but must not keep paying for max effort.
    seen = _script(monkeypatch, [("", "web_browse", 100, "stop")] * 8
                   + [("done", None, 10, "stop")], same_args=True)
    _turn()
    efforts = [_effort(c.body) for c in seen]
    assert efforts.count("max") == 1, efforts
