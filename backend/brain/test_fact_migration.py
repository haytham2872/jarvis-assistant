"""A05 slice 5: old facts survive an embedding migration — and stay visibly unusable.

Roadmap acceptance: *"old facts survive embedding migration rehearsal"*, plus *"replace
similarity-only destructive fact cleanup with conservative source-aware resolution"*.

Two findings, both from rehearsing/measuring rather than reading:

1. **The warning vanished after one restart.** A00 already refuses to delete a
   non-empty incompatible collection, so facts genuinely survive. But
   `_embedder_changed()` wrote the sentinel as a side effect of *noticing* the change,
   so the second startup compared the new model against itself and reported nothing —
   while the collection still held vectors built by the old model. Facts survived and
   became silently unsearchable, which is the worse half of the failure.

2. **Dedup could permanently delete something Sam said** in favour of a longer
   claim JARVIS made. Measured on the real collection: of 10 near-duplicate pairs above
   the threshold, 1 was exactly that shape.
"""
import pytest

from backend.brain import memory


# --------------------------------------------------- detection vs. recording
@pytest.fixture
def sentinel(tmp_path, monkeypatch):
    path = tmp_path / ".embedder"
    monkeypatch.setattr(memory, "_EMBEDDER_SENTINEL", path)
    return path


def test_detection_does_not_write_the_sentinel(sentinel):
    """The whole bug in one assertion: checking must not record."""
    sentinel.write_text("old/model", encoding="utf-8")
    assert memory._embedder_changed() is True
    assert sentinel.read_text(encoding="utf-8").strip() == "old/model", (
        "detecting the change rewrote the sentinel, so the next startup would see none")


def test_detection_is_stable_across_repeated_calls(sentinel):
    sentinel.write_text("old/model", encoding="utf-8")
    assert [memory._embedder_changed() for _ in range(3)] == [True, True, True]


def test_a_first_run_with_no_sentinel_is_not_a_change(sentinel):
    """Nothing to be incompatible with."""
    assert memory._embedder_changed() is False


def test_a_matching_sentinel_is_not_a_change(sentinel):
    sentinel.write_text(memory._EMBED_MODEL, encoding="utf-8")
    assert memory._embedder_changed() is False


def test_recording_is_explicit(sentinel):
    memory._record_embedder_identity()
    assert sentinel.read_text(encoding="utf-8").strip() == memory._EMBED_MODEL


def test_an_unreadable_sentinel_degrades_to_unknown(sentinel, monkeypatch):
    def boom(*a, **k):
        raise OSError("unreadable")
    monkeypatch.setattr(type(sentinel), "exists", lambda self: True)
    monkeypatch.setattr(type(sentinel), "read_text", boom)
    assert memory._recorded_embedder() == ""
    assert memory._embedder_changed() is False     # unknown is not a known mismatch


# ------------------------------------------------------- the rehearsal itself
class _FakeClient:
    def __init__(self, points=1128):
        self.deleted = []
        self._points = points

    def get_collections(self):
        return type("R", (), {"collections": [type("C", (), {"name": "mem0"})()]})()

    def get_collection(self, name):
        vectors = type("V", (), {"size": memory._EMBED_DIM})()
        params = type("P", (), {"vectors": vectors, "sparse_vectors": {"bm25": 1}})()
        return type("I", (), {"config": type("C", (), {"params": params})()})()

    def count(self, collection_name=None, exact=True):
        return type("C", (), {"count": self._points})()

    def delete_collection(self, name):
        self.deleted.append(name)
        self._points = 0


@pytest.fixture
def rehearsal(sentinel, monkeypatch):
    import qdrant_client
    sentinel.write_text("thenlper/gte-small", encoding="utf-8")
    monkeypatch.setattr(memory, "_EMBED_MODEL", "BAAI/bge-base-en-v1.5")
    client = _FakeClient()
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda *a, **k: client)
    monkeypatch.setattr(memory, "_ALLOW_COLLECTION_DELETION", False)
    return client, sentinel


