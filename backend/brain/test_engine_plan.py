"""Tests for the Plan-and-Execute helpers in engine.py.

Pure-function tests only — no Qdrant, no network, no LLM. Run with:
    .venv\\Scripts\\pytest backend/brain/test_engine_plan.py -v
"""

import asyncio

import pytest

from backend.brain import engine


# ---------------------------------------------------------------------------
# _extract_json_array — already covers the fence/prose-wrapping cases used by
# _plan_task/_replan; a couple of targeted regressions for the plan shape.
# ---------------------------------------------------------------------------

def test_extract_json_array_plain():
    raw = '[{"id": 1, "label": "Search", "tool": "web_browse", "parallel_group": null}]'
    steps = engine._extract_json_array(raw)
    assert steps == [{"id": 1, "label": "Search", "tool": "web_browse", "parallel_group": None}]


def test_extract_json_array_prose_wrapped():
    raw = 'Here is the plan:\n[{"id": 1, "label": "Search", "tool": null, "parallel_group": null}]\nDone.'
    steps = engine._extract_json_array(raw)
    assert steps is not None and steps[0]["label"] == "Search"


def test_extract_json_array_empty_plan_is_valid():
    assert engine._extract_json_array("[]") == []


def test_extract_json_array_no_brackets_returns_none():
    assert engine._extract_json_array("I don't think any tools are needed here.") is None


# ---------------------------------------------------------------------------
# _format_plan_for_discord — the one-shot "here's what I'm doing" DM
# ---------------------------------------------------------------------------

_SAMPLE_PLAN = [
    {"id": 1, "label": "Search the web for Schopenhauer", "tool": "web_browse", "parallel_group": None},
    {"id": 2, "label": "Check email", "tool": "gmail_action", "parallel_group": "A"},
    {"id": 3, "label": "Fetch the article", "tool": "fetch_page", "parallel_group": "A"},
    {"id": 4, "label": "Synthesize and write the note", "tool": None, "parallel_group": None},
]


def test_format_plan_for_discord_includes_every_step():
    text = engine._format_plan_for_discord("create a note for Schopenhauer", _SAMPLE_PLAN)
    for step in _SAMPLE_PLAN:
        assert step["label"] in text


def test_format_plan_for_discord_shows_tool_and_parallel_group():
    text = engine._format_plan_for_discord("x", _SAMPLE_PLAN)
    assert "(web_browse)" in text
    assert "[parallel A]" in text


def test_format_plan_for_discord_step_without_tool_has_no_tool_suffix():
    text = engine._format_plan_for_discord("x", _SAMPLE_PLAN)
    lines = text.splitlines()
    synth_line = next(l for l in lines if "Synthesize" in l)
    assert "(" not in synth_line   # no tool, no parallel group -- bare label


def test_format_plan_for_discord_no_per_step_status_language():
    # This is a one-shot notice, not a progress feed -- must not look like a
    # live status update (no "done"/"in progress"/percentage language).
    text = engine._format_plan_for_discord("x", _SAMPLE_PLAN)
    low = text.lower()
    assert "done" not in low or "i'll message you when it's done" in low
    assert "%" not in text


def test_format_plan_for_discord_truncates_long_request():
    long_text = "x" * 300
    text = engine._format_plan_for_discord(long_text, _SAMPLE_PLAN)
    assert "…" in text
    assert len(text.splitlines()[0]) < 160


def test_format_plan_for_discord_ends_with_completion_notice():
    text = engine._format_plan_for_discord("x", _SAMPLE_PLAN)
    assert "message you when it's done" in text.lower()


# ---------------------------------------------------------------------------
# Plan notifier DI hook — set_plan_notifier / _notify_plan_discord
# ---------------------------------------------------------------------------

