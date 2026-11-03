"""Tests for _run_stream_openai's provider-agnostic reasoning/usage handling.

_run_stream_openai drives both the Kimi/Moonshot and the OpenRouter live-chat
path through one function. It pins:
  - which delta field carries streamed reasoning tokens (Moonshot:
    reasoning_content; OpenRouter: reasoning, per its docs)
  - that OpenRouter's authoritative per-call usage.cost reaches
    usage.record() when present
No real network call is made -- a fake async-iterable stream and a fake
client stand in for the OpenAI SDK.

F4 (2026-09-08): _run_stream_openai used to resolve its own extra_body
reasoning toggle from MODEL_MODE and model identity -- a SECOND derivation
of "which tier is this call", alongside _chat_compat's own
`use_thinking = (model == provider.thinking)`. They agreed only because
both reduced to model identity, a coincidence a later per-call effort/model
escalation feature would have broken. That derivation now lives in ONE
place, `_reasoning_config(provider, use_thinking)`, and is passed into
_run_stream_openai explicitly as the `extra_body` parameter --
_run_stream_openai's own job shrank to "send whatever extra_body you were
given, or nothing at all if None." The "extra_body resolved by model
identity" tests that used to live in this file moved to
test_engine_reasoning_config.py, which tests _reasoning_config directly
and also confirms end-to-end (via a real _chat_compat call with the client
mocked) that its output is what actually reaches the request.
"""

import asyncio
import types

import pytest

from backend.brain import engine
from backend.brain import usage as usage_mod


def run(coro):
    return asyncio.run(coro)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeCompletions:
    def __init__(self, stream):
        self._stream = stream
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._stream


class _FakeClient:
    def __init__(self, stream):
        completions = _FakeCompletions(stream)
        self.completions = completions
        self.chat = types.SimpleNamespace(completions=completions)


def _delta(content=None, reasoning=None, reasoning_content=None, tool_calls=None):
    return types.SimpleNamespace(
        content=content, reasoning=reasoning, reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )


def _chunk(delta=None, usage=None):
    choices = [types.SimpleNamespace(delta=delta)] if delta is not None else []
    return types.SimpleNamespace(choices=choices, usage=usage)


async def _noop_send(msg):
    pass


# ---------------------------------------------------------------------------
# Reasoning field: both providers' shapes stream as {"type": "thinking"}
# ---------------------------------------------------------------------------

def test_openrouter_reasoning_field_streams_as_thinking():
    chunks = [_chunk(_delta(reasoning="thinking about it")), _chunk(_delta(content="hi"))]
    client = _FakeClient(_FakeStream(chunks))
    sent = []

    async def capture(msg):
        sent.append(msg)

    text, reasoning, tool_calls = run(engine._run_stream_openai(
        client, "some-model", [], capture, None,
    ))
    assert reasoning == "thinking about it"
    assert text == "hi"
    thinking_msgs = [m for m in sent if m["type"] == "thinking"]
    assert thinking_msgs and thinking_msgs[0]["delta"] == "thinking about it"


def test_kimi_reasoning_content_field_streams_as_thinking():
    chunks = [_chunk(_delta(reasoning_content="moonshot reasoning")), _chunk(_delta(content="hi"))]
    client = _FakeClient(_FakeStream(chunks))
    sent = []

    async def capture(msg):
        sent.append(msg)

    text, reasoning, tool_calls = run(engine._run_stream_openai(
        client, "some-model", [], capture, None,
    ))
    assert reasoning == "moonshot reasoning"
    thinking_msgs = [m for m in sent if m["type"] == "thinking"]
    assert thinking_msgs and thinking_msgs[0]["delta"] == "moonshot reasoning"


def test_reasoning_content_preferred_when_both_present():
    chunks = [_chunk(_delta(reasoning="fallback", reasoning_content="preferred"))]
    client = _FakeClient(_FakeStream(chunks))
    _, reasoning, _ = run(engine._run_stream_openai(client, "m", [], _noop_send, None))
    assert reasoning == "preferred"


# ---------------------------------------------------------------------------
# extra_body -- F4 (2026-09-08): _run_stream_openai just forwards whatever
# it's handed. The tier-resolution tests live in
# test_engine_reasoning_config.py now (see module docstring above).
# ---------------------------------------------------------------------------

def test_extra_body_forwarded_verbatim_when_given():
    client = _FakeClient(_FakeStream([]))
    body = {"reasoning": {"effort": "high"}}
    run(engine._run_stream_openai(client, "m", [], _noop_send, None, extra_body=body))
    assert client.completions.calls[0]["extra_body"] is body


def test_extra_body_key_omitted_entirely_when_none():
    """Not even sent as extra_body=None -- Kimi's live path has never sent
    this key at all, and some SDKs/servers treat an explicit None
    differently from an absent key."""
    client = _FakeClient(_FakeStream([]))
    run(engine._run_stream_openai(client, "m", [], _noop_send, None))
    assert "extra_body" not in client.completions.calls[0]


# ---------------------------------------------------------------------------
# usage.cost -- OpenRouter's authoritative per-call cost reaches usage.record
# ---------------------------------------------------------------------------

def test_usage_cost_reaches_usage_record(monkeypatch):
    recorded = []
    monkeypatch.setattr(usage_mod, "record", lambda *a, **kw: recorded.append((a, kw)))

    fake_usage = types.SimpleNamespace(
        prompt_tokens=100, completion_tokens=50, cost=0.0042,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=10),
    )
    chunks = [_chunk(usage=fake_usage)]
    client = _FakeClient(_FakeStream(chunks))
    run(engine._run_stream_openai(client, "some-model", [], _noop_send, None))

    assert recorded, "usage.record was never called"
    _, kwargs = recorded[0]
    assert kwargs["cost"] == pytest.approx(0.0042)


def test_usage_without_cost_field_passes_none(monkeypatch):
    recorded = []
    monkeypatch.setattr(usage_mod, "record", lambda *a, **kw: recorded.append((a, kw)))

    fake_usage = types.SimpleNamespace(
        prompt_tokens=100, completion_tokens=50,
        prompt_tokens_details=None,
    )
    chunks = [_chunk(usage=fake_usage)]
    client = _FakeClient(_FakeStream(chunks))
    run(engine._run_stream_openai(client, "some-model", [], _noop_send, None))

    _, kwargs = recorded[0]
    assert kwargs["cost"] is None
