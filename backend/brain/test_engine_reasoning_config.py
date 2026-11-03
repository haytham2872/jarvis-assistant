"""Tests for F4 (2026-09-08): _reasoning_config(provider, use_thinking) is the
single derivation of "which tier is this call" for the OpenAI-compatible live
chat path.

Before this, the same fact was computed twice by two different rules that
happened to agree only because both reduced to model identity:
  - _chat_compat:          use_thinking = (model == provider.thinking)
  - _run_stream_openai:  extra_body = thinking_body if (model ==
                          OPENROUTER_THINKING) else no_thinking_body

_reasoning_config replaces the second derivation: it resolves BOTH the
temperature (F2) and the extra_body reasoning toggle from (provider,
use_thinking) in one place, and _chat_compat passes the result down to
_run_stream_openai explicitly instead of the latter re-deriving anything.

This file has two halves:
  1. Pure unit tests of _reasoning_config against the real OPENROUTER
     descriptor.
  2. An end-to-end test driving the real _chat_compat with only the OpenAI
     client mocked out, confirming the resolved config is what actually
     reaches the outgoing request -- not just what a unit test of
     _reasoning_config in isolation would suggest.
"""

import asyncio
import types

import pytest

from backend.brain import engine
from backend.brain.router import OPENROUTER


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. _reasoning_config -- pure unit tests
# ---------------------------------------------------------------------------

def test_openrouter_thinking_gets_effort_body():
    temperature, extra_body = engine._reasoning_config(OPENROUTER, use_thinking=True)
    assert temperature == OPENROUTER.thinking_temperature == 1.0
    # The reasoning half is still exactly thinking_body; since 2026-09-21 the routing
    # preference (provider.order) is merged in alongside it -- see with_provider_prefs.
    assert extra_body["reasoning"] == OPENROUTER.thinking_body["reasoning"]
    assert extra_body["provider"] == OPENROUTER.provider_prefs


def test_openrouter_not_thinking_gets_disable_body():
    temperature, extra_body = engine._reasoning_config(OPENROUTER, use_thinking=False)
    assert temperature is None
    assert extra_body["reasoning"] == OPENROUTER.no_thinking_body["reasoning"]
    assert extra_body["provider"] == OPENROUTER.provider_prefs


def test_openrouter_thinking_body_dict_is_never_mutated():
    """The two traps named in the upgrade plan: OPENROUTER_THINKING_BODY is a
    module-level constant shared by every call -- _reasoning_config must
    return the SAME object (or an equal one), never a copy that a caller
    might later mutate in place and corrupt for every other call."""
    before = dict(OPENROUTER.thinking_body)
    engine._reasoning_config(OPENROUTER, use_thinking=True)
    assert OPENROUTER.thinking_body == before


# ---------------------------------------------------------------------------
# 2. End-to-end -- the resolved config actually reaches the request
# ---------------------------------------------------------------------------

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
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream([])


class _FakeClient:
    def __init__(self):
        completions = _FakeCompletions()
        self.completions = completions
        self.chat = types.SimpleNamespace(completions=completions)


async def _noop_send(msg):
    pass


def test_chat_compat_openrouter_thinking_call_reaches_request_with_resolved_config(monkeypatch):
    """Drives the real _chat_compat (only _get_compat_client is mocked, so the
    real _run_stream_openai and the real _reasoning_config both run) and
    inspects the actual kwargs the fake client received -- proving the
    resolved temperature/extra_body genuinely reach the request, not just
    what a unit test of _reasoning_config in isolation would suggest."""
    from backend.brain import router as router_mod

    monkeypatch.setattr(engine, "_provider", lambda: OPENROUTER)

    fake_client = _FakeClient()
    monkeypatch.setattr(engine, "_get_compat_client", lambda: fake_client)

    # use_thinking is passed explicitly: since the 2026-09-21 swap both openrouter tiers
    # are the SAME model at different reasoning efforts, so the model string alone can no
    # longer identify the tier (router.route_tier() is what decides it on the live path).
    run(engine._chat_compat(
        OPENROUTER.thinking, "system prompt", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", tools=[], use_thinking=True,
    ))

    assert len(fake_client.completions.calls) == 1
    sent = fake_client.completions.calls[0]
    assert sent["temperature"] == OPENROUTER.thinking_temperature
    assert sent["extra_body"]["reasoning"] == OPENROUTER.thinking_body["reasoning"]
    assert sent["extra_body"]["provider"] == OPENROUTER.provider_prefs


def test_chat_compat_openrouter_standard_call_reaches_request_with_resolved_config(monkeypatch):
    from backend.brain import router as router_mod

    monkeypatch.setattr(engine, "_provider", lambda: OPENROUTER)

    fake_client = _FakeClient()
    monkeypatch.setattr(engine, "_get_compat_client", lambda: fake_client)

    run(engine._chat_compat(
        OPENROUTER.standard, "system prompt", [{"role": "user", "content": "hi"}],
        _noop_send, None, user_text="", tools=[], use_thinking=False,
    ))

    assert len(fake_client.completions.calls) == 1
    sent = fake_client.completions.calls[0]
    assert "temperature" not in sent
    assert sent["extra_body"]["reasoning"] == OPENROUTER.no_thinking_body["reasoning"]
    assert sent["extra_body"]["provider"] == OPENROUTER.provider_prefs
