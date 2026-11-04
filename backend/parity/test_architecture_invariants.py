"""A10: one active implementation per responsibility, and no archival paths reachable.

Acceptance: *"Active dependency graph excludes archival paths; no duplicated
provider/tool loop remains reachable; a clean headless startup and the full parity
matrix pass; documentation describes the actual active runtime."*

These are structural invariants rather than behaviours, so they are enforced by walking
the real source. Each one has already been violated at least once in this migration —
that is why it is pinned rather than trusted.
"""
import ast
import pathlib
import re
from collections import Counter

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]          # backend/
SOURCE_FILES = [p for p in ROOT.rglob("*.py")
                if not p.name.startswith("test_") and p.name != "conftest.py"]


def _all_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in SOURCE_FILES)


# ------------------------------------------------- forbidden dependency directions
LAYERS = ("brain", "tools", "context", "storage", "providers")


@pytest.mark.parametrize("layer", LAYERS)
def test_no_layer_imports_the_entry_point(layer):
    """`backend.main` instantiates real audio hardware and owns the FastAPI app.
    Importing it from a library module would re-run that on import — which is exactly
    why `background.set_broadcaster`, `discord_bridge.set_turn_handler` and the other
    DI hooks exist.
    """
    offenders = []
    for path in (ROOT / layer).rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*(?:from\s+backend\.main|import\s+backend\.main|"
                     r"from\s+backend\s+import\s+main)\b", src, re.M):
            offenders.append(path.name)
    assert offenders == [], f"{layer} imports the entry point: {offenders}"


def test_storage_does_not_depend_on_brain():
    """Storage is the bottom layer; a cycle here would make the runtime store depend on
    the features that use it."""
    offenders = []
    for path in (ROOT / "storage").rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*from\s+backend\.brain", src, re.M):
            offenders.append(path.name)
    assert offenders == [], f"storage depends on brain: {offenders}"


# ------------------------------------------- one implementation per responsibility
def test_llm_clients_are_built_only_by_the_gateway():
    """A01 pooled the OpenAI-compatible clients; A10 brought Anthropic in too.
    `consolidation._call_claude` and `research._llm` used to construct a FRESH client
    per call — a new TLS handshake every consolidation and every research tick, owned
    by nobody and closed by nothing."""
    CLIENTS = {"OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic"}
    # memory.get_client is a deliberate module-level singleton for the Claude-mode
    # chat/background path; everything else must go through the pool.
    ALLOWED = {"gateway.py", "memory.py"}

    offenders = []
    for path in SOURCE_FILES:
        if path.name in ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        # AST, not a text search: an earlier draft of this test flagged a DOCSTRING in
        # engine.py that quotes `AsyncOpenAI(api_key="")` while explaining the F3 fix.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in CLIENTS:
                offenders.append(f"{path.name}:{node.lineno} {name}(...)")
    assert offenders == [], "LLM clients built outside the gateway:\n  " + "\n  ".join(offenders)


def test_tool_execution_policy_has_one_owner():
    """A04 slice 3 converged `_exec_tool` and `_bg_exec`. Neither may re-grow its own
    validation/classification/receipt logic."""
    from backend.brain import background, engine

    import inspect
    for fn in (engine._exec_tool, background._bg_exec):
        src = inspect.getsource(fn)
        assert "gateway.execute(" in src or "_gateway.execute(" in src, (
            f"{fn.__name__} no longer routes through the shared tool gateway")


def test_receipts_are_written_only_by_the_gateway():
    """`engine._receipt` was left behind by that convergence — a second receipt-writing
    implementation, dead but reachable-looking. Removed in A10."""
    from backend.brain import engine

    assert not hasattr(engine, "_receipt"), (
        "a duplicate receipt writer reappeared in engine.py")


# ----------------------------------------------------- no archival paths reachable
def test_the_legacy_llm_self_model_path_is_gone():
    """A whole alternative digest implementation — LLM analysis over source files —
    sat unused behind a LEGACY comment. It duplicated
    `_build_digest_from_claudemd()`'s responsibility and made paid API calls if ever
    invoked. Recoverable from git history; see CLAUDE.md for the commit."""
    from backend.brain import self_model

    for gone in ("_build_digest", "_analyze_section_sync", "_run_analysis_pass",
                 "_make_kimi_client", "_resolve_files", "_build_files_block"):
        assert not hasattr(self_model, gone), f"{gone} is back"
    assert hasattr(self_model, "_build_digest_from_claudemd"), (
        "the ACTIVE digest builder was removed by mistake")


def _is_route(node) -> bool:
    """@app.get(...), @router.post(...): registered by the decorator, called by FastAPI."""
    return any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
               and isinstance(d.func.value, ast.Name) and d.func.value.id in {"app", "router"}
               for d in node.decorator_list)


