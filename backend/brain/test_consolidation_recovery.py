"""A05 slice 1: a partially-failed consolidation is recoverable.

Roadmap acceptance, verbatim: *"Fail one consolidation write, restart, and complete only
missing work without duplicate append artifacts; uncovered facts remain pending."*

All four clauses failed before this. Reproduced end to end: with one vault write failing,
the failure surfaced nowhere (the gather's results were discarded, and `vault_write`
signals failure by *returning* a string rather than raising), the facts were marked
consolidated anyway — removing from `recall()` the raw material the lost note would have
been rebuilt from — and a restart with a healthy disk skipped everything because a letter
file existed. One transient write failure destroyed that session's portrait update
permanently.

`test_the_full_acceptance_criterion` is that scenario, kept as the regression test.
"""
import asyncio
import pathlib

import pytest

from backend.brain import consolidation as C
from backend.storage import consolidation_state as cstate, db


def run(coro):
    return asyncio.run(coro)


PROPOSAL = {
    "letter": "Dear future me, today mattered.",
    "owner_update": "# Sam\n\nA REWRITTEN PORTRAIT THAT MUST NOT BE LOST.",
    "mood_patterns_update": "He gets terse when tired.",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real consolidate_session, isolated vault + DB, no LLM, no Qdrant, no indexing."""
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()

    vault = tmp_path / "vault"
    for d in ("letters", "people", "topics", "self", "creative",
              "conversations/significant"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(C, "_VAULT", vault)

    marked: list[str] = []

    async def fake_generate(*a, **k):
        return dict(PROPOSAL)

    async def fake_threads(_sid):
        return [{"user": "hi", "assistant": "hello", "timestamp": 1.0}]

    async def fake_sleep():
        return None

    monkeypatch.setattr(C, "_generate_consolidation", fake_generate)
    monkeypatch.setattr(C, "threads_for_session", fake_threads)
    monkeypatch.setattr(C, "latest_sleep", fake_sleep)
    monkeypatch.setattr(C, "_snapshot_unconsolidated_ids_sync", lambda _u: ["f1", "f2"])
    monkeypatch.setattr(C, "_mark_facts_consolidated_sync", lambda ids: marked.extend(ids))

    import backend.tools.vault as V
    monkeypatch.setattr(V, "_VAULT", vault)
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)

    yield {"vault": vault, "marked": marked, "V": V}
    db._reset_for_tests()


def _fail_folder(monkeypatch, env, folder):
    """Make writes to one vault folder fail the way vault_write really does: by
    RETURNING an error string, not raising."""
    V = env["V"]
    real = V.vault_write

    async def flaky(filename, content, fold, expected_revision=None):
        if fold == folder:
            return "Error writing to vault: disk full"
        return await real(filename, content, fold, expected_revision=expected_revision)

    monkeypatch.setattr(V, "vault_write", flaky)
    return real


SID = "sess-abcdefgh-1111"


def test_the_full_acceptance_criterion(env, monkeypatch):
    """Fail one write, restart, complete only the missing work, no duplicates, facts
    stay pending until it is genuinely finished."""
    vault, marked = env["vault"], env["marked"]
    real_write = _fail_folder(monkeypatch, env, "people")

    run(C.consolidate_session(SID))

    assert list((vault / "letters").iterdir()), "the letter should still have landed"
    assert not list((vault / "people").iterdir()), "the portrait write was supposed to fail"
    assert marked == [], "facts were marked while an output was still unwritten"

    # restart, healthy disk
    monkeypatch.setattr(env["V"], "vault_write", real_write)
    run(C.consolidate_session(SID))

    assert list((vault / "people").iterdir()), "the restart did not complete missing work"
    assert len(list((vault / "letters").iterdir())) == 1, "the letter was duplicated"
    mp = (vault / "self" / "mood-patterns.md").read_text(encoding="utf-8")
    assert mp.count("He gets terse when tired.") == 1, "duplicate append artifact"
    assert marked == ["f1", "f2"], "facts were never marked after a successful finish"


def test_a_failed_write_is_logged_not_swallowed(env, monkeypatch, caplog):
    """The failure surfaced nowhere before -- the gather's results were discarded."""
    _fail_folder(monkeypatch, env, "people")
    with caplog.at_level("WARNING"):
        run(C.consolidate_session(SID))
    text = caplog.text
    assert "owner" in text and "FAILED" in text


def test_the_proposal_is_saved_before_anything_is_applied(env, monkeypatch):
    """Recovery must reuse the same content: regenerating costs another LLM call and
    yields a different session summary, so the letter on disk and the portrait written
    on the retry would describe the session differently."""
    _fail_folder(monkeypatch, env, "people")
    run(C.consolidate_session(SID))

    saved = cstate.load_proposal(SID)
    assert saved is not None
    assert saved["owner_update"] == PROPOSAL["owner_update"]


def test_the_retry_does_not_call_the_model_again(env, monkeypatch):
    calls = {"n": 0}

    async def counting_generate(*a, **k):
        calls["n"] += 1
        return dict(PROPOSAL)

    monkeypatch.setattr(C, "_generate_consolidation", counting_generate)
    real_write = _fail_folder(monkeypatch, env, "people")

    run(C.consolidate_session(SID))
    assert calls["n"] == 1

    monkeypatch.setattr(env["V"], "vault_write", real_write)
    run(C.consolidate_session(SID))
    assert calls["n"] == 1, "the retry regenerated the proposal instead of reusing it"


def test_a_fully_completed_session_is_not_redone(env):
    vault, marked = env["vault"], env["marked"]
    run(C.consolidate_session(SID))
    assert marked == ["f1", "f2"]

    marked.clear()
    run(C.consolidate_session(SID))
    assert marked == [], "a completed session was consolidated a second time"
    assert len(list((vault / "letters").iterdir())) == 1
    mp = (vault / "self" / "mood-patterns.md").read_text(encoding="utf-8")
    assert mp.count("He gets terse when tired.") == 1


def test_per_output_state_records_what_landed(env, monkeypatch):
    _fail_folder(monkeypatch, env, "people")
    run(C.consolidate_session(SID))

    done = cstate.completed_outputs(SID)
    failed = cstate.failed_outputs(SID)
    assert "letter" in done and "mood_patterns" in done
    assert "owner" in failed
    assert "disk full" in failed["owner"]


def test_legacy_sessions_with_a_letter_are_not_redone(env):
    """Sessions consolidated BEFORE this feature have no saved proposal. Redoing them
    would duplicate the mood-patterns append and rewrite the portrait from a fresh LLM
    call, so the old letter-exists test is kept for exactly that case."""
    vault, marked = env["vault"], env["marked"]
    (vault / "letters" / f"2020-01-01_{SID[:8]}.md").write_text("old", encoding="utf-8")

    run(C.consolidate_session(SID))
    assert marked == []
    assert not list((vault / "people").iterdir())


def test_everything_still_works_when_storage_is_unavailable(env, monkeypatch):
    """Silent degradation: with no database, consolidation must behave as it did before
    this feature rather than failing."""
    monkeypatch.setattr(db, "connect", lambda: None)
    vault, marked = env["vault"], env["marked"]

    run(C.consolidate_session(SID))

    assert list((vault / "letters").iterdir())
    assert list((vault / "people").iterdir())
    assert marked == ["f1", "f2"]


# ------------------------------------------------------------- the state module
def test_saving_a_proposal_twice_keeps_the_first(tmp_path, monkeypatch):
    """The first proposal is the one partially applied; replacing it would strand
    whatever already landed on disk."""
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "s.db"))
    db._reset_for_tests()
    cstate.save_proposal("s1", {"letter": "first"})
    cstate.save_proposal("s1", {"letter": "second"})
    assert cstate.load_proposal("s1") == {"letter": "first"}
    db._reset_for_tests()


