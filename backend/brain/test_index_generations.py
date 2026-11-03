"""A05 slice 4: an interrupted reindex retains a usable committed generation.

Roadmap acceptance: *"interrupted reindex retains a usable committed generation."*

`_index_file_sync` and `_index_skill_sync` deleted the existing points and *then*
upserted the new ones. An upsert that failed in between — Qdrant restarting, disk full,
the process killed during teardown — left the note with **no points at all**: invisible
to semantic recall until the next startup backfill, which on the always-on deployment
can be weeks.

The embedding call already happens *before* the delete, so an embed failure was never
the problem; the window was the two Qdrant round-trips. Reproduced, then fixed by
capturing the previous generation's ids, writing the new one, and retiring the old only
once the new is live.

Deleting captured ids (rather than filtering on a revision field) is deliberate: it
cannot delete points another writer added in the meantime, and it needs no payload
schema change.
"""
import pytest

from backend.brain import embeddings, semindex


class FakeQdrant:
    """Enough of the client to exercise ordering. Points keyed by id."""

    def __init__(self):
        self.points = {}
        self.fail_upsert = False
        self.fail_scroll = False
        self.fail_delete = False
        self.deleted_ids = []

    def upsert(self, collection_name=None, points=None):
        if self.fail_upsert:
            raise RuntimeError("qdrant went away mid-reindex")
        for p in points:
            self.points[p.id] = p

    def delete(self, collection_name=None, points_selector=None):
        """Handles BOTH id-list and filter selectors.

        The filter branch matters even though the fix never uses it: without it, a
        regression back to filter-based delete-first would delete nothing in this fake
        and `test_the_reproduced_incident` would pass spuriously — a test that cannot
        fail. Verified by mutation.
        """
        if self.fail_delete:
            raise RuntimeError("delete failed")
        ids = getattr(points_selector, "points", None)
        if ids is None:
            flt = getattr(points_selector, "filter", None)
            wanted = {c.key: c.match.value for c in getattr(flt, "must", []) or []}
            ids = [pid for pid, p in self.points.items()
                   if all(p.payload.get(k) == v for k, v in wanted.items())]
        for pid in list(ids):
            self.deleted_ids.append(pid)
            self.points.pop(pid, None)

    def scroll(self, collection_name=None, scroll_filter=None, limit=None,
               offset=None, with_payload=False, with_vectors=False):
        if self.fail_scroll:
            raise RuntimeError("scroll failed")
        return [type("P", (), {"id": k, "payload": v.payload})()
                for k, v in self.points.items()], None


@pytest.fixture
def fake(monkeypatch):
    q = FakeQdrant()
    monkeypatch.setattr(semindex, "_qdrant", lambda: q)
    monkeypatch.setattr(semindex, "_ensure_collection", lambda c: True)
    # Mirrors the real signature, including A08's `background` flag, so a call site
    # that stops marking derived work fails loudly instead of silently passing.
    monkeypatch.setattr(embeddings, "_embed_many_sync",
                        lambda chunks, background=False: [[0.1] * 384 for _ in chunks])
    return q


# ------------------------------------------------------------------ vault notes
def test_the_reproduced_incident(fake):
    """An interrupted reindex must not leave the note unindexed."""
    semindex._index_file_sync("topics", "nvidia.md", "# NVIDIA\n\nGood content.\n")
    assert len(fake.points) == 1

    fake.fail_upsert = True
    assert semindex._index_file_sync("topics", "nvidia.md", "# NVIDIA\n\nNew.\n") == 0

    assert len(fake.points) == 1, (
        "the previous generation was deleted before the replacement was live")
    assert "Good content" in next(iter(fake.points.values())).payload["text"]


def test_a_successful_reindex_retires_the_old_generation(fake):
    """The control: no duplicates are left behind on the happy path."""
    semindex._index_file_sync("topics", "n.md", "first body")
    first_ids = set(fake.points)
    semindex._index_file_sync("topics", "n.md", "second body")

    assert len(fake.points) == 1
    assert set(fake.points).isdisjoint(first_ids)
    assert "second body" in next(iter(fake.points.values())).payload["text"]


