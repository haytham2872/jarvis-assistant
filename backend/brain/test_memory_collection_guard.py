"""A00 data guard: an incompatible mem0 collection is reported, never silently deleted.

`_drop_stale_collections()` used to call `client.delete_collection()` at startup on any
embedder/dimension/BM25 mismatch, destroying every extracted fact with no confirmation
and no backup. The roadmap names this directly: "No startup deletion of a fact collection
because the embedder, dimension or sparse configuration differs."

These drive the real function against a fake Qdrant client -- no server required.
"""
import pytest

from backend.brain import memory


class _Cols:
    def __init__(self, names):
        self.collections = [type("C", (), {"name": n})() for n in names]


class _Info:
    def __init__(self, size, sparse, points=None):
        self.config = type("Cfg", (), {})()
        self.config.params = type("P", (), {})()
        self.config.params.vectors = type("V", (), {"size": size})()
        self.config.params.sparse_vectors = sparse
        self.points_count = points


class _FakeClient:
    def __init__(self, names, info_by_name, counts=None, count_raises=False):
        self._names = names
        self._info = info_by_name
        self._counts = counts or {}
        self._count_raises = count_raises
        self.deleted = []

    def get_collections(self):
        return _Cols(self._names)

    def get_collection(self, name):
        return self._info[name]

    def count(self, collection_name, exact=True):
        if self._count_raises:
            raise RuntimeError("count unsupported")
        return type("R", (), {"count": self._counts.get(collection_name, 0)})()

    def delete_collection(self, name):
        self.deleted.append(name)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Never touch the real sentinel file or a real Qdrant during these tests."""
    monkeypatch.setattr(memory, "_embedder_changed", lambda: True)
    monkeypatch.setattr(memory, "_ALLOW_COLLECTION_DELETION", False)
    memory._INCOMPATIBLE_COLLECTIONS.clear()
    yield
    memory._INCOMPATIBLE_COLLECTIONS.clear()


def _run(monkeypatch, client):
    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kw: client)
    memory._drop_stale_collections()


def test_non_empty_incompatible_collection_is_never_deleted(monkeypatch):
    client = _FakeClient(["mem0"], {"mem0": _Info(1536, None)}, counts={"mem0": 412})
    _run(monkeypatch, client)

    assert client.deleted == []                      # the data survives
    reported = memory.incompatible_collections()
    assert reported == [("mem0", "embedder changed", 412)]


def test_empty_incompatible_collection_is_still_dropped(monkeypatch):
    """A fresh install can legitimately create a wrong-dimension collection with no
    points. Refusing to drop that would break setup and protects nothing."""
    client = _FakeClient(["mem0"], {"mem0": _Info(1536, None)}, counts={"mem0": 0})
    _run(monkeypatch, client)

    assert client.deleted == ["mem0"]
    assert memory.incompatible_collections() == []


def test_unknown_point_count_is_treated_as_unsafe_not_empty(monkeypatch):
    """-1 must never be mistaken for 'empty and therefore safe to delete'."""
    client = _FakeClient(["mem0"], {"mem0": _Info(1536, None)}, count_raises=True)
    client._info["mem0"].points_count = None
    _run(monkeypatch, client)

    assert client.deleted == []
    assert [c[0] for c in memory.incompatible_collections()] == ["mem0"]
    assert memory.incompatible_collections()[0][2] == -1


def test_compatible_collection_is_left_alone(monkeypatch):
    monkeypatch.setattr(memory, "_embedder_changed", lambda: False)
    client = _FakeClient(
        ["mem0"], {"mem0": _Info(memory._EMBED_DIM, {"bm25": object()})}, counts={"mem0": 9})
    _run(monkeypatch, client)

    assert client.deleted == []
    assert memory.incompatible_collections() == []


def test_explicit_opt_in_restores_destructive_migration(monkeypatch):
    """The deliberate, backed-up migration path stays available -- but only on purpose."""
    monkeypatch.setattr(memory, "_ALLOW_COLLECTION_DELETION", True)
    client = _FakeClient(["mem0"], {"mem0": _Info(1536, None)}, counts={"mem0": 412})
    _run(monkeypatch, client)

    assert client.deleted == ["mem0"]
    assert memory.incompatible_collections() == []


def test_report_is_rebuilt_each_run_not_accumulated(monkeypatch):
    client = _FakeClient(["mem0"], {"mem0": _Info(1536, None)}, counts={"mem0": 5})
    _run(monkeypatch, client)
    _run(monkeypatch, client)
    assert len(memory.incompatible_collections()) == 1


def test_only_mem0_collections_are_considered(monkeypatch):
    """jarvis_semantic / jarvis_threads have their own lifecycle and must not be swept
    up by the mem0 compatibility check."""
    client = _FakeClient(
        ["mem0", "jarvis_semantic", "jarvis_threads"],
        {"mem0": _Info(memory._EMBED_DIM, {"bm25": object()}),
         "jarvis_semantic": _Info(1536, None),
         "jarvis_threads": _Info(1536, None)},
        counts={"mem0": 0, "jarvis_semantic": 0, "jarvis_threads": 0},
    )
    _run(monkeypatch, client)
    assert client.deleted == ["mem0"]