def test_old_facts_survive_and_stay_flagged_across_restarts(rehearsal):
    """The rehearsal. Startup 2 used to report nothing at all."""
    client, sentinel = rehearsal
    for startup in (1, 2, 3):
        memory._drop_stale_collections()
        assert client.deleted == [], f"startup {startup}: facts were deleted"
        flagged = memory.incompatible_collections()
        assert flagged == [("mem0", "embedder changed", 1128)], (
            f"startup {startup}: the incompatibility was forgotten")

    assert sentinel.read_text(encoding="utf-8").strip() == "thenlper/gte-small", (
        "the sentinel adopted the new model without the vectors ever being rebuilt")


def test_a_deliberate_migration_clears_the_flag(rehearsal, monkeypatch):
    """The operator opts in; only then is the identity recorded."""
    client, sentinel = rehearsal
    memory._drop_stale_collections()
    assert memory.incompatible_collections()

    monkeypatch.setattr(memory, "_ALLOW_COLLECTION_DELETION", True)
    memory._drop_stale_collections()

    assert client.deleted == ["mem0"]
    assert memory.incompatible_collections() == []
    assert sentinel.read_text(encoding="utf-8").strip() == "BAAI/bge-base-en-v1.5"


def test_a_compatible_collection_records_the_identity(sentinel, monkeypatch):
    """A fresh install must still adopt the active embedder, or every startup would
    treat an unknown sentinel as something to re-check forever."""
    import qdrant_client
    client = _FakeClient(points=0)
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda *a, **k: client)
    memory._drop_stale_collections()
    assert sentinel.read_text(encoding="utf-8").strip() == memory._EMBED_MODEL


# --------------------------------------------- source-aware fact deduplication
def _dedup_with(monkeypatch, facts, vectors, assistant_ids):
    from backend.brain import embeddings
    monkeypatch.setattr(memory, "_scroll_facts_sync", lambda: facts)
    monkeypatch.setattr(embeddings, "_embed_many_sync", lambda texts, background=False: vectors)
    monkeypatch.setattr(memory, "_get_assistant_attributed_ids", lambda: assistant_ids)
    removed = {}

    class _C:
        def retrieve(self, collection_name=None, ids=None, **k):
            return [type("P", (), {"id": i})() for i in ids]
        def delete(self, collection_name=None, points_selector=None):
            removed["ids"] = set(getattr(points_selector, "points", []))

    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda *a, **k: _C())
    monkeypatch.setattr(memory, "_find_mem0_collection", lambda: "mem0")
    memory._dedup_facts_sync()
    return removed.get("ids", set())


IDENTICAL = [[1.0] + [0.0] * 383, [1.0] + [0.0] * 383]     # cosine 1.0


def test_a_user_fact_is_never_deleted_for_a_longer_assistant_claim(monkeypatch):
    """The measured live shape: JARVIS's longer report vs Sam's shorter statement."""
    facts = [("a1", "User holds NVDA and here is a long assistant elaboration of it"),
             ("u1", "User holds NVDA")]
    removed = _dedup_with(monkeypatch, facts, IDENTICAL, {"a1"})
    assert removed == set(), "a cross-attribution pair was treated as duplicates"


def test_duplicates_within_one_source_are_still_removed(monkeypatch):
    """The control: source-awareness must not disable dedup entirely."""
    facts = [("u1", "User holds NVDA stock, bought in June"), ("u2", "User holds NVDA")]
    removed = _dedup_with(monkeypatch, facts, IDENTICAL, set())
    assert removed == {"u2"}, "the shorter same-source duplicate should still go"


def test_two_assistant_claims_still_dedupe(monkeypatch):
    facts = [("a1", "NVDA closed at 205.10 on June 7 2026"), ("a2", "NVDA closed at 205")]
    removed = _dedup_with(monkeypatch, facts, IDENTICAL, {"a1", "a2"})
    assert removed == {"a2"}
