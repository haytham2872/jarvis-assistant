"""Tests for backend/tools/codegraph.py — the code-graph traversal tool.

Covers this module's own logic only: argument validation, the degradation
paths, interpreter resolution, and the staleness note. The traversal itself
belongs to graphify and is deliberately not re-tested here — no subprocess is
spawned by this suite, so it stays fast and passes on a machine where graphify
was never installed.
"""

import sys
import asyncio
from pathlib import Path

import pytest

from backend.tools import codegraph


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def graph(tmp_path, monkeypatch):
    """Point the module at a throwaway graph.json that exists."""
    g = tmp_path / "graph.json"
    g.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(codegraph, "_GRAPH_JSON", g)
    monkeypatch.setattr(codegraph, "_PROJECT_ROOT", tmp_path)
    return g


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Degradation: no graph built
# ---------------------------------------------------------------------------

def test_missing_graph_explains_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setattr(codegraph, "_GRAPH_JSON", tmp_path / "nope.json")
    out = run(codegraph.code_graph("explain", "chat"))
    assert out == codegraph._NO_GRAPH
    assert "graphify" in out


def test_missing_graph_checked_before_action_validation(tmp_path, monkeypatch):
    """No graph is the more useful thing to say than 'bad action'."""
    monkeypatch.setattr(codegraph, "_GRAPH_JSON", tmp_path / "nope.json")
    assert run(codegraph.code_graph("garbage")) == codegraph._NO_GRAPH


# ---------------------------------------------------------------------------
# Argument validation — none of these should reach a subprocess
# ---------------------------------------------------------------------------

def test_unknown_action_is_rejected(graph):
    out = run(codegraph.code_graph("summarize"))
    assert out.startswith("Error:")
    assert "summarize" in out


@pytest.mark.parametrize("action", ["explain", "affected", "query"])
def test_actions_requiring_a_query_reject_empty(graph, action):
    assert run(codegraph.code_graph(action, "")).startswith("Error:")
    assert run(codegraph.code_graph(action, "   ")).startswith("Error:")


def test_path_requires_both_endpoints(graph):
    assert run(codegraph.code_graph("path", "chat()")).startswith("Error:")
    assert run(codegraph.code_graph("path", "", "vault_write()")).startswith("Error:")


def test_action_matching_is_case_and_space_insensitive(graph, monkeypatch):
    seen = {}

    async def fake_run(args):
        seen["args"] = args
        return "ok"

    monkeypatch.setattr(codegraph, "_run", fake_run)
    run(codegraph.code_graph("  ExPlAiN  ", "chat"))
    assert seen["args"][0] == "explain"


# ---------------------------------------------------------------------------
# Numeric clamping — a model passing something absurd must not reach the CLI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [(0, "1"), (2, "2"), (99, "4"), (-5, "1")])
def test_affected_depth_is_clamped(graph, monkeypatch, given, expected):
    seen = {}

    async def fake_run(args):
        seen["args"] = args
        return "ok"

    monkeypatch.setattr(codegraph, "_run", fake_run)
    run(codegraph.code_graph("affected", "vault_write", depth=given))
    assert seen["args"][seen["args"].index("--depth") + 1] == expected


@pytest.mark.parametrize("given,expected", [(0, "1"), (10, "10"), (500, "25")])
def test_god_nodes_top_is_clamped(graph, monkeypatch, given, expected):
    seen = {}

    async def fake_run(args):
        seen["args"] = args
        return "ok"

    monkeypatch.setattr(codegraph, "_run", fake_run)
    run(codegraph.code_graph("god_nodes", top=given))
    assert seen["args"][seen["args"].index("--top") + 1] == expected


# ---------------------------------------------------------------------------
# Interpreter resolution
# ---------------------------------------------------------------------------

def test_interpreter_prefers_the_recorded_sidecar(tmp_path, monkeypatch):
    real = Path(sys.executable)
    sidecar = tmp_path / ".graphify_python"
    sidecar.write_text(str(real), encoding="utf-8")
    monkeypatch.setattr(codegraph, "_PY_SIDECAR", sidecar)
    assert codegraph._interpreter() == str(real)


def test_interpreter_falls_back_when_sidecar_points_at_nothing(tmp_path, monkeypatch):
    sidecar = tmp_path / ".graphify_python"
    sidecar.write_text(str(tmp_path / "no-such-python.exe"), encoding="utf-8")
    monkeypatch.setattr(codegraph, "_PY_SIDECAR", sidecar)
    assert codegraph._interpreter() == sys.executable


def test_interpreter_falls_back_when_sidecar_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(codegraph, "_PY_SIDECAR", tmp_path / "missing")
    assert codegraph._interpreter() == sys.executable


# ---------------------------------------------------------------------------
# Staleness note — mtime-based, not commit-based
# ---------------------------------------------------------------------------

def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    import os
    os.utime(path, (mtime, mtime))


