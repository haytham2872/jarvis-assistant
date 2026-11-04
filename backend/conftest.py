"""Suite-wide safety rails.

**Why this file exists.** A02 added a SQLite mirror inside `threads.record_thread()`.
Every pre-existing test that exercised `record_thread` — with Qdrant mocked, so they
looked perfectly isolated — immediately began writing into the developer's *real*
`.jarvis_runtime.db`. Eight rows of fixture text ("hi"/"hello", "user says X") were found
in it after one suite run.

Per-test fixtures cannot fix that class of problem: the danger is precisely the tests
that do not know they now touch storage. So the redirect is autouse — a test has to opt
*out* to reach real state, rather than remembering to opt in.

**Function-scoped, not session-scoped.** The first version shared one temporary database
for the whole run, which was worse than it sounds: rows written by one test leaked into
the next, and four `test_threads_session.py` cases broke because `_last_exchange()` found
a real row instead of falling through to their mocked `recent_threads`. Cross-test
contamination through a shared datastore is the same hazard as contaminating the real
one, just harder to notice. Each test now gets its own file.

The same reasoning applies to any future durable store added below a widely-called
function. Add it here rather than to the tests that happen to notice.
"""
import os
from pathlib import Path

# The test owner and time zone are set in the repository's root conftest.py, before
# `backend` is imported.

import pytest


def pytest_xdist_auto_num_workers(config):
    """`-n auto` (pytest.ini): every core for the suite or a folder, one process for a
    single test file or test, where starting 8 workers costs more than it saves, and one
    process with --testmon, which selects tests in a single process."""
    if config.getoption("testmon", default=False):
        return 0
    args = [a for a in config.args if a]
    if args and all(a.split("::")[0].endswith(".py") for a in args):
        return 0
    return None


@pytest.fixture(autouse=True)
def _isolated_runtime_database(tmp_path, monkeypatch):
    """Point the SQLite runtime store at this test's own throwaway file.

    Individual tests that set `JARVIS_DB_PATH` themselves still work — monkeypatch
    applies theirs after this, and `db._reset_for_tests()` in their own fixture reopens
    against it. This is only the floor.
    """
    from backend.storage import db

    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "runtime.db"))
    # Production syncs FULL (G2, N12); the suite does not test power-loss durability and
    # makes thousands of commits, so it runs NORMAL.
    monkeypatch.setenv("JARVIS_DB_SYNCHRONOUS", "NORMAL")
    db._reset_for_tests()
    try:
        yield
    finally:
        # Close before pytest removes tmp_path: on Windows an open handle makes the
        # cleanup a hard PermissionError, and connections opened inside executor threads
        # are exactly the ones a per-thread close would miss.
        db._reset_for_tests()


@pytest.fixture(autouse=True)
def _block_real_semantic_index(monkeypatch):
    """Stop the test suite writing into the real Qdrant semantic index.

    **This was a live bug, not a hypothetical.** Tests monkeypatch `vault._VAULT` to a
    tmp_path, which correctly isolates the FILES -- but `vault_write()` also fires
    `_index_note_async()`, which indexes into whatever Qdrant `semindex._qdrant()`
    returns: the developer's real `jarvis_semantic`. And `index_file()` deletes the
    prior points for a `(folder, filename)` before upserting, so a fixture writing
    `self/positions.md` or `people/predictions.md` REPLACED the real note's index
    entries with test content.

    Found 2026-09-22: `self/positions.md`, `people/predictions.md` and
    `self/ruminations.md` each held exactly ONE indexed point containing fixture text
    ("nietzsche", "calibration right=0", "1970-01-01 session sess-1"), where the real
    files chunk to 7, 12 and 6 points. Semantic recall for all three was returning test
    junk. The files on disk were never touched -- only the index -- and re-indexing from
    disk repaired it.

    `semindex` already degrades silently when Qdrant is unavailable, so returning None
    makes every index write a no-op. Tests that genuinely exercise indexing patch
    `semindex._qdrant` themselves and are unaffected, because their patch is applied
    after this one.
    """
    from backend.brain import semindex

    monkeypatch.setattr(semindex, "_qdrant", lambda: None)
    yield


@pytest.fixture(autouse=True)
def _block_real_mem0_collection(monkeypatch):
    """Stop the test suite reaching the real mem0 fact collection.

    The sibling guard above covers `semindex`, but mem0's own paths build a
    `QdrantClient` directly via `memory._find_mem0_collection()` and never go through
    `semindex._qdrant`, so they were not covered by it.

    **This caught a real one.** `consolidation.consolidate_session()` calls
    `memory.dedup_facts()` on its success path, and `_dedup_facts_sync` **deletes**
    near-duplicate points from the live collection. Tests exercising a successful
    consolidation were therefore running a destructive operation against the
    developer's real memory (it happened to delete nothing -- the guard clause
    `len(facts) < 2` fired -- but nothing about the test guaranteed that), and were
    slow for the same reason: ~5s per test of real Qdrant round-trips.

    Returning None makes every one of those call sites degrade to a no-op, which each
    already handles. Tests that genuinely exercise collection resolution patch this
    themselves afterwards and are unaffected.
    """
    from backend.brain import memory

    monkeypatch.setattr(memory, "_find_mem0_collection", lambda: None)
    yield