def test_is_fully_done_requires_the_facts_step(tmp_path, monkeypatch):
    """A run whose vault writes all landed but whose fact marking never ran is not
    finished, and its facts must stay recallable until it is."""
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "s.db"))
    db._reset_for_tests()
    cstate.save_proposal("s2", {"letter": "x"})
    cstate.record_output("s2", "letter", True)
    assert cstate.is_fully_done("s2") is False
    cstate.mark_facts_done("s2")
    assert cstate.is_fully_done("s2") is True
    db._reset_for_tests()


def test_a_retried_output_flips_from_failed_to_done(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "s.db"))
    db._reset_for_tests()
    cstate.record_output("s3", "owner", False, "disk full")
    assert "owner" in cstate.failed_outputs("s3")
    cstate.record_output("s3", "owner", True, "Confirmed written")
    assert cstate.failed_outputs("s3") == {}
    assert "owner" in cstate.completed_outputs("s3")
    db._reset_for_tests()


def test_state_functions_degrade_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert cstate.save_proposal("s", {}) is False
    assert cstate.load_proposal("s") is None
    assert cstate.completed_outputs("s") == set()
    assert cstate.failed_outputs("s") == {}
    assert cstate.facts_marked("s") is False
    assert cstate.is_fully_done("s") is False
    cstate.record_output("s", "letter", True)      # must not raise
    cstate.mark_facts_done("s")                    # must not raise


