"""The semantic index reconciles against the vault by content, not by presence.

Backfill used to ask only "is this note indexed at all?". A note first indexed while
small stayed indexed at that size forever as it grew through any writer that skipped
`vault_write`. Measured on the real vault: evolution.md 1 chunk covering 6.6% of the
file, mood-patterns.md 29%. An earlier audit reported "0 stale notes" because its check
asked whether indexed text still appeared in the file -- which a stale prefix always
does. Each chunk now records the hash of the content it came from, and a periodic pass
re-indexes any note whose file no longer matches.
"""
import pytest

from backend.brain import embeddings, semindex


class FilteringQdrant:
    """A fake client whose scroll/delete honour `must` filters, so reconciling several
    notes cannot retire one note's points while re-indexing another."""

    def __init__(self):
        self.points = {}
        self.fail_scroll = False

    @staticmethod
    def _matches(payload, flt):
        conds = getattr(flt, "must", None) or []
        return all(payload.get(c.key) == c.match.value for c in conds)

    def upsert(self, collection_name=None, points=None):
        for p in points:
            self.points[p.id] = p

    def delete(self, collection_name=None, points_selector=None):
        ids = getattr(points_selector, "points", None)
        if ids is None:
            flt = getattr(points_selector, "filter", None)
            ids = [pid for pid, p in self.points.items() if self._matches(p.payload, flt)]
        for pid in list(ids):
            self.points.pop(pid, None)

    def scroll(self, collection_name=None, scroll_filter=None, limit=None,
               offset=None, with_payload=False, with_vectors=False):
        if self.fail_scroll:
            raise RuntimeError("scroll failed")
        hits = [type("P", (), {"id": k, "payload": v.payload})()
                for k, v in self.points.items()
                if scroll_filter is None or self._matches(v.payload, scroll_filter)]
        return hits, None

    def texts(self, folder, filename):
        return sorted(p.payload["text"] for p in self.points.values()
                      if p.payload.get("folder") == folder
                      and p.payload.get("filename") == filename)


@pytest.fixture
def env(tmp_path, monkeypatch):
    q = FilteringQdrant()
    embeds = {"n": 0}

    def fake_embed(chunks, background=False):
        embeds["n"] += 1
        return [[0.1] * 384 for _ in chunks]

    monkeypatch.setattr(semindex, "_qdrant", lambda: q)
    monkeypatch.setattr(semindex, "_ensure_collection", lambda c: True)
    monkeypatch.setattr(embeddings, "_embed_many_sync", fake_embed)
    monkeypatch.setattr(semindex, "_VAULT_ROOT", tmp_path)
    (tmp_path / "self").mkdir()
    (tmp_path / "topics").mkdir()
    return {"q": q, "root": tmp_path, "embeds": embeds}


def write(env, folder, name, text):
    (env["root"] / folder / name).write_text(text, encoding="utf-8")


def test_a_note_that_grew_outside_vault_write_is_reindexed(env):
    """The evolution.md shape: indexed small, then appended to by a raw write."""
    write(env, "self", "evolution.md", "## 2026-05-01\nold entry\n")
    semindex._backfill_sync()
    write(env, "self", "evolution.md",
          "## 2026-05-01\nold entry\n\n## 2026-09-22\nthe roadmap landed\n")

    semindex._backfill_sync()
    assert any("the roadmap landed" in t for t in env["q"].texts("self", "evolution.md"))


def test_an_unchanged_vault_costs_no_embedding(env):
    """The periodic pass must be cheap when nothing moved."""
    write(env, "topics", "camus.md", "# Camus\n\nabsurd\n")
    semindex._backfill_sync()
    before = env["embeds"]["n"]
    assert semindex._backfill_sync() == 0
    assert env["embeds"]["n"] == before


def test_chunks_written_before_hashes_existed_are_reindexed_once(env):
    """Existing deployments hold only hashless chunks. The first pass repairs them;
    the second finds nothing to do."""
    from qdrant_client.models import PointStruct
    write(env, "topics", "nvda.md", "# NVDA\n\nfull current text\n")
    env["q"].upsert(points=[PointStruct(
        id="00000000-0000-0000-0000-000000000001", vector=[0.1] * 384,
        payload={"kind": "vault", "folder": "topics", "filename": "nvda.md",
                 "text": "# NVDA\n\nstale prefix"})])

    assert semindex._backfill_sync() > 0
    assert env["q"].texts("topics", "nvda.md") == ["# NVDA\n\nfull current text"]
    assert semindex._backfill_sync() == 0


def test_two_generations_side_by_side_are_collapsed(env):
    """A failed retire (A05 slice 4) degrades to duplicates. Disagreeing hashes mean
    re-index, which also retires the extra generation."""
    write(env, "topics", "a.md", "v1\n")
    semindex._backfill_sync()
    from qdrant_client.models import PointStruct
    env["q"].upsert(points=[PointStruct(
        id="00000000-0000-0000-0000-0000000000aa", vector=[0.1] * 384,
        payload={"kind": "vault", "folder": "topics", "filename": "a.md",
                 "text": "v0", "content_hash": "somethingelse"})])

    semindex._backfill_sync()
    assert env["q"].texts("topics", "a.md") == ["v1"]


def test_a_missing_note_is_still_indexed(env):
    write(env, "topics", "new.md", "brand new\n")
    semindex._backfill_sync()
    assert env["q"].texts("topics", "new.md") == ["brand new"]