@pytest.fixture(autouse=True)
def _isolated_skill_nightly_state(tmp_path, monkeypatch):
    """The skills' overnight pass keeps its state at the project root (seen repeats, routine
    offers, the morning report). Every test gets its own file: the brief and the Sunday
    review read it, and a test writing the real one would leave offers for the next run."""
    from backend.brain import skill_nightly
    monkeypatch.setattr(skill_nightly, "_STATE_FILE", tmp_path / "skills_nightly.json")
    yield


@pytest.fixture(autouse=True)
def _isolated_history_refill_state(tmp_path, monkeypatch):
    """/new records its time in .jarvis_history.json at the project root, so a restart does
    not bring back the conversation he ended. Every test gets its own file."""
    from backend.brain import history_refill
    monkeypatch.setattr(history_refill, "_STATE_FILE", tmp_path / "history_refill.json")
    yield


@pytest.fixture(autouse=True)
def _no_real_reranker(monkeypatch):
    """Keep the passive-recall cross-encoder out of the suite.

    The recall paths opt into reranking, so without this every memory/recall test would
    load a real 88MB model (~14s) and score with it. Disabled, `semindex` returns
    exactly the old cosine answer, which is what those tests assert. Tests of the
    reranker itself stub `rerank._get_model` and re-enable it.
    """
    monkeypatch.setenv("JARVIS_RERANK", "0")
    yield


@pytest.fixture(autouse=True)
def _no_real_jev(monkeypatch):
    """Keep Jev (the per-turn classifier) off the network in the suite.

    chat() starts a Jev call on every turn when an OpenRouter key is present -- and the
    developer's .env has one. Disabled, turn_signals.start() returns None and every
    decision uses the keyword/embedding fallback, which is what the existing tests assert.
    Tests of turn_signals itself fake the HTTP client and re-enable it.
    """
    monkeypatch.setenv("JARVIS_JEV", "0")
    yield


@pytest.fixture(autouse=True)
def _no_real_tinyfish(monkeypatch):
    """Keep web search and page reads off TinyFish's real API in the suite.

    The developer's .env holds a TinyFish key, and web_browse/fetch_page try TinyFish
    first whenever one is set. The key is read at call time, so unsetting it makes every
    call skip straight to the fallbacks. Tests of the TinyFish path set a fake key and
    fake the HTTP client; the pause state is reset so one test's failure can't pause the
    next.
    """
    from backend.tools import tinyfish

    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.setattr(tinyfish, "_pause_until", {"search": 0.0, "fetch": 0.0})
    yield


@pytest.fixture(autouse=True)
def _guard_real_database(_isolated_runtime_database):
    """Fail loudly if a test resolves the real database path.

    Takes `_isolated_runtime_database` as an argument purely for ordering: both are
    autouse, and without the explicit dependency this can run first and trip on the real
    path before the redirect has been applied.
    """
    from backend.storage import db

    resolved = Path(str(db.db_path()))
    assert resolved.name != ".jarvis_runtime.db", (
        f"test would use the REAL runtime database at {resolved}"
    )
    yield


# ---------------------------------------------------------------------------
# The real vault
#
# The other rails here block stores reached *indirectly* (SQLite, the semantic index,
# mem0). The vault had none, and it was reached the same way: a probe redirected
# `vault._VAULT` to a temp dir while `vault._QUEUE_FILE` stayed bound to the real path
# from import time, and overwrote the developer's real queue.md. That hole is fixed at
# source (the path now resolves from `_VAULT` at call time); this makes the next one
# fail the offending test by name instead of being found afterwards.
#
# A stat fingerprint rather than a content hash, so it stays cheap across ~1,500 tests.
# `.obsidian/` is excluded: an open Obsidian rewrites its workspace file on its own.
# A backend running locally during the suite would also trip this -- stop it first.
# ---------------------------------------------------------------------------

_REAL_VAULT = Path(__file__).resolve().parent.parent / "jarvis-vault"


def _vault_fingerprint(root: Path | None = None) -> dict | None:
    """(size, mtime_ns) per file. os.scandir, not Path.rglob + stat(): on Windows a
    DirEntry's stat comes from the directory listing itself, where Path.stat() is a
    syscall per file -- measured at ~2.5x the whole suite's runtime with rglob."""
    root = root if root is not None else _REAL_VAULT
    if not root.exists():
        return None
    out: dict = {}
    stack = [(str(root), "")]
    while stack:
        path, rel = stack.pop()
        try:
            with os.scandir(path) as it:
                for e in it:
                    name = f"{rel}{e.name}"
                    if e.is_dir(follow_symlinks=False):
                        if name != ".obsidian":
                            stack.append((e.path, name + "/"))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        out[name] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return out


@pytest.fixture(autouse=True)
def _guard_real_vault():
    # Bound at setup: other autouse fixtures hold a monkeypatch that is torn down AFTER
    # this one, so re-reading the module global here could see a patched stand-in.
    root = _REAL_VAULT
    before = _vault_fingerprint(root)
    yield
    after = _vault_fingerprint(root)
    # A missing vault and an empty one hold the same files. On a fresh clone there is no
    # jarvis-vault/ yet, and building a prompt creates its empty folders.
    before, after = before or {}, after or {}
    if before == after:
        return
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    created = sorted(after.keys() - before.keys())
    deleted = sorted(before.keys() - after.keys())
    pytest.fail(
        "test touched the REAL jarvis-vault/ -- "
        f"modified={changed[:10]} created={created[:10]} deleted={deleted[:10]}")