@pytest.mark.parametrize("result,ok", [
    ("Confirmed written to jarvis-vault/people/owner.md (42 chars)", True),
    ("Error writing to vault: disk full", False),
    (RuntimeError("boom"), False),
])
def test_write_landed_classifies_every_failure_shape(result, ok):
    """Three distinct ways a write can fail, and the old code caught none of them."""
    got, _detail = C._write_landed("owner", result)
    assert got is ok


# ------------------------------------------------- the mem0 collection resolution bug
# Bound at import so these exercise the REAL function: conftest's autouse guard replaces
# the module attribute, not this reference.
from backend.brain.memory import _find_mem0_collection as _real_find_mem0   # noqa: E402


class _FakeCollections:
    def __init__(self, names):
        self.collections = [type("C", (), {"name": n})() for n in names]


def _fake_qdrant(monkeypatch, names):
    import qdrant_client

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_collections(self):
            return _FakeCollections(names)

    monkeypatch.setattr(qdrant_client, "QdrantClient", _FakeClient)


def test_the_fact_collection_is_not_a_mem0_bookkeeping_table(monkeypatch):
    """The live bug, in the order Qdrant actually returned them.

    `mem0migrations` (migration state, empty) and `mem0_entities` (the entity graph)
    both start with "mem0", and the old "first startswith" logic picked the first one
    listed. Measured live: `_get_consolidated_ids()` returned 0 while 1,118 facts were
    genuinely marked consolidated -- so recall could exclude none of them, and
    consolidation's own marking wrote to an empty table.
    """
    _fake_qdrant(monkeypatch, ["mem0migrations", "mem0_entities", "mem0"])
    assert _real_find_mem0() == "mem0"


@pytest.mark.parametrize("names", [
    ["mem0"],
    ["mem0", "mem0migrations"],
    ["jarvis_threads", "mem0migrations", "mem0_entities", "mem0", "jarvis_mood"],
])
def test_the_exact_collection_wins_regardless_of_listing_order(monkeypatch, names):
    _fake_qdrant(monkeypatch, names)
    assert _real_find_mem0() == "mem0"


def test_an_unusually_named_deployment_still_resolves(monkeypatch):
    """The prefix scan is kept as a fallback so a renamed collection resolves rather
    than returning None -- but it must still skip the known internals."""
    _fake_qdrant(monkeypatch, ["mem0migrations", "mem0_entities", "mem0_sam"])
    assert _real_find_mem0() == "mem0_sam"


def test_only_internals_present_resolves_to_nothing(monkeypatch):
    """Better None (every caller degrades) than silently operating on the wrong table."""
    _fake_qdrant(monkeypatch, ["mem0migrations", "mem0_entities"])
    assert _real_find_mem0() is None