def test_no_unreferenced_module_level_functions():
    """Dead code that still looks live is what A10 exists to remove. FastAPI route
    handlers are referenced only by their decorator, so they are the allowed
    exceptions."""
    DECORATED_ROUTES = {"graceful_shutdown", "websocket_endpoint", "health"}

    defined = {}
    for path in SOURCE_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_route(node):
                defined.setdefault(node.name, path.name)

    # Tests count as references: a repository query API like `uncertain_operations`
    # or `pool_size` is exercised there and is part of the operator surface A09 pins.
    # What this catches is code referenced by NOTHING AT ALL -- which is exactly the
    # shape of everything removed in A10.
    # Every identifier counted once: a function name is all word characters, so its count
    # here equals a \bname\b search. Searching the whole blob once per function took 404 s
    # of a 14-minute suite run (4 November 2026); this takes under 2 s.
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in ROOT.rglob("*.py"))
    words = Counter(re.findall(r"\w+", blob))
    dead = [f"{where}::{name}" for name, where in sorted(defined.items())
            if not name.startswith("__")
            and name not in DECORATED_ROUTES
            and words[name] <= 1]
    assert dead == [], f"unreferenced functions: {dead}"


# ------------------------------------------------------- the graph is the artifact
def test_the_code_graph_excludes_the_vault():
    """`jarvis-vault/` is JARVIS's own space with its own retrieval path. A first
    incremental rebuild silently pulled 397 vault nodes in before `.graphifyignore`
    existed."""
    import json

    graph = ROOT.parent / "graphify-out" / "graph.json"
    if not graph.exists():
        pytest.skip("graph.json not present (regenerable artifact)")
    nodes = json.loads(graph.read_text(encoding="utf-8")).get("nodes", [])
    leaked = [n for n in nodes if "jarvis-vault" in str(n.get("file", ""))]
    assert leaked == [], f"{len(leaked)} vault nodes leaked into the code graph"


def test_the_code_graph_does_not_describe_removed_code():
    """'Documentation describes the actual active runtime' — the graph is documentation
    too, and it silently answers for code that no longer exists unless rebuilt."""
    import json

    graph = ROOT.parent / "graphify-out" / "graph.json"
    if not graph.exists():
        pytest.skip("graph.json not present (regenerable artifact)")
    names = {n.get("name") for n in json.loads(graph.read_text(encoding="utf-8")).get("nodes", [])}
    for gone in ("_build_digest", "_analyze_section_sync", "_receipt", "mood_context"):
        assert gone not in names, (
            f"the code graph still contains {gone} — run `python -m graphify update .`")


# ------------------------------------------------------- P1: nothing sends around the gateway
def test_email_is_only_ever_reached_through_the_tool_gateway():
    """The approval policy lives in `tools/gateway.execute`. It only protects anything if
    that is the ONLY way to reach `gmail_action`: a module that imported the function and
    called it directly would send with no quote check, no second look, no approval.

    Today exactly two modules import it: `engine.py`, which puts it in TOOL_DISPATCH (only
    `_exec_tool`, `background._bg_exec` and `approval_flow.approve` dispatch from there, all
    through the gateway), and `watches.py`, which builds its own dispatch table for
    `gateway.execute` with fixed read-only arguments.
    """
    importers = []
    for path in SOURCE_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "backend.tools.gmail":
                if any(a.name in ("gmail_action", "_do") for a in node.names):
                    importers.append(path.relative_to(ROOT).as_posix())
    assert sorted(set(importers)) == ["brain/engine.py", "brain/watches.py"], importers


def test_watches_touches_the_mail_function_only_to_build_its_gateway_dispatch_table():
    tree = ast.parse((ROOT / "brain" / "watches.py").read_text(encoding="utf-8"))
    users = []
    for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if any(isinstance(n, ast.Name) and n.id == "gmail_action" for n in ast.walk(func)):
            users.append(func.name)
    assert users == ["_dispatch"], users
    src = (ROOT / "brain" / "watches.py").read_text(encoding="utf-8")
    assert "gateway.execute(tool, args, dispatch=_dispatch()" in src


def test_the_only_dispatchers_of_the_tool_table_go_through_the_gateway():
    """`TOOL_DISPATCH` is only ever passed to `gateway.execute`: three call sites."""
    offenders = []
    for path in SOURCE_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # TOOL_DISPATCH[...] / TOOL_DISPATCH.get(...) : calling a tool function directly
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                    and node.value.id == "TOOL_DISPATCH" and isinstance(node.ctx, ast.Load):
                offenders.append(f"{path.name}:{node.lineno} subscript")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "TOOL_DISPATCH" \
                    and node.func.attr in ("get", "__getitem__"):
                offenders.append(f"{path.name}:{node.lineno} .{node.func.attr}")
    assert offenders == [], offenders


# ------------------------------------------------- background work is held until done
def _dropped_tasks(path: pathlib.Path) -> list[str]:
    """Statements that start a task and throw the task away."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr in ("create_task", "ensure_future"):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_no_task_is_started_and_dropped():
    """The loop keeps only a weak reference to a task, so one nobody holds can be collected
    mid-run. Fire-and-forget work goes through backend.tasks.spawn, which holds it."""
    offenders = [hit for p in SOURCE_FILES if p.name != "tasks.py" for hit in _dropped_tasks(p)]
    assert not offenders, "use backend.tasks.spawn: " + ", ".join(offenders)


def test_the_dropped_task_check_sees_one(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("import asyncio\nasync def f():\n    x = 1\n    asyncio.create_task(g())\n")
    global ROOT
    old, ROOT = ROOT, tmp_path
    try:
        assert _dropped_tasks(planted) == ["planted.py:4"]
    finally:
        ROOT = old
