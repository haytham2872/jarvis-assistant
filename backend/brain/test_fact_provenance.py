"""A05 slice 2: an assistant claim is not a verified user fact.

Roadmap acceptance: *"extraction does not treat assistant guesses as verified user
facts."*

The investigation behind this is worth keeping, because the first two hypotheses were
both wrong and only checking the real artifact settled it:

1. mem0 2.0.1 ships three extraction prompts. `FACT_RETRIEVAL_PROMPT` (legacy) and
   `USER_MEMORY_EXTRACTION_PROMPT` (unused here) both forbid extracting from assistant
   messages. The one actually in use, `ADDITIVE_EXTRACTION_PROMPT`, deliberately does the
   opposite: *"You extract from BOTH user and assistant messages."*
2. So extraction from assistant turns is intended — and mem0 already records
   `attributed_to` per fact. Provenance is captured correctly at write time.
3. The real gap was the read path. mem0's own `search()` does **not** return
   `attributed_to`, and nothing in this codebase read it. Measured on the live
   collection: **622 of 1,128 facts are assistant-attributed** and every one was being
   injected into `[Memory]` as if Sam had said it.

Confirmed live against the real extractor, the shape that matters: an assistant
*assertion* is stored unhedged — `"User holds 200 shares of NVDA at an average cost
basis of $118"` — textually identical to the same fact when Sam states it. Only
`attributed_to` separates them. (Speculation fares better: mem0 keeps the "likely".)
"""
import pytest

from backend.brain import memory


@pytest.fixture(autouse=True)
def _clear_caches():
    memory.invalidate_attribution_cache()
    memory._assistant_ids = set()
    yield
    memory.invalidate_attribution_cache()
    memory._assistant_ids = set()


# ------------------------------------------------------------- the id cache
class _FakeQdrant:
    def __init__(self, ids, fail=False):
        self._ids = ids
        self.fail = fail
        self.calls = 0

    def scroll(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("qdrant down")
        return [type("P", (), {"id": i})() for i in self._ids], None


def _install(monkeypatch, fake):
    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda *a, **k: fake)
    monkeypatch.setattr(memory, "_find_mem0_collection", lambda: "mem0")


def test_the_cache_reads_assistant_ids(monkeypatch):
    _install(monkeypatch, _FakeQdrant(["a1", "a2"]))
    assert memory._get_assistant_attributed_ids() == {"a1", "a2"}


def test_the_cache_is_not_rescrolled_every_turn(monkeypatch):
    """A per-turn Qdrant round-trip on the recall path is exactly what the sibling
    consolidated-id cache exists to avoid."""
    fake = _FakeQdrant(["a1"])
    _install(monkeypatch, fake)
    for _ in range(5):
        memory._get_assistant_attributed_ids()
    assert fake.calls == 1


def test_invalidating_forces_a_refresh(monkeypatch):
    fake = _FakeQdrant(["a1"])
    _install(monkeypatch, fake)
    memory._get_assistant_attributed_ids()
    memory.invalidate_attribution_cache()
    memory._get_assistant_attributed_ids()
    assert fake.calls == 2


def test_a_qdrant_failure_degrades_to_no_labels(monkeypatch):
    """Silent degradation: an unavailable Qdrant must not stop recall. Unlabelled is
    exactly how facts read before this feature existed."""
    _install(monkeypatch, _FakeQdrant([], fail=True))
    assert memory._get_assistant_attributed_ids() == set()


def test_no_collection_resolves_to_no_labels(monkeypatch):
    monkeypatch.setattr(memory, "_find_mem0_collection", lambda: None)
    assert memory._get_assistant_attributed_ids() == set()


# ------------------------------------------------- mem0 fake
def _fake_mem(items):
    class _M:
        def search(self, query, filters=None, top_k=None):
            return {"results": items}
    return _M()


# ------------------------------------------------- the on-demand tool path
def test_search_facts_carries_the_flag(monkeypatch):
    items = [
        {"id": "a1", "memory": "NVDA closed at $205.10", "score": 0.9},
        {"id": "u1", "memory": "User bought NVDA", "score": 0.8},
    ]
    monkeypatch.setattr(memory, "_get_mem", lambda: _fake_mem(items))
    monkeypatch.setattr(memory, "_get_consolidated_ids", lambda: set())
    monkeypatch.setattr(memory, "_get_assistant_attributed_ids", lambda: {"a1"})

    out = memory._search_facts_sync("nvda", 10)
    by_text = {h["text"]: h for h in out}
    assert by_text["NVDA closed at $205.10"]["from_assistant"] is True
    assert by_text["User bought NVDA"]["from_assistant"] is False


def test_memory_search_renders_the_provenance_label():
    """The flag is only useful if the tool's own output actually shows it."""
    import inspect
    from backend.tools import vault

    assert "you said this, not «owner»" in inspect.getsource(vault), (
        "memory_search no longer distinguishes an assistant claim from a user fact")
