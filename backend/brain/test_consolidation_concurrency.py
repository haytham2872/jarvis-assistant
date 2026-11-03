"""Consolidation must not overwrite what the live session wrote while it was thinking.

Consolidation reads the portrait and the queue, spends 17-41s in an LLM call, then
rewrites both in full. It runs as a background task during the first turn of the NEXT
session (`engine.py`), which is exactly when JARVIS tends to update the queue.
Reproduced: a queue edit that reported "Confirmed written ... verified" was overwritten
0.4s later. A05 slice 1 made it worse across restarts -- a failed write is retried by
replaying the saved proposal verbatim, so a snapshot days old lands on today's file.

A05 slice 3's claim that research held "exactly one dangerous window" came from a scan
that looked for read -> await -> write inside a single function. This window spans two.

The first reproduction of this bug was itself flawed in an instructive way: it
redirected `vault._VAULT` to a temp dir, but `vault._QUEUE_FILE` was bound at import,
so `vault_write_queue` wrote the developer's REAL queue.md. The queue path now resolves
from `_VAULT` at call time, and the first test here pins that.
"""
import asyncio

import pytest

from backend.brain import consolidation as C
from backend.storage import consolidation_state as cstate, db


def run(coro):
    return asyncio.run(coro)


QUEUE_V1 = ("[open] 2026-09-20 -- finish the NVDA note\n"
            "[open] 2026-09-19 -- reread the Frankl chapter\n")
LIVE_LINE = "[open] 2026-09-22 -- Sam asked me to track the Fed decision"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()

    vault = tmp_path / "vault"
    for d in ("letters", "people", "topics", "self", "creative",
              "conversations/significant"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(C, "_VAULT", vault)

    import backend.tools.vault as V
    monkeypatch.setattr(V, "_VAULT", vault)
    monkeypatch.setattr(V, "_QUEUE_FILE", None)       # derive from _VAULT, as in production
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)

    (vault / "queue.md").write_text(QUEUE_V1, encoding="utf-8")
    (vault / "people" / "owner.md").write_text("# Sam\n\nportrait v1\n", encoding="utf-8")

    state = {"during": None, "proposal": {"letter": "a letter"}, "calls": 0}

    async def slow_generate(threads, prior_letter, owner_md, queue_raw, *a, **k):
        state["calls"] += 1
        state["seen_queue"] = queue_raw
        await asyncio.sleep(0.2)                      # the LLM call
        if state["during"]:
            await state["during"]()                   # the live session acts meanwhile
        return dict(state["proposal"])

    async def fake_threads(_sid):
        return [{"user": "hi", "assistant": "hello", "timestamp": 1.0}]

    async def none():
        return None

    monkeypatch.setattr(C, "_generate_consolidation", slow_generate)
    monkeypatch.setattr(C, "threads_for_session", fake_threads)
    monkeypatch.setattr(C, "latest_sleep", none)
    monkeypatch.setattr(C, "_snapshot_unconsolidated_ids_sync", lambda _u: ["f1"])
    marked: list = []
    monkeypatch.setattr(C, "_mark_facts_consolidated_sync", lambda ids: marked.extend(ids))

    yield {"vault": vault, "V": V, "state": state, "marked": marked}
    db._reset_for_tests()


