"""G1b / D8: drafts/ and shared/ are indexed when written but were never reconciled.

`vault_write` indexes every folder, so a background job's result note in drafts/ is in
recall from the moment it is written -- but the reconcile pass skipped drafts/ and shared/,
so an edited draft stayed indexed as first written, and a deleted one stayed in recall for
good. Decision (docs/archive/GOALS-ROADMAP.md section 10, D8): reconcile both folders, remove orphans in
those two folders only (the memory canary's index-only sentinel lives in self/ and must
stay), and date the job notes so a recalled finding shows its age.
"""
import asyncio
import json

from backend.brain import background as B
from backend.brain import semindex
from backend.brain.test_index_reconcile import env, write  # noqa: F401 (fixture)


def test_an_edited_draft_is_reindexed(env):
    (env["root"] / "drafts").mkdir()
    write(env, "drafts", "bg-1-etf.md", "first finding: TER 0.20%")
    semindex._backfill_sync()
    write(env, "drafts", "bg-1-etf.md", "corrected finding: TER 0.07%")
    semindex._backfill_sync()
    texts = env["q"].texts("drafts", "bg-1-etf.md")
    assert texts and all("0.07%" in t for t in texts)


def test_a_deleted_draft_leaves_recall_but_the_canary_sentinel_stays(env):
    (env["root"] / "drafts").mkdir()
    (env["root"] / "shared").mkdir()
    write(env, "drafts", "bg-2-old.md", "an old result")
    write(env, "shared", "s.md", "shared note")
    # indexed on write, as vault_write does for every folder
    semindex._index_file_sync("drafts", "bg-2-old.md", "an old result")
    semindex._index_file_sync("shared", "s.md", "shared note")
    assert env["q"].texts("drafts", "bg-2-old.md")
    semindex._index_file_sync("self", "canary.md", "canary sentinel, index-only")
    (env["root"] / "drafts" / "bg-2-old.md").unlink()
    (env["root"] / "shared" / "s.md").unlink()
    semindex._backfill_sync()
    assert env["q"].texts("drafts", "bg-2-old.md") == []
    assert env["q"].texts("shared", "s.md") == []
    assert env["q"].texts("self", "canary.md")            # protected: never on disk


def test_a_goal_folder_is_never_indexed_by_reconcile(env):
    (env["root"] / "goals" / "g1-x").mkdir(parents=True)
    write(env, "goals/g1-x", "result.md", "unfinished goal draft")
    semindex._backfill_sync()
    assert not any(p.payload.get("folder", "").startswith("goals")
                   for p in env["q"].points.values())


def test_a_job_result_note_carries_its_date(monkeypatch):
    written = {}

    async def vault_write(filename, content, folder):
        written[filename] = content
        return "Confirmed"
    monkeypatch.setattr("backend.tools.vault.vault_write", vault_write)
    t = B.BgTask(id=5, description="compare ETFs", status="done", result="done it")
    asyncio.run(B._write_result_note(t))
    [content] = written.values()
    assert "**Date:**" in content and "(local)" in content
