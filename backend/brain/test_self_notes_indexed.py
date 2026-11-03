"""Notes under `self/` must be written through `vault_write`, so they stay searchable.

`vault_write` fires `semindex.index_file()`; a raw `path.write_text()` does not, and
`backfill()` skips a file it has already seen once. So a note first indexed when it was
small stays indexed at that size forever while the file grows underneath it.

Measured on the real vault before the fix:

    self/evolution.md        1 chunk    1,079 indexed / 16,299 on disk =  6.6%
    self/mood-patterns.md    1 chunk      654 indexed /  2,257 on disk = 29.0%

Both are injected into the system prompt and searched semantically, so "when did you
get X" was searching a months-old prefix. `evolution.md` is now owned by
`backend/brain/evolution.py`; `mood-patterns.md` is written by consolidation.

Deliberately scoped to `self/`. `drafts/listening-log.md` and the root-level
`schedule.md` / `research-queue.md` are written by hand on purpose — high-churn
operational logs kept *out* of the index, documented as such where they are written.
"""
import ast
import asyncio
import inspect
import pathlib

import pytest

from backend.brain import consolidation as C


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# mood-patterns.md — the behavioural half
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real consolidate_session against an isolated vault, no LLM, no Qdrant."""
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    from backend.storage import db
    db._reset_for_tests()

    vault = tmp_path / "vault"
    for d in ("letters", "people", "topics", "self", "creative",
              "conversations/significant"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(C, "_VAULT", vault)

    async def fake_generate(*a, **k):
        return {"letter": "a letter", "mood_patterns_update": "He gets terse when tired."}

    async def fake_threads(_sid):
        return [{"user": "hi", "assistant": "hello", "timestamp": 1.0}]

    async def fake_sleep():
        return None

    monkeypatch.setattr(C, "_generate_consolidation", fake_generate)
    monkeypatch.setattr(C, "threads_for_session", fake_threads)
    monkeypatch.setattr(C, "latest_sleep", fake_sleep)
    monkeypatch.setattr(C, "_snapshot_unconsolidated_ids_sync", lambda _u: [])
    monkeypatch.setattr(C, "_mark_facts_consolidated_sync", lambda ids: None)

    import backend.tools.vault as V
    monkeypatch.setattr(V, "_VAULT", vault)

    indexed: list[tuple] = []
    monkeypatch.setattr(V, "_index_note_async",
                        lambda folder, filename, *a, **k: indexed.append((folder, filename)))

    yield {"vault": vault, "indexed": indexed, "V": V}
    db._reset_for_tests()


def test_mood_patterns_is_indexed_when_written(env):
    """The fix: the append goes through `vault_write`, which re-indexes."""
    run(C.consolidate_session("sess-aaaaaaaa-1"))

    text = (env["vault"] / "self" / "mood-patterns.md").read_text(encoding="utf-8")
    assert "He gets terse when tired." in text
    assert ("self", "mood-patterns.md") in env["indexed"], (
        "mood-patterns.md was written without re-indexing — the 29%-coverage defect")


def test_mood_patterns_still_appends_rather_than_overwriting(env):
    """Routing through `vault_write` (which overwrites) must not turn the append into
    a replace -- the file's whole value is its accumulated history."""
    mp = env["vault"] / "self" / "mood-patterns.md"
    mp.write_text("# Mood Patterns\n\n## 2026-01-01\nAn older observation.\n",
                  encoding="utf-8")

    run(C.consolidate_session("sess-bbbbbbbb-1"))

    text = mp.read_text(encoding="utf-8")
    assert "An older observation." in text
    assert "He gets terse when tired." in text
    assert text.index("An older observation.") < text.index("He gets terse when tired.")


def test_a_failed_mood_patterns_write_is_recorded_as_failed(env, monkeypatch):
    """A05 slice 1's contract: `vault_write` signals failure by returning a string, so
    the result has to be classified rather than assumed."""
    from backend.storage import consolidation_state as cstate

    async def failing_write(filename, content, folder):
        return "Error writing to vault: disk full"

    monkeypatch.setattr(env["V"], "vault_write", failing_write)

    sid = "sess-cccccccc-1"
    run(C.consolidate_session(sid))

    done = cstate.completed_outputs(sid)
    assert "mood_patterns" not in done, (
        "a failed mood-patterns write was recorded as completed, so a retry would skip it")


