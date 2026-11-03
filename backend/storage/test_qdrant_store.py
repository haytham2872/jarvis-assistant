"""One owner for Qdrant clients, and `localhost` pinned to the loopback it is bound on.

Measured on the desk, one `get_collection`: 2,518 ms per call as the code stood
(`localhost` + a new client each time) against 4.5 ms with a shared client on
127.0.0.1. See backend/storage/qdrant.py for the breakdown.
"""
import ast
import pathlib

import pytest

from backend.storage import qdrant as store


@pytest.fixture(autouse=True)
def _clean():
    store._clients.clear()
    yield
    store._clients.clear()


class FakeClient:
    made = 0

    def __init__(self, host, port, timeout):
        FakeClient.made += 1
        self.host, self.port, self.timeout = host, port, timeout
        self.closed = False

    def close(self):
        self.closed = True


def test_localhost_is_pinned_to_the_ipv4_loopback(monkeypatch):
    """compose binds 127.0.0.1 only; on Windows `localhost` tries ::1 first and stalls."""
    monkeypatch.setenv("QDRANT_HOST", "localhost")
    assert store.host() == "127.0.0.1"
    monkeypatch.setenv("QDRANT_HOST", "LocalHost ")
    assert store.host() == "127.0.0.1"


def test_a_real_remote_host_is_left_alone(monkeypatch):
    monkeypatch.setenv("QDRANT_HOST", "10.0.0.7")
    assert store.host() == "10.0.0.7"


def test_the_default_is_the_ipv4_loopback(monkeypatch):
    monkeypatch.delenv("QDRANT_HOST", raising=False)
    assert store.host() == "127.0.0.1"


def test_the_genuine_client_is_built_once_per_timeout(monkeypatch):
    import qdrant_client
    FakeClient.made = 0
    monkeypatch.setattr(store, "_REAL_CLIENT", FakeClient)
    monkeypatch.setattr(qdrant_client, "QdrantClient", FakeClient)

    a, b = store.get_client(timeout=3), store.get_client(timeout=3)
    c = store.get_client(timeout=10)
    assert a is b and a is not c
    assert FakeClient.made == 2
    assert (a.host, a.port) == (store.host(), 6333)


def test_a_patched_in_fake_is_never_cached(monkeypatch):
    """The isolation property: a test that fakes the client must get its fake every
    time, and must never be handed a real client cached by an earlier caller."""
    import qdrant_client
    sentinel = object()
    store._clients[(store.host(), 3.0)] = sentinel        # pretend a real one is cached
    FakeClient.made = 0
    monkeypatch.setattr(qdrant_client, "QdrantClient", FakeClient)

    a, b = store.get_client(timeout=3), store.get_client(timeout=3)
    assert a is not sentinel and b is not sentinel
    assert a is not b and FakeClient.made == 2


def test_close_all_releases_and_forgets(monkeypatch):
    import qdrant_client
    monkeypatch.setattr(store, "_REAL_CLIENT", FakeClient)
    monkeypatch.setattr(qdrant_client, "QdrantClient", FakeClient)
    c = store.get_client()
    store.close_all()
    assert c.closed and store._clients == {}


def test_no_module_builds_its_own_qdrant_client():
    """The regression guard. Every client goes through the store except the backup CLI,
    which runs as its own short-lived process."""
    root = pathlib.Path(store.__file__).resolve().parent.parent
    allowed = {"storage/qdrant.py", "tools/backup.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if path.name.startswith("test_") or rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name == "QdrantClient":
                    offenders.append(rel)
    assert not offenders, f"direct QdrantClient construction in: {sorted(set(offenders))}"


def test_mem0_uses_the_pinned_host_too():
    """mem0 builds its own client from its config -- recall's hot path."""
    from backend.brain import memory
    assert memory._MEM0_CONFIG["vector_store"]["config"]["host"] == store.host()