def test_no_note_when_graph_is_newer_than_source(graph, tmp_path):
    _touch(tmp_path / "backend" / "a.py", 1_000)
    import os
    os.utime(graph, (2_000, 2_000))
    assert codegraph._freshness_note() == ""


def test_note_when_source_is_newer_than_graph(graph, tmp_path):
    import os
    os.utime(graph, (1_000, 1_000))
    _touch(tmp_path / "backend" / "a.py", 1_000 + 3 * 86400)
    note = codegraph._freshness_note()
    assert "changed" in note and "3.0 days" in note


def test_note_says_hours_for_a_small_gap(graph, tmp_path):
    import os
    os.utime(graph, (1_000, 1_000))
    _touch(tmp_path / "backend" / "a.py", 1_000 + 600)
    assert "hours" in codegraph._freshness_note()


def test_pycache_is_ignored_when_measuring_staleness(graph, tmp_path):
    """A stale-looking .pyc must not make the graph look out of date."""
    import os
    os.utime(graph, (1_000, 1_000))
    _touch(tmp_path / "backend" / "__pycache__" / "a.py", 9_000)
    assert codegraph._freshness_note() == ""


def test_freshness_note_degrades_silently_without_sources(graph, tmp_path):
    assert codegraph._freshness_note() == ""


def test_frontend_sources_count_toward_staleness(graph, tmp_path):
    import os
    os.utime(graph, (1_000, 1_000))
    _touch(tmp_path / "frontend" / "src" / "App.jsx", 1_000 + 2 * 86400)
    assert "changed" in codegraph._freshness_note()


# ---------------------------------------------------------------------------
# Output handling
# ---------------------------------------------------------------------------

def test_missing_graphify_module_is_reported_plainly(graph, monkeypatch):
    async def fake_exec(*a, **k):
        class P:
            async def communicate(self):
                return b"ModuleNotFoundError: No module named graphify", b""
            def kill(self): pass
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert run(codegraph.code_graph("god_nodes")) == codegraph._NO_GRAPHIFY


def test_long_output_is_truncated(graph, monkeypatch):
    async def fake_exec(*a, **k):
        class P:
            async def communicate(self):
                return b"x" * 50_000, b""
            def kill(self): pass
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = run(codegraph.code_graph("god_nodes"))
    assert len(out) < 50_000
    assert "truncated" in out


def test_errors_skip_the_freshness_note(graph, monkeypatch, tmp_path):
    """A failure message shouldn't be padded with an unrelated staleness note."""
    import os
    os.utime(graph, (1_000, 1_000))
    _touch(tmp_path / "backend" / "a.py", 9_000)

    async def fake_run(args):
        return "Error: boom"

    monkeypatch.setattr(codegraph, "_run", fake_run)
    assert run(codegraph.code_graph("god_nodes")) == "Error: boom"


def test_subprocess_failure_returns_an_error_string(graph, monkeypatch):
    async def fake_exec(*a, **k):
        raise OSError("no such interpreter")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = run(codegraph.code_graph("god_nodes"))
    assert out.startswith("Error:")


def test_cancellation_propagates(graph, monkeypatch):
    """Interrupt must not be swallowed into an 'Error:' string."""
    killed = {}

    async def fake_exec(*a, **k):
        class P:
            async def communicate(self):
                raise asyncio.CancelledError()
            def kill(self): killed["yes"] = True
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(asyncio.CancelledError):
        run(codegraph.code_graph("god_nodes"))
    assert killed.get("yes"), "child process should be killed on interrupt"


# ---------------------------------------------------------------------------
# Registration — the tool is only usable if it is wired in
# ---------------------------------------------------------------------------

def test_tool_is_registered_and_dispatchable():
    from backend.tools.registry import TOOLS
    from backend.brain.engine import TOOL_DISPATCH, tools_for_channel

    schema = next((t for t in TOOLS if t["name"] == "code_graph"), None)
    assert schema is not None, "code_graph missing from the tool registry"
    assert TOOL_DISPATCH.get("code_graph") is codegraph.code_graph

    actions = schema["input_schema"]["properties"]["action"]["enum"]
    assert set(actions) == {"explain", "affected", "path", "query", "god_nodes"}
    assert schema["input_schema"]["required"] == ["action"]

    # Read-only and side-effect free — no reason to withhold it from Discord.
    assert "code_graph" in [t["name"] for t in tools_for_channel("discord")]


def test_every_registered_action_is_handled(graph, monkeypatch):
    """The schema enum and the dispatch branches must not drift apart."""
    from backend.tools.registry import TOOLS

    async def fake_run(args):
        return "ok"

    monkeypatch.setattr(codegraph, "_run", fake_run)
    schema = next(t for t in TOOLS if t["name"] == "code_graph")
    for action in schema["input_schema"]["properties"]["action"]["enum"]:
        out = run(codegraph.code_graph(action, query="chat()", target="vault_write()"))
        assert not out.startswith("Error: unknown"), f"{action} not handled"