# ---------------------------------------------------------------------------
# the static guard — the defect class, not the two instances
# ---------------------------------------------------------------------------

_BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _self_note_write_offenders(src: str) -> list[str]:
    """Variables bound to a `.../ "self" / ...` path and then written with write_text,
    **within one function**.

    Function scope is load-bearing, not tidiness: `tools/vault.py` binds a local called
    `path` to a self/ note in its positions reader AND calls `path.write_text(...)` in
    `vault_write` — different functions, unrelated locals. A file-wide scan reports that
    as an offender, which would make the guard wrong about the one module that does
    indexing correctly.

    Regex was tried first and was worse in both directions: it also missed every
    indented assignment until `re.MULTILINE` was added, and the planted-offender fixture
    below did not catch that because its offending line happened to sit at position 0.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        bound: set[str] = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            parts = [n.value for n in ast.walk(node.value)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if "self" in parts:
                bound.add(target.id)

        if not bound:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_text"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in bound):
                offenders.append(f"{fn.name}: {node.func.value.id}.write_text(...)")
    return offenders


def _modules():
    for p in sorted(_BACKEND.rglob("*.py")):
        if p.name.startswith("test_") or "conftest" in p.name:
            continue
        yield p


def test_no_module_writes_a_self_note_with_raw_write_text():
    """The guard. `mp_path = _VAULT / "self" / "mood-patterns.md"` followed by
    `mp_path.write_text(...)` is exactly the shape that shipped, twice."""
    offenders = []
    for path in _modules():
        src = path.read_text(encoding="utf-8", errors="replace")
        if '"self"' not in src:
            continue
        offenders += [f"{path.relative_to(_BACKEND)}: {o}"
                      for o in _self_note_write_offenders(src)]

    assert not offenders, (
        "a self/ note is written without re-indexing, so it will silently stop being "
        "searchable as it grows: " + "; ".join(offenders))


def test_the_guard_detects_a_planted_offender():
    """Proof the scan is not passing vacuously -- indented and not the first line,
    the two things an earlier regex version got wrong."""
    src = (
        "import pathlib\n"
        "\n"
        "def write_it():\n"
        '    p = _VAULT / "self" / "notes.md"\n'
        '    p.write_text("x", encoding="utf-8")\n'
    )
    assert _self_note_write_offenders(src) == ["write_it: p.write_text(...)"]


def test_reading_a_self_note_with_pathlib_is_not_flagged():
    """Reads are fine and common (personality.py injects these files); only writes
    bypass indexing."""
    src = ("def read_it():\n"
           '    p = _VAULT / "self" / "evolution.md"\n'
           '    return p.read_text(encoding="utf-8")\n')
    assert _self_note_write_offenders(src) == []


def test_an_unrelated_local_in_another_function_is_not_flagged():
    """The real false positive this guard hit: `tools/vault.py` reads a self/ note into
    a local called `path` in one function and writes an unrelated `path` in another."""
    src = ("def read_positions():\n"
           '    path = _VAULT / "self" / "positions.md"\n'
           "    return path.read_text()\n"
           "\n"
           "def vault_write(folder, filename, content):\n"
           "    path = _VAULT / folder / filename\n"
           "    path.write_text(content)\n")
    assert _self_note_write_offenders(src) == []


def test_every_self_note_writer_routes_through_vault_write():
    """The positive statement, so a NEW self/ writer that skips vault_write entirely
    (never touching write_text) is still caught."""
    from backend.brain import evolution, positions, rumination

    for fn in (evolution._append, positions.write_file,
               rumination._try_ruminate_channel):
        src = inspect.getsource(fn)
        assert "vault_write(" in src, f"{fn.__qualname__} does not write via vault_write"

    # the body lives in _consolidate_session; consolidate_session only takes the lock
    consolidate_src = inspect.getsource(C._consolidate_session)
    tree = ast.parse(consolidate_src.lstrip())
    assert 'mood-patterns.md"' in consolidate_src
    mood_region = consolidate_src[consolidate_src.index('mood-patterns.md"'):]
    assert "vault_write(" in mood_region[:900], (
        "the mood-patterns append no longer writes through vault_write")
    assert tree is not None