def test_notify_plan_discord_calls_the_injected_notifier():
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    engine.set_plan_notifier(fake_notifier)
    try:
        asyncio.run(engine._notify_plan_discord("do the thing", _SAMPLE_PLAN))
        assert len(sent) == 1
        assert "Search the web for Schopenhauer" in sent[0]
    finally:
        engine.set_plan_notifier(None)


def test_notify_plan_discord_noop_without_notifier():
    engine.set_plan_notifier(None)
    asyncio.run(engine._notify_plan_discord("x", _SAMPLE_PLAN))  # must not raise


def test_notify_plan_discord_swallows_notifier_failure():
    async def broken_notifier(text):
        raise RuntimeError("discord is down")

    engine.set_plan_notifier(broken_notifier)
    try:
        asyncio.run(engine._notify_plan_discord("x", _SAMPLE_PLAN))  # must not raise
    finally:
        engine.set_plan_notifier(None)


# ---------------------------------------------------------------------------
# Timeout/token-budget regression -- pin the fixed values so a future revert
# doesn't silently reintroduce the live "_plan_task failed ()" bug. Two
# distinct live-verified failure modes, both against the real Moonshot API
# with a real key:
#   1. Timeout too tight -- observed real _plan_task/_replan failures at
#      11-19s elapsed against the 8.0s, then 15.0s caps.
#   2. Token budget too tight -- kimi-k2.5 reasons at length in
#      reasoning_content (observed up to ~4900 chars for one call) and that
#      reasoning shares the SAME max_tokens budget as the answer; at
#      max_tokens=1200 the reasoning alone consumed the whole budget and the
#      JSON got cut off (finish_reason="length") on any non-trivial request,
#      independent of the timeout. A live 4-request batch through the real
#      _plan_task (not just the raw API call) at max_tokens=4000 completed
#      cleanly end to end in 17.2-40.75s, so both the token budget and the
#      timeout needed raising together -- fixing only one would have left
#      the other failure mode live.
# ---------------------------------------------------------------------------

def test_plan_timeout_has_headroom_above_observed_live_failures():
    observed_worst_case_seconds = 40.75
    assert engine._PLAN_TIMEOUT > observed_worst_case_seconds


def test_plan_task_token_budget_above_the_live_truncation_point():
    # The exact live failure: reasoning_content alone consumed the full
    # max_tokens=1200 budget, cutting the JSON off after ~170 chars.
    observed_truncating_budget = 1200
    assert engine._PLAN_TASK_MAX_TOKENS > observed_truncating_budget * 2


def test_replan_token_budget_raised_from_the_old_undersized_value():
    observed_old_budget = 1000
    assert engine._REPLAN_MAX_TOKENS > observed_old_budget * 2


def test_plan_task_and_replan_disable_thinking(monkeypatch):
    """Both planner calls must pass their active provider's no_thinking_body as
    extra_body (2026-09-01 origin): both providers' standard tier thinks by
    default, and that reasoning shares max_tokens with the JSON answer -- on the
    research picker the identical omission burned the entire budget on narration
    with no JSON ever emitted. Live-verified: thinking disabled = finish_reason
    "stop" + clean JSON; default = finish_reason "length", empty content. A
    behavioral check against a fake client rather than source-inspection, since
    the call sites now read provider.no_thinking_body rather than a fixed
    KIMI_NO_THINKING_BODY literal -- true for both the Kimi and OpenRouter
    provider descriptor."""
    from backend.brain import router as router_mod

    class _FakeMessage:
        content = '[{"id": 1, "label": "Do a thing", "tool": null, "parallel_group": null}]'
        reasoning_content = None

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResp:
        usage = None
        choices = [_FakeChoice()]

    class _FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return _FakeResp()

    class _FakeClient:
        def __init__(self):
            completions = _FakeCompletions()
            self.completions = completions
            self.chat = type("Chat", (), {"completions": completions})()

    for provider in (router_mod.OPENROUTER,):
        monkeypatch.setattr(engine, "_provider", lambda p=provider: p)

        client = _FakeClient()
        asyncio.run(engine._plan_task(client, "do something", tools=[]))
        _assert_no_thinking(client.completions.calls[0]["extra_body"], provider)
        assert client.completions.calls[0]["model"] == provider.standard

        client2 = _FakeClient()
        asyncio.run(engine._replan(client2, "orig task", [], ["some error"], [{"id": 1, "label": "x"}]))
        _assert_no_thinking(client2.completions.calls[0]["extra_body"], provider)
        assert client2.completions.calls[0]["model"] == provider.standard


