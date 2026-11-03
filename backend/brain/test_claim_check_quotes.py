"""20 October, 16:45: asked to correct his weather write-up, JARVIS did all three steps and
answered, quoting his own earlier false claims to say they had no tool call behind them:
'the two sentences before that ("I've now closed it properly", "I've marked it closed") had
no tool call'. The claim check read the quotes as new claims. The correction pass then re-ran
a full tool loop (12 steps, thinking tier) that could not see what this turn's tools had
returned, so it re-verified everything and moved his finished answer into the thinking view,
until Sam pressed Stop."""
import asyncio

import pytest

from backend.brain import engine

ANSWER = ('the research-state paragraph reads as you gave it: the subject was not closed until '
          '10:57, and the two sentences before that ("I\'ve now closed it properly", "I\'ve '
          'marked it closed") had no tool call behind them.')


@pytest.mark.parametrize("reply", [
    ANSWER,
    "Your words were “the email is on its way”, which is what I wrongly said.",
    'He wrote "two standing rules saved" in the old note.',
    'The memory said "I checked, and nothing is in the mailbox".',
])
def test_quoted_text_is_not_a_claim(reply):
    assert not engine._unbacked_claims(reply, set(), 0)


def test_a_claim_outside_the_quotes_still_counts():
    reply = 'You asked for the "weather" note to go. Done: I\'ve now closed it properly.'
    assert engine._unbacked_claims(reply, set(), 0)


def _correction_pass(monkeypatch, done):
    seen = {}

    async def rerun(messages):
        seen["frame"] = messages[-1]["content"]
        return "Fixed reply."
    monkeypatch.setattr(engine, "_turn_tools_ok", set(done))
    monkeypatch.setattr(engine, "_turn_tool_calls", len(done))
    out = asyncio.run(engine._enforce_backed_claims("I've now closed it properly.",
                                                    [{"role": "user", "content": "x"}],
                                                    rerun, "mobile"))
    return out, seen["frame"]


def test_the_correction_pass_is_told_what_already_succeeded(monkeypatch):
    out, frame = _correction_pass(monkeypatch, {"vault_write", "correct_memory"})
    assert out == "Fixed reply."
    assert "vault_write" in frame and "correct_memory" in frame
    assert "do not repeat or re-check" in frame and "keep the rest" in frame.lower()


def test_the_correction_pass_is_a_short_one(monkeypatch):
    """chat() hands _enforce_backed_claims a rerun bounded to a few steps."""
    import inspect
    src = inspect.getsource(engine.chat)
    assert "max_steps=_CORRECTION_STEPS" in src
    assert engine._CORRECTION_STEPS <= 3


def test_the_step_bound_is_honoured(monkeypatch):
    from backend.brain import router
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    calls = []

    async def fake(client, model, messages, send, sentence_queue, temperature=None, tools=None,
                   extra_body=None, *, purpose="chat", iteration=None, plan_active=False,
                   max_tokens=None):
        calls.append(tools)
        if tools:
            return ("", "", [{"id": str(len(calls)), "name": "vault_list", "input": {}}])
        return ("Done.", "", [])
    monkeypatch.setattr(engine, "_run_stream_openai", fake)

    async def ok(tc, send):
        return tc, "listing", True
    monkeypatch.setattr(engine, "_exec_tool", ok)

    async def noop(m):
        pass
    out = asyncio.run(engine._chat_compat(router.OPENROUTER_STANDARD, "s",
                                          [{"role": "user", "content": "x"}], noop, None,
                                          use_thinking=False, max_steps=3))
    assert out == "Done." and len(calls) == 3 and calls[-1] == []


def test_an_ordinary_turn_keeps_all_its_steps():
    """Only the correction pass is short; a normal turn still gets the full tool loop."""
    import inspect
    assert engine._MAX_TOOL_ITERATIONS == 12
    default = inspect.signature(engine._chat_compat).parameters["max_steps"].default
    assert default == engine._MAX_TOOL_ITERATIONS
    assert inspect.getsource(engine.chat).count("max_steps=") == 1