def test_reconciling_one_note_leaves_the_others_alone(env):
    write(env, "topics", "a.md", "alpha\n")
    write(env, "topics", "b.md", "beta\n")
    semindex._backfill_sync()
    write(env, "topics", "a.md", "alpha revised\n")
    semindex._backfill_sync()
    assert env["q"].texts("topics", "a.md") == ["alpha revised"]
    assert env["q"].texts("topics", "b.md") == ["beta"]


def test_a_failed_scan_reindexes_nothing(env):
    """An empty answer from a flaky scroll must not look like 'everything is stale'."""
    write(env, "topics", "a.md", "alpha\n")
    env["q"].fail_scroll = True
    assert semindex._backfill_sync() == 0
    assert env["embeds"]["n"] == 0


def test_the_canary_sentinel_orphan_is_left_alone(env):
    """Indexed on purpose with no file on disk; deleting it could fake a memory alarm."""
    semindex._index_file_sync("self", "canary.md", "canary sentinel")
    semindex._backfill_sync()
    assert env["q"].texts("self", "canary.md") == ["canary sentinel"]


def test_every_written_chunk_carries_the_content_hash(env):
    semindex._index_file_sync("topics", "x.md", "content\n")
    hashes = {p.payload.get("content_hash") for p in env["q"].points.values()}
    assert hashes == {semindex._content_hash("content\n")}


def test_external_skill_edit_is_reconciled(env, monkeypatch):
    """A file edit must replace stale procedural instructions in semantic recall."""
    vault_root = env["root"] / "jarvis-vault"
    skills = vault_root / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setattr(semindex, "_VAULT_ROOT", vault_root)
    # Keep the pre-fix path calculation in this isolated vault too, so this test
    # reproduces the stale-skill failure against the previous implementation.
    monkeypatch.setattr(semindex, "__file__", str(env["root"] / "a" / "b" / "semindex.py"))
    skill = skills / "research-flow.md"
    skill.write_text("---\nstatus: active\n---\n\nSearch, then verify.", encoding="utf-8")

    assert semindex._backfill_skills_sync() == 1
    skill.write_text("---\nstatus: inactive\n---\n\nRetired procedure.", encoding="utf-8")

    assert semindex._backfill_skills_sync() == 1
    points = list(env["q"].points.values())
    assert len(points) == 1
    assert points[0].payload["status"] == "inactive"
    assert points[0].payload["text"].endswith("Retired procedure.")


def test_legacy_skill_without_hash_is_repaired_once(env, monkeypatch):
    from qdrant_client.models import PointStruct

    skills = env["root"] / "skills"
    skills.mkdir()
    monkeypatch.setattr(semindex, "_VAULT_ROOT", env["root"])
    (skills / "old.md").write_text("Current procedure", encoding="utf-8")
    env["q"].upsert(points=[PointStruct(
        id="00000000-0000-0000-0000-0000000000ab", vector=[0.1] * 384,
        payload={"kind": "skill", "skill_name": "old", "text": "Stale procedure"})])

    assert semindex._backfill_skills_sync() == 1
    assert [p.payload["text"] for p in env["q"].points.values()] == ["Current procedure"]
    before = env["embeds"]["n"]
    assert semindex._backfill_skills_sync() == 0
    assert env["embeds"]["n"] == before


def test_failed_skill_scan_does_not_trigger_reindex(env, monkeypatch):
    skills = env["root"] / "skills"
    skills.mkdir()
    monkeypatch.setattr(semindex, "_VAULT_ROOT", env["root"])
    (skills / "a.md").write_text("Procedure", encoding="utf-8")
    env["q"].fail_scroll = True

    assert semindex._backfill_skills_sync() == 0
    assert env["embeds"]["n"] == 0


def test_deleted_skill_is_not_recalled_from_the_index(env, monkeypatch):
    skills = env["root"] / "skills"
    skills.mkdir()
    monkeypatch.setattr(semindex, "_VAULT_ROOT", env["root"])
    skill = skills / "retired.md"
    skill.write_text("---\nstatus: active\n---\n\nDo this old thing.", encoding="utf-8")
    assert semindex._backfill_skills_sync() == 1

    skill.unlink()
    semindex._backfill_skills_sync()
    assert not env["q"].points, "deleted active skill remains searchable"


def test_failed_skill_scan_never_retires_existing_points(env, monkeypatch):
    skills = env["root"] / "skills"
    skills.mkdir()
    monkeypatch.setattr(semindex, "_VAULT_ROOT", env["root"])
    skill = skills / "keep.md"
    skill.write_text("Keep this procedure", encoding="utf-8")
    semindex._backfill_skills_sync()
    skill.unlink()
    env["q"].fail_scroll = True

    semindex._backfill_skills_sync()
    assert len(env["q"].points) == 1


def test_the_reconcile_loop_is_periodic_and_started(monkeypatch):
    import asyncio
    import inspect
    from backend import main

    captured = {}

    async def fake_run_periodic(*, name, check_interval_s, on_cycle):
        captured.update(name=name, interval=check_interval_s)

    monkeypatch.setattr("backend.brain.pacing.run_periodic", fake_run_periodic)
    asyncio.run(semindex.run_reconcile_loop())
    assert captured == {"name": "semindex_reconcile",
                        "interval": semindex._RECONCILE_INTERVAL_S}
    assert "semindex.run_reconcile_loop" in inspect.getsource(main)