def test_plan_task_and_replan_use_temperature_0_6():
    """Pins the temperature required when thinking is disabled (2026-09-01,
    live-verified against the real Moonshot API): kimi-k2.6 pins this model
    to exactly one allowed temperature, but WHICH one flips with the
    thinking-mode extra_body -- 1.0 with thinking on, 0.6 with it explicitly
    disabled. The 400 ("only 0.6 is allowed for this model") this pins
    against fired the moment KIMI_NO_THINKING_BODY was added without also
    updating temperature to match. Lives on Provider.no_thinking_temperature
    rather than a bare literal at the call site, so this pins both the call
    sites' source AND the descriptor's actual value."""
    import inspect
    from backend.brain import router
    for fn in (engine._plan_task, engine._replan):
        src = inspect.getsource(fn)
        assert "temperature=provider.no_thinking_temperature" in src, \
            f"{fn.__name__} not using provider.no_thinking_temperature"
        assert "temperature=1.0" not in src, f"{fn.__name__} still has the thinking-enabled temperature"
    assert router.OPENROUTER.no_thinking_temperature == 0.6


def test_chat_compat_uses_provider_thinking_temperature():
    """F2 (2026-09-08): _chat_compat used to compute `1.0 if use_thinking else
    None` as a bare literal -- Moonshot's own requirement, applied
    unconditionally to whichever provider was active and never verified
    against DeepSeek. Live-verified against the real OpenRouter API that day:
    DeepSeek V4 Pro's thinking tier accepted temperature 0.0, 0.6, 1.0, and
    2.0 alike (all finish_reason "stop") -- unlike Moonshot, it does not pin
    this at all. 1.0 is kept (not None) purely to leave OpenRouter's live
    behavior byte-identical to what it already was.

    F4 (2026-09-08) folded this resolution into _reasoning_config(), the
    single derivation of "which tier is this call" shared with the
    extra_body reasoning toggle -- so this now pins _reasoning_config's
    source (where the literal actually lives) AND that _chat_compat calls it,
    plus the descriptor's actual value, same discipline as
    test_plan_task_and_replan_use_temperature_0_6 above."""
    import inspect
    from backend.brain import router
    assert "temperature = provider.thinking_temperature if use_thinking else None" \
        in inspect.getsource(engine._reasoning_config), \
        "_reasoning_config not reading provider.thinking_temperature"
    assert "_reasoning_config(provider, use_thinking)" in inspect.getsource(engine._chat_compat), \
        "_chat_compat not calling _reasoning_config"
    assert router.OPENROUTER.thinking_temperature == 1.0


def _assert_no_thinking(extra_body, provider):
    """The cheap tier must disable reasoning; since 2026-09-21 the same body also
    carries the OpenRouter routing preference (router.with_provider_prefs), so the
    disable half is asserted exactly and the provider half alongside it.

    Provider-agnostic on purpose: Moonshot's disable key is `thinking`, OpenRouter's is
    `reasoning`, so this compares against whatever that provider's own no_thinking_body
    actually contains rather than hardcoding either spelling.
    """
    for k, v in provider.no_thinking_body.items():
        assert extra_body[k] == v, (k, extra_body.get(k), v)
    if provider.provider_prefs:
        assert extra_body["provider"] == provider.provider_prefs
    else:
        assert set(extra_body) == set(provider.no_thinking_body)
