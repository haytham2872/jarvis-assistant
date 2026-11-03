"""Tests for backend/brain/background.py's tool allowlist.

Scoped to _ALLOWED_TOOLS / _bg_tools() / _bg_exec() -- specifically the
addition of code_graph, so a background task (run_in_background) or an
overnight night task (chronos.py's execute_task(), which shares this exact
_bg_tools()/_bg_exec() machinery) can trace the real call graph instead of
guessing from file names. No real subprocess/graphify call is made --
_bg_exec's dispatch is verified against a stubbed TOOL_DISPATCH entry.
"""

import asyncio

import pytest

from backend.brain import background as bg
from backend.tools.registry import TOOLS


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _ALLOWED_TOOLS / _bg_tools — schema-level exposure
# ---------------------------------------------------------------------------

def test_code_graph_is_allowed():
    assert "code_graph" in bg._ALLOWED_TOOLS


def test_bg_tools_includes_code_graph_schema():
    names = [t["name"] for t in bg._bg_tools()]
    assert "code_graph" in names


def test_bg_tools_schema_matches_registry_entry():
    """The schema _bg_tools() exposes must be the SAME dict registry.py
    defines -- not a hand-copied duplicate that could drift."""
    registry_schema = next(t for t in TOOLS if t["name"] == "code_graph")
    bg_schema = next(t for t in bg._bg_tools() if t["name"] == "code_graph")
    assert bg_schema is registry_schema


def test_still_excluded_tools_remain_excluded():
    """Guards against accidentally loosening the allowlist beyond code_graph
    while making this change -- these must never appear in a background task."""
    excluded = {
        "run_in_background", "check_background_tasks",
        "vault_write_queue", "get_self_model",
    }
    assert excluded.isdisjoint(bg._ALLOWED_TOOLS)
    names = {t["name"] for t in bg._bg_tools()}
    assert excluded.isdisjoint(names)


# ---------------------------------------------------------------------------
# _bg_exec — dispatch actually reaches code_graph, respects the allowlist
# ---------------------------------------------------------------------------

def test_bg_exec_dispatches_to_code_graph(monkeypatch):
    from backend.brain import engine
    calls = {}

    async def fake_code_graph(**kwargs):
        calls.update(kwargs)
        return "explained: chat() calls vault_write()"

    monkeypatch.setitem(engine.TOOL_DISPATCH, "code_graph", fake_code_graph)
    result = run(bg._bg_exec("code_graph", {"action": "explain", "query": "chat"}))
    assert calls == {"action": "explain", "query": "chat"}
    assert "chat()" in result


def test_bg_exec_rejects_a_tool_not_on_the_allowlist():
    result = run(bg._bg_exec("vault_write_queue", {"content": "x"}))
    assert result.startswith("Error:")
    assert "not available in background tasks" in result


def test_bg_exec_returns_string_even_on_dispatch_exception(monkeypatch):
    from backend.brain import engine

    async def boom(**kwargs):
        raise RuntimeError("graphify not installed")

    monkeypatch.setitem(engine.TOOL_DISPATCH, "code_graph", boom)
    result = run(bg._bg_exec("code_graph", {"action": "explain", "query": "x"}))
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# execute_task() (chronos.py's night-task path) shares the same tool set --
# proven by construction: it calls the same runner functions that call
# _bg_tools()/_bg_exec() internally, so no separate allowlist exists to drift.
# ---------------------------------------------------------------------------

def test_execute_task_and_run_in_background_share_one_allowlist():
    """There is exactly one _ALLOWED_TOOLS -- execute_task (night tasks) and
    the run_in_background worker both call _run_compat_loop,
    which both call _bg_tools()/_bg_exec(). Nothing here can drift out of
    sync because there is nothing to duplicate."""
    import inspect
    src = inspect.getsource(bg.execute_task)
    assert "_run_compat_loop" in src
    assert "code_graph" in {t["name"] for t in bg._bg_tools()}


# ---------------------------------------------------------------------------
# openrouter mode -- dispatches to the OpenAI-compatible loop (same runner
# Kimi mode uses), with the cheap Flash model and reasoning explicitly off.
# ---------------------------------------------------------------------------

def test_execute_task_runs_the_compat_loop(monkeypatch):

    async def fake_compat_loop(task_text):
        return f"handled: {task_text}"

    monkeypatch.setattr(bg, "_run_compat_loop", fake_compat_loop)

    result = run(bg.execute_task("do X"))
    assert result == "handled: do X"


def test_run_compat_loop_disables_thinking_and_uses_flash_in_openrouter_mode(monkeypatch):
    from backend.brain import engine as claude_mod
    from backend.brain import router as router_mod
    from backend.brain.router import OPENROUTER_STANDARD, OPENROUTER_NO_THINKING_BODY

    # provider() (used for model selection) reads router.MODEL_MODE directly;
    # background.py's own extra_body gate reads its bare imported MODEL_MODE name --
    # both need patching since they're separate bindings of the same import.

    captured = []

    class _FakeMessage:
        content = "task result"
        tool_calls = None

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResp:
        usage = None
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _FakeResp()

    class _FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

    monkeypatch.setattr(claude_mod, "_get_compat_client", lambda: _FakeClient())
    monkeypatch.setattr(claude_mod, "_to_openai_tools", lambda tools: [])

    result = run(bg._run_compat_loop("do something"))

    assert (result.status, result.text) == (bg.COMPLETE, "task result")
    assert captured[0]["model"] == OPENROUTER_STANDARD
    # Reasoning stays disabled; since 2026-09-21 the body also carries the OpenRouter
    # routing preference merged in by router.with_provider_prefs.
    eb = captured[0]["extra_body"]
    assert eb["reasoning"] == OPENROUTER_NO_THINKING_BODY["reasoning"]
    assert eb["provider"] == router_mod.OPENROUTER.provider_prefs
