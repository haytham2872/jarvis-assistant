"""backend/tools/codegraph.py — structural self-knowledge, derived not authored.

JARVIS's self-model (backend/brain/self_model.py) is CLAUDE.md sliced into
prose: authoritative, hand-maintained, and accurate exactly as far as the owner
keeps it current. This module is the other half — a queryable graph of what
the source code *actually* does, extracted by graphify's tree-sitter AST pass.
Nobody writes those edges; they fall out of the real imports and calls, so
they cannot drift from the code the way a prose description can.

Same zero-LLM discipline as the self-model: `graphify query/explain/path/
affected` are pure local graph traversal (NetworkX over graphify-out/graph.json),
no API call, no tokens spent on the retrieval itself.

Runs graphify as a subprocess rather than importing it, deliberately: graphify
lives in the system Python (it is a developer tool installed alongside the
Claude Code skill), not in JARVIS's .venv, and keeping it at arm's length means
an uninstall degrades to one honest sentence instead of an ImportError at
startup. Same silent-degradation contract as every other tool here — every
path returns a string, nothing raises.

The graph is a *snapshot*. Source mtimes are compared against the graph's own
so a stale graph announces itself rather than quietly answering questions about
code that has since changed.
"""

import sys
import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_GRAPH_DIR    = _PROJECT_ROOT / "graphify-out"
_GRAPH_JSON   = _GRAPH_DIR / "graph.json"
# Written by the /graphify skill: the interpreter that actually has graphify
# importable. Preferred over sys.executable, which is JARVIS's own .venv.
_PY_SIDECAR   = _GRAPH_DIR / ".graphify_python"

_TIMEOUT      = 45.0   # seconds — a cold traversal of a ~1500-node graph runs ~5s
_MAX_CHARS    = 6000   # cap on returned text, same spirit as vault_read's cap

_NO_GRAPH = (
    "No code graph has been built yet, so I can't answer structural questions "
    "about my own source from it. Building one is a one-off: run `/graphify .` "
    "in Claude Code, or `python -m graphify update .` if graphify is installed. "
    "My self-model digest (from CLAUDE.md) still describes how I work."
)

_NO_GRAPHIFY = (
    "The code graph exists but graphify isn't installed in the interpreter I "
    "can reach, so I can't traverse it right now. `pip install graphifyy` fixes it."
)


def _interpreter() -> str:
    """Path to a Python that (probably) has graphify importable."""
    try:
        recorded = _PY_SIDECAR.read_text(encoding="utf-8").strip()
        if recorded and Path(recorded).exists():
            return recorded
    except Exception:
        pass
    return sys.executable


# Source trees whose mtimes decide whether the graph has gone stale.
_SOURCE_GLOBS = (
    ("backend", "*.py"),
    ("frontend/src", "*.js"),
    ("frontend/src", "*.jsx"),
)


def _newest_source_mtime() -> float:
    """Newest mtime across the source trees the graph was built from.

    Deliberately mtime-based rather than git-based: graph.json is itself
    committed, so any commit-sha comparison would report the graph as stale
    one commit after every rebuild — a warning that fires always is a warning
    nobody reads. File times answer the question actually being asked ("has
    code changed since this graph was built"). Returns 0.0 on any failure,
    which skips the note entirely.
    """
    newest = 0.0
    try:
        for subdir, pattern in _SOURCE_GLOBS:
            root = _PROJECT_ROOT / subdir
            if not root.exists():
                continue
            for path in root.rglob(pattern):
                if "__pycache__" in path.parts:
                    continue
                newest = max(newest, path.stat().st_mtime)
    except Exception:
        return 0.0
    return newest


def _freshness_note() -> str:
    """One line when source files have changed since the graph was built."""
    try:
        built_at = _GRAPH_JSON.stat().st_mtime
    except Exception:
        return ""
    newest = _newest_source_mtime()
    if not newest or newest <= built_at:
        return ""
    stale_days = (newest - built_at) / 86400
    when = f"{stale_days:.1f} days" if stale_days >= 1 else "hours"
    return (
        f"\n\n[Note: source files have changed in the {when} since this graph was "
        "built, so anything added since then is missing from it.]"
    )


async def _run(args: list[str]) -> str:
    """Run one graphify subcommand against the graph. Never raises."""
    cmd = [_interpreter(), "-m", "graphify", *args, "--graph", str(_GRAPH_JSON)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_PROJECT_ROOT),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: code graph query timed out after {_TIMEOUT:.0f}s."
        except asyncio.CancelledError:
            # Pipeline interrupt — don't leave the child process running.
            proc.kill()
            raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Degrade to a string like every other tool here, but leave a trace:
        # a tool that only ever fails quietly is exactly what canary.py exists
        # to catch elsewhere in this codebase.
        log.warning("code_graph subprocess failed (%s): %s", " ".join(args[:2]), e)
        return f"Error: {e}"

    out = stdout.decode("utf-8", errors="replace").strip()
    if "No module named graphify" in out:
        log.warning("code_graph: graphify not importable by %s", _interpreter())
        return _NO_GRAPHIFY
    if not out:
        return "The code graph returned nothing for that — try a different symbol or wording."
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS] + "\n… (truncated)"
    return out


def _clamp(value, default: int, low: int, high: int) -> int:
    """Bound a model-supplied integer. None means 'unset' — 0 does not.

    Deliberately not `int(value or default)`: 0 is falsy, so that idiom turns
    an explicit 0 into the default instead of clamping it to the minimum.
    """
    if value is None:
        return default
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


async def code_graph(
    action: str,
    query: str = "",
    target: str = "",
    depth: int = 2,
    top: int = 10,
) -> str:
    """Traverse the structural graph of JARVIS's own source code.

    Actions: query (BFS around a question), explain (one symbol and everything
    wired to it), path (shortest route between two symbols), affected (reverse
    traversal — what depends on a symbol), god_nodes (most-connected hubs).
    """
    if not _GRAPH_JSON.exists():
        return _NO_GRAPH

    action = (action or "").strip().lower()
    query  = (query or "").strip()
    target = (target or "").strip()

    if action == "query":
        if not query:
            return "Error: 'query' action needs a question."
        args = ["query", query, "--budget", "1500"]
    elif action == "explain":
        if not query:
            return "Error: 'explain' action needs a symbol name."
        args = ["explain", query]
    elif action == "path":
        if not query or not target:
            return "Error: 'path' action needs both query (start) and target (end) symbols."
        args = ["path", query, target]
    elif action == "affected":
        if not query:
            return "Error: 'affected' action needs a symbol name."
        args = ["affected", query, "--depth", str(_clamp(depth, 2, 1, 4))]
    elif action == "god_nodes":
        args = ["god-nodes", "--top", str(_clamp(top, 10, 1, 25))]
    else:
        return (
            f"Error: unknown code graph action '{action}'. "
            "Use query, explain, path, affected, or god_nodes."
        )

    result = await _run(args)
    if result.startswith("Error:") or result in (_NO_GRAPHIFY,):
        return result
    return result + _freshness_note()