def test_a_write_arriving_during_the_reindex_is_not_retired(fake):
    """The guarantee the id-capture approach buys over a filter-based retire.

    Only points that existed when the reindex *started* are retired. A filter like
    "delete everything for this note that isn't my revision" would instead delete a
    generation another writer committed while this one was running.

    (A point that was already there before the capture is legitimately part of the
    previous generation and is correctly retired -- that is not this test's case.)
    """
    semindex._index_file_sync("topics", "n.md", "mine v1")

    # another writer commits mid-reindex, i.e. after the capture, before the retire
    original_upsert = fake.upsert

    def upsert_then_interleave(collection_name=None, points=None):
        original_upsert(collection_name=collection_name, points=points)
        fake.points["other-writer"] = type("P", (), {
            "id": "other-writer",
            "payload": {"kind": "vault", "folder": "topics", "filename": "n.md",
                        "text": "theirs"},
        })()

    fake.upsert = upsert_then_interleave
    semindex._index_file_sync("topics", "n.md", "mine v2")

    assert "other-writer" in fake.points, (
        "a generation committed during the reindex was retired along with the old one")
    assert "other-writer" not in fake.deleted_ids


def test_a_failed_retire_leaves_duplicates_not_a_gap(fake):
    """Duplicated is recoverable (recall dedupes per file); deleted is not."""
    semindex._index_file_sync("topics", "n.md", "v1")
    fake.fail_delete = True
    semindex._index_file_sync("topics", "n.md", "v2")

    texts = {p.payload["text"] for p in fake.points.values()}
    assert "v1" in texts and "v2" in texts


def test_an_unlistable_previous_generation_still_writes_the_new_one(fake):
    """If the capture scroll fails, the new generation must still land."""
    semindex._index_file_sync("topics", "n.md", "v1")
    fake.fail_scroll = True
    assert semindex._index_file_sync("topics", "n.md", "v2") == 1
    assert any(p.payload["text"] == "v2" for p in fake.points.values())


def test_emptying_a_note_still_removes_its_chunks(fake):
    """The genuine-delete path is unchanged -- an emptied note should not linger."""
    semindex._index_file_sync("topics", "n.md", "body")
    assert semindex._index_file_sync("topics", "n.md", "   ") == 0


# ---------------------------------------------------------------------- skills
def test_an_interrupted_skill_reindex_keeps_the_old_generation(fake):
    """Skills had the identical delete-then-upsert ordering."""
    semindex._index_skill_sync("research-flow", "---\nstatus: active\n---\n\nStep one.")
    assert len(fake.points) == 1

    fake.fail_upsert = True
    semindex._index_skill_sync("research-flow", "---\nstatus: active\n---\n\nStep two.")

    assert len(fake.points) == 1
    assert "Step one" in next(iter(fake.points.values())).payload["text"]


def test_a_successful_skill_reindex_retires_the_old_generation(fake):
    semindex._index_skill_sync("s", "---\nstatus: active\n---\n\nfirst")
    semindex._index_skill_sync("s", "---\nstatus: active\n---\n\nsecond")
    assert len(fake.points) == 1
    assert "second" in next(iter(fake.points.values())).payload["text"]


# ------------------------------------------------------------------- ordering
@pytest.mark.parametrize("fn_name", ["_index_file_sync", "_index_skill_locked"])
def test_the_capture_precedes_the_upsert_which_precedes_the_retire(fn_name):
    """Static guard on the ordering this slice exists to establish. Reordering these
    silently reintroduces the gap, and only a failing upsert would reveal it."""
    import inspect
    src = inspect.getsource(getattr(semindex, fn_name))
    capture = src.index("previous_ids = _point_ids_sync")
    upsert = src.index("client.upsert(")
    retire = src.index("_delete_point_ids_sync(client, previous_ids)")
    assert capture < upsert < retire, f"{fn_name}: generation ordering is wrong"


def test_the_exchange_path_needs_no_generations():
    """Exchanges are append-only -- each is indexed once under its own exchange_id and
    never re-indexed, so there is no old generation to retire and no window to protect.
    Recorded so a future reader does not 'fix' a path that has no defect."""
    import inspect
    src = inspect.getsource(semindex._index_exchange_sync)
    assert "_delete_point_ids_sync" not in src
    assert "_delete_file_points_sync" not in src
