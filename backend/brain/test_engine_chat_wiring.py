"""Regression guard for a real production incident: chat() had a local
variable literally named `intent` (`intent = _format_intent_context(queue_raw)`)
while `backend.brain.intent` was also imported bare (`from backend.brain
import intent`) and called earlier in the same function as `intent.classify(...)`.

Python resolves a function's variable scope for the WHOLE function body at
compile time -- the later local assignment made `intent` local throughout
chat(), so the earlier `intent.classify(...)` call raised
`UnboundLocalError: cannot access local variable 'intent' where it is not
associated with a value` on every single turn. Every existing test mocked
around the exact code path and none caught it; this file is a static check
that runs in milliseconds with no mocking to route around.

Fixed by renaming the local to `intent_context`. This test doesn't just pin
that one rename -- it walks every top-level function in engine.py AND
main.py (main.py started importing module namespaces the same way when
self_model.run_loop was wired in) and asserts NONE of them assign a local
variable that shadows a bare module-namespace import (`intent`,
`background`, `research`, `usage`, `runtime_status`, `chronos`,
`predictions`, `rumination`, `self_model`, ...) that's called via dotted
attribute access (`module.func(...)`) elsewhere in the same function.
Catches this entire class of bug, not just this one instance, so a future
`research = ...`/`self_model = ...` local doesn't reintroduce it.
"""

import ast
import pathlib

import pytest

_BRAIN_DIR = pathlib.Path(__file__).parent
_TARGET_FILES = [
    _BRAIN_DIR / "engine.py",
    _BRAIN_DIR.parent / "main.py",
]


def _module_namespace_imports(tree: ast.Module) -> set[str]:
    """Names imported as `from backend.brain import X` (bare module
    namespaces meant to be used as `X.attr(...)`), excluding aliased or
    from-attribute imports which can't collide the same way."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("backend.brain"):
            for alias in node.names:
                # Only bare module imports: `from backend.brain import intent`
                # (alias.name == the module's own name, no `as`, and the
                # imported name is itself a submodule, not a function/class --
                # heuristic: skip anything that looks like a function/class,
                # i.e. starts lowercase is fine, but skip known non-module
                # imports by checking they're used as Attribute bases below
                # instead of guessing here).
                if alias.asname is None:
                    names.add(alias.name)
    return names


def _locals_assigned_in_function(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """All names ever used as an assignment target inside this function,
    at any nesting depth (including for-loops, with-statements, walrus)."""
    targets = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign,)):
            for t in node.targets:
                targets.update(_names_in_target(t))
        elif isinstance(node, ast.AnnAssign):
            targets.update(_names_in_target(node.target))
        elif isinstance(node, ast.AugAssign):
            targets.update(_names_in_target(node.target))
        elif isinstance(node, ast.NamedExpr):   # walrus :=
            targets.update(_names_in_target(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets.update(_names_in_target(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    targets.update(_names_in_target(item.optional_vars))
    return targets


def _names_in_target(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for elt in target.elts:
            out.update(_names_in_target(elt))
        return out
    return set()


def _dotted_attribute_bases_used(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names used as the base of a dotted call/attribute access, e.g. the
    `intent` in `intent.classify(...)`. A shadowed import only actually
    breaks something if the function also USES it this way."""
    bases = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            bases.add(node.value.id)
    return bases


@pytest.mark.parametrize("target", _TARGET_FILES, ids=lambda p: p.name)
def test_no_local_variable_shadows_a_used_module_import_in_any_function(target):
    tree = ast.parse(target.read_text(encoding="utf-8"))
    module_imports = _module_namespace_imports(tree)

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        locals_assigned = _locals_assigned_in_function(node)
        dotted_bases = _dotted_attribute_bases_used(node)
        # A collision only matters if the function BOTH assigns a local with
        # that name AND calls it as module.attr(...) somewhere in its body --
        # that combination is exactly what produces UnboundLocalError.
        shadowed = (locals_assigned & module_imports) & dotted_bases
        if shadowed:
            violations.append((node.name, shadowed))

    assert violations == [], (
        f"function(s) in {target.name} assign a local variable that shadows a "
        f"module import used via dotted attribute access in the same function -- "
        f"this raises UnboundLocalError at call time, not import time, so tests "
        f"that mock around the real code path won't catch it: {violations}"
    )


def test_engine_py_sanity_the_original_incident_is_still_detectable():
    """Confirms the checker itself still recognizes the exact import this bug
    involved -- if `intent` stopped being a module-namespace import here,
    the check above would silently stop covering the original incident."""
    tree = ast.parse((_BRAIN_DIR / "engine.py").read_text(encoding="utf-8"))
    assert "intent" in _module_namespace_imports(tree)


def test_main_py_sanity_self_model_import_is_detectable():
    """Same sanity check for main.py's newer self_model.run_loop wiring,
    added the same way and just as capable of reintroducing this bug."""
    tree = ast.parse((_BRAIN_DIR.parent / "main.py").read_text(encoding="utf-8"))
    assert "self_model" in _module_namespace_imports(tree)


def test_chat_specifically_no_longer_has_a_bare_intent_local():
    """Direct pin of the actual incident, kept alongside the general check
    above so the specific regression is legible without reading AST logic."""
    from backend.brain import engine
    assert "intent" not in engine.chat.__code__.co_varnames
    assert "intent_context" in engine.chat.__code__.co_varnames


# ------------------------------------------------------------------ live speech (phone)

@pytest.mark.parametrize("sentence", [
    "I've set the reminder for 9.", "The email was sent.", "the ledger is empty",
    "I've recorded the trade.", "I just sent it, check your mail.",
    "I've recorded the decline.",
])
def test_a_claim_sentence_is_not_speakable(sentence):
    from backend.brain import engine
    assert engine.speakable(sentence) is False


@pytest.mark.parametrize("sentence", [
    "Let me check.", "Your meeting is at nine.", "Il est neuf heures.", "I can't do that yet.",
    "The email from Sarah is about the lease.",
])
def test_an_ordinary_sentence_is_speakable(sentence):
    from backend.brain import engine
    assert engine.speakable(sentence) is True


def test_a_spoken_phone_turn_runs_no_foreground_plan_and_is_told_to_lead_with_the_answer():
    import inspect
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    line = src.split("allow_foreground_plan=")[1].split("\n")[0]
    assert 'channel == "mobile" and _turn_input.spoken.get()' in line and "goal_offer" in line
    assert "Put the answer in your first sentence and keep that sentence short" in engine._SPOKEN_NOTE