def queue(env):
    return (env["vault"] / "queue.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the isolation hole
def test_the_queue_path_follows_the_vault_root(env):
    """The defect behind the flawed first repro: an import-time path ignored `_VAULT`."""
    assert env["V"]._queue_file() == env["vault"] / "queue.md"
    run(env["V"].vault_write_queue("x\n"))
    assert queue(env) == "x\n"


def test_an_explicit_queue_override_still_wins(env, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.md"
    monkeypatch.setattr(env["V"], "_QUEUE_FILE", other)
    run(env["V"].vault_write_queue("y\n"))
    assert other.read_text(encoding="utf-8") == "y\n"


# ---------------------------------------------------------------- the reproduced race
def test_a_queue_edit_made_during_consolidation_survives(env):
    V, state = env["V"], env["state"]
    state["proposal"] = {"letter": "a letter", "queue_update": QUEUE_V1}   # "keep items"

    async def live_turn():
        r = await V.vault_write_queue(QUEUE_V1 + LIVE_LINE + "\n")
        assert r.startswith("Confirmed written")
    state["during"] = live_turn

    run(C.consolidate_session("sess-race-000001"))
    assert LIVE_LINE in queue(env), "the live queue edit was overwritten"


def test_consolidations_own_changes_still_land_after_a_merge(env):
    """Merging must not mean 'the live side wins everything' -- consolidation resolving
    one item and adding another are real work."""
    V, state = env["V"], env["state"]
    new_item = "[open] 2026-09-21 -- follow up on the backup cron"
    state["proposal"] = {
        "letter": "a letter",
        "queue_update": ("[resolved] 2026-09-20 -- finish the NVDA note\n"
                         "[open] 2026-09-19 -- reread the Frankl chapter\n"
                         f"{new_item}\n"),
    }

    async def live_turn():
        await V.vault_write_queue(QUEUE_V1 + LIVE_LINE + "\n")
    state["during"] = live_turn

    run(C.consolidate_session("sess-race-000002"))
    q = queue(env)
    assert LIVE_LINE in q
    assert new_item in q
    assert "[resolved] 2026-09-20 -- finish the NVDA note" in q
    assert "[open] 2026-09-20 -- finish the NVDA note" not in q, "resolution was lost"


def test_without_a_concurrent_edit_the_rewrite_is_written_verbatim(env):
    """No conflict, no merge: the model's rewrite keeps its own order and structure."""
    rewrite = ("## Open\n[open] 2026-09-19 -- reread the Frankl chapter\n\n"
               "## Done\n[resolved] 2026-09-20 -- finish the NVDA note\n")
    env["state"]["proposal"] = {"letter": "a letter", "queue_update": rewrite}
    run(C.consolidate_session("sess-race-000003"))
    assert queue(env) == rewrite


def test_a_portrait_edited_during_consolidation_is_not_overwritten(env):
    V, state = env["V"], env["state"]
    state["proposal"] = {"letter": "a letter", "owner_update": "# Sam\n\nstale rewrite\n"}

    async def live_turn():
        await V.vault_write("owner.md", "# Sam\n\nedited live\n", "people")
    state["during"] = live_turn

    sid = "sess-race-000004"
    run(C.consolidate_session(sid))
    text = (env["vault"] / "people" / "owner.md").read_text(encoding="utf-8")
    assert "edited live" in text


def test_a_superseded_output_is_terminal_so_facts_still_get_marked(env):
    """If a refused write were a retryable failure, the same proposal would be refused
    forever and this session's facts could never be marked."""
    V, state = env["V"], env["state"]
    state["proposal"] = {"letter": "a letter", "owner_update": "# Sam\n\nstale\n"}

    async def live_turn():
        await V.vault_write("owner.md", "# Sam\n\nedited live\n", "people")
    state["during"] = live_turn

    sid = "sess-race-000005"
    run(C.consolidate_session(sid))
    assert "owner" in cstate.completed_outputs(sid)
    assert env["marked"] == ["f1"]
    assert cstate.is_fully_done(sid)


# ---------------------------------------------------------------- replay across restarts
def test_replaying_a_saved_proposal_after_the_queue_moved_merges_instead(env):
    """The A05 slice 1 amplification: a retry reuses the proposal verbatim, possibly
    days later. Its preconditions must be the generation-time ones."""
    V, state = env["V"], env["state"]
    sid = "sess-race-000006"
    state["proposal"] = {"letter": "a letter", "queue_update": QUEUE_V1}

    # first attempt: the queue write fails for an unrelated reason (disk full)
    real_write_queue = V.vault_write_queue

    async def failing(content, expected_revision=None):
        return "Error: queue.md write did not verify on read-back"
    V.vault_write_queue = failing
    try:
        run(C.consolidate_session(sid))
    finally:
        V.vault_write_queue = real_write_queue
    assert "queue" not in cstate.completed_outputs(sid)

    # days pass; the live session edits the queue
    run(V.vault_write_queue(QUEUE_V1 + LIVE_LINE + "\n"))

    # the retry replays the saved proposal
    state["during"] = None
    run(C.consolidate_session(sid))
    assert state["calls"] == 1, "the retry regenerated instead of reusing the proposal"
    assert LIVE_LINE in queue(env), "a replayed stale proposal overwrote the live queue"


def test_a_legacy_proposal_without_base_state_is_applied_as_before(env):
    """Proposals saved before this change carry no generation-time state; there is
    nothing to check them against, so they keep their previous behaviour."""
    sid = "sess-race-000007"
    cstate.save_proposal(sid, {"letter": "a letter", "queue_update": "legacy\n"})
    run(C.consolidate_session(sid))
    assert queue(env) == "legacy\n"


# ---------------------------------------------------------------- positions
def test_positions_patch_applies_to_the_current_file_not_the_stale_read(env, monkeypatch):
    V, state = env["V"], env["state"]
    pos = env["vault"] / "self" / "positions.md"
    pos.write_text("# Positions\n\n## Camus\n**Stance:** old view.\n", encoding="utf-8")
    state["proposal"] = {"letter": "a letter", "positions_patch": [
        {"topic": "Frankl", "action": "add", "stance": "Meaning is found, not made.",
         "sources": [], "revision_note": ""}]}

    async def live_turn():
        await V.vault_write("positions.md",
                            "# Positions\n\n## Camus\n**Stance:** revised live.\n", "self")
    state["during"] = live_turn

    run(C.consolidate_session("sess-race-000008"))
    text = pos.read_text(encoding="utf-8")
    assert "revised live" in text, "positions patch reverted a concurrent edit"
    assert "Frankl" in text


# ---------------------------------------------------------------- the merge itself
def test_merge_keeps_their_additions_and_our_changes():
    base = "a\nb\nc\n"
    ours = "a\nB\nc\nd\n"            # changed b -> B, added d
    theirs = "a\nb\nc\ne\n"          # added e
    # d follows its predecessor c in the rewrite; their e keeps its place after it
    assert C.merge_queue(base, ours, theirs) == "a\nB\nc\nd\ne\n"


def test_merge_inserts_additions_after_their_predecessor_in_order():
    base = "## Open\nx\n## Done\n"
    ours = "## Open\nx\ny1\ny2\n## Done\n"
    theirs = "## Open\nx\n## Done\nz\n"
    assert C.merge_queue(base, ours, theirs) == "## Open\nx\ny1\ny2\n## Done\nz\n"


def test_merge_does_not_resurrect_what_they_removed():
    assert C.merge_queue("a\nb\n", "a\nb\n", "a\n") == "a\n"


def test_a_line_changed_on_both_sides_keeps_both_versions():
    merged = C.merge_queue("[open] x\n", "[resolved] x\n", "[in_progress] x\n")
    assert "[resolved] x" in merged and "[in_progress] x" in merged


def test_queue_writer_refuses_a_stale_precondition(env):
    V = env["V"]
    rev = V.queue_revision()
    run(V.vault_write_queue("changed\n"))
    result = run(V.vault_write_queue("stale\n", expected_revision=rev))
    assert C._is_superseded(result)
    assert queue(env) == "changed\n"
