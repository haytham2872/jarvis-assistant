"""Tests for backend/brain/canary.py — the memory-layer self-diagnosis.

Pure-function + state-file tests, plus run_check() with all three probes
monkeypatched out (no real Qdrant/mem0/network). Run with:
    .venv\\Scripts\\pytest backend/brain/test_canary.py -v
"""

import time
import asyncio

import pytest

from backend.brain import canary


# ---------------------------------------------------------------------------
# due — pure gating
# ---------------------------------------------------------------------------

def test_due_false_immediately_after_a_check():
    now = time.time()
    assert not canary.due(now, now)


def test_due_true_after_a_full_interval():
    now = time.time()
    last = now - (canary._CHECK_INTERVAL_HOURS * 3600.0) - 1.0
    assert canary.due(last, now)


def test_due_false_just_under_the_interval():
    now = time.time()
    last = now - (canary._CHECK_INTERVAL_HOURS * 3600.0) + 60.0
    assert not canary.due(last, now)


# ---------------------------------------------------------------------------
# sentinel text — must be unique per check (embeds a timestamp)
# ---------------------------------------------------------------------------

def test_sentinel_text_contains_prefix_and_is_distinct_per_call():
    a = canary._sentinel_text(1000.0)
    b = canary._sentinel_text(2000.0)
    assert canary._SENTINEL_PREFIX in a
    assert canary._SENTINEL_PREFIX in b
    assert a != b


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "_STATE_FILE", tmp_path / "canary_state.json")
    return tmp_path / "canary_state.json"


def test_load_state_missing_file_returns_empty(tmp_state_file):
    assert canary._load_state() == {}


def test_save_then_load_round_trips(tmp_state_file):
    canary._save_state({"mem0_memory_id": "abc-123", "last_check": 12345.0})
    assert canary._load_state() == {"mem0_memory_id": "abc-123", "last_check": 12345.0}


def test_corrupt_state_file_degrades_to_empty(tmp_state_file):
    tmp_state_file.write_text("{not json", encoding="utf-8")
    assert canary._load_state() == {}


# ---------------------------------------------------------------------------
# _check_mem0_sync — probe internals against a fake mem0 (2026-09-01: the
# old "sentinel must appear in search results" criterion false-positived
# live -- mem0 2.0.1's entity-boost scoring caps an infer=False sentinel at
# 0.8 combined while real memories ABOUT the canary score 0.88+, locking
# the sentinel out of the top-k once enough canary-discussion facts
# accumulated. New criterion: search pipeline returns something AND the
# just-written point reads back by id.)
# ---------------------------------------------------------------------------

class _FakeMem:
    def __init__(self, search_results=None, get_returns_sentinel=True, get_raises=False):
        self.search_results = search_results if search_results is not None else [{"memory": "unrelated"}]
        self.get_returns_sentinel = get_returns_sentinel
        self.get_raises = get_raises
        self.added = []
        self.deleted = []
        self.search_kwargs = None
        self._stored = {}

    def add(self, messages, user_id=None, infer=False):
        new_id = f"id-{len(self.added)}"
        self.added.append(messages[0]["content"])
        self._stored[new_id] = messages[0]["content"]
        return {"results": [{"id": new_id}]}

    def search(self, query, **kwargs):
        self.search_kwargs = kwargs
        return {"results": self.search_results}

    def get(self, memory_id):
        if self.get_raises:
            raise RuntimeError("get exploded")
        if self.get_returns_sentinel:
            return {"memory": self._stored.get(memory_id, "")}
        return {"memory": "something else entirely"}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _install_fake_mem(monkeypatch, fake):
    from backend.brain import memory as memory_mod
    monkeypatch.setattr(memory_mod, "_get_mem", lambda: fake)


def test_mem0_probe_passes_when_pipeline_runs_and_write_reads_back(monkeypatch):
    fake = _FakeMem()
    _install_fake_mem(monkeypatch, fake)
    found, new_id = canary._check_mem0_sync("SENTINEL-xyz", None)
    assert found is True
    assert new_id == "id-0"


def test_mem0_probe_passes_even_when_sentinel_outranked_in_search(monkeypatch):
    """The exact live false-positive scenario (2026-09-01 21:23): search
    returns real canary-discussion memories, none of which contain the
    sentinel -- the layer is HEALTHY and the probe must say so."""
    fake = _FakeMem(search_results=[
        {"memory": "The canary feature is a three-layer self-diagnosis probe"},
        {"memory": "Assistant views the memory canary as critical infrastructure"},
    ])
    _install_fake_mem(monkeypatch, fake)
    found, _ = canary._check_mem0_sync("CANARY-SENTINEL-jarvis-memory-self-check 2026-09-01", None)
    assert found is True


def test_mem0_probe_fails_when_search_returns_empty(monkeypatch):
    """The historical failure class this canary exists for: the search
    pipeline silently returning nothing (qdrant-client API removal)."""
    fake = _FakeMem(search_results=[])
    _install_fake_mem(monkeypatch, fake)
    found, _ = canary._check_mem0_sync("SENTINEL-xyz", None)
    assert found is False


def test_mem0_probe_fails_when_write_does_not_read_back(monkeypatch):
    fake = _FakeMem(get_returns_sentinel=False)
    _install_fake_mem(monkeypatch, fake)
    found, _ = canary._check_mem0_sync("SENTINEL-xyz", None)
    assert found is False


def test_mem0_probe_fails_when_get_raises(monkeypatch):
    fake = _FakeMem(get_raises=True)
    _install_fake_mem(monkeypatch, fake)
    found, _ = canary._check_mem0_sync("SENTINEL-xyz", None)
    assert found is False


def test_mem0_probe_uses_top_k_not_limit(monkeypatch):
    """mem0 2.0.1 renamed limit -> top_k and swallows unknown kwargs
    silently -- passing limit= means the default top_k=20 applies with no
    error. Pin the correct parameter name."""
    fake = _FakeMem()
    _install_fake_mem(monkeypatch, fake)
    canary._check_mem0_sync("SENTINEL-xyz", None)
    assert "top_k" in fake.search_kwargs
    assert "limit" not in fake.search_kwargs


def test_mem0_probe_deletes_previous_sentinel_first(monkeypatch):
    fake = _FakeMem()
    _install_fake_mem(monkeypatch, fake)
    canary._check_mem0_sync("SENTINEL-xyz", "old-sentinel-id")
    assert fake.deleted == ["old-sentinel-id"]


# ---------------------------------------------------------------------------
# run_check — aggregation + alerting, all three probes stubbed
# ---------------------------------------------------------------------------

def _stub_probes(monkeypatch, mem0_ok=True, semantic_ok=True, threads_ok=True, new_mem0_id="new-id"):
    monkeypatch.setattr(canary, "_check_mem0_sync", lambda sentinel, prev_id: (mem0_ok, new_mem0_id))

    async def fake_semantic(sentinel):
        return semantic_ok
    monkeypatch.setattr(canary, "_check_semantic", fake_semantic)

    async def fake_threads(sentinel, now):
        return threads_ok
    monkeypatch.setattr(canary, "_check_threads", fake_threads)


def test_run_check_all_healthy_no_alert(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, True, True, True)
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    canary.set_notifier(fake_notifier)
    try:
        result = asyncio.run(canary.run_check())
        assert result["failed"] == []
        assert sent == []
    finally:
        canary.set_notifier(None)


def test_run_check_one_layer_failed_triggers_alert(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, mem0_ok=False, semantic_ok=True, threads_ok=True)
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    canary.set_notifier(fake_notifier)
    try:
        result = asyncio.run(canary.run_check())
        assert result["failed"] == ["mem0"]
        assert len(sent) == 1
        assert "mem0" in sent[0]
    finally:
        canary.set_notifier(None)


def test_run_check_multiple_layers_failed_named_in_one_alert(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, mem0_ok=False, semantic_ok=False, threads_ok=True)
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    canary.set_notifier(fake_notifier)
    try:
        result = asyncio.run(canary.run_check())
        assert set(result["failed"]) == {"mem0", "jarvis_semantic"}
        assert len(sent) == 1
        assert "mem0" in sent[0] and "jarvis_semantic" in sent[0]
    finally:
        canary.set_notifier(None)


def test_run_check_failure_without_notifier_does_not_raise(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, mem0_ok=False, semantic_ok=False, threads_ok=False)
    canary.set_notifier(None)
    result = asyncio.run(canary.run_check())
    assert set(result["failed"]) == {"mem0", "jarvis_semantic", "jarvis_threads"}


def test_run_check_persists_new_mem0_memory_id(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, True, True, True, new_mem0_id="fresh-id-42")
    asyncio.run(canary.run_check())
    state = canary._load_state()
    assert state["mem0_memory_id"] == "fresh-id-42"
    assert "last_check" in state


def test_run_check_passes_previous_mem0_id_to_the_probe(tmp_state_file, monkeypatch):
    canary._save_state({"mem0_memory_id": "old-id"})
    seen_prev_ids = []

    def fake_mem0(sentinel, prev_id):
        seen_prev_ids.append(prev_id)
        return True, "new-id"

    monkeypatch.setattr(canary, "_check_mem0_sync", fake_mem0)

    async def fake_semantic(sentinel):
        return True
    async def fake_threads(sentinel, now):
        return True
    monkeypatch.setattr(canary, "_check_semantic", fake_semantic)
    monkeypatch.setattr(canary, "_check_threads", fake_threads)

    asyncio.run(canary.run_check())
    assert seen_prev_ids == ["old-id"]


# ---------------------------------------------------------------------------
# last_check_info — read-only accessor used by runtime_status.py
# ---------------------------------------------------------------------------

def test_last_check_info_no_state_file(tmp_state_file):
    last_check, failed = canary.last_check_info()
    assert last_check is None
    assert failed == []


def test_last_check_info_reads_back_what_run_check_persisted(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, mem0_ok=False, semantic_ok=True, threads_ok=True)
    asyncio.run(canary.run_check())
    last_check, failed = canary.last_check_info()
    assert last_check is not None
    assert failed == ["mem0"]


def test_last_check_info_empty_failed_when_all_healthy(tmp_state_file, monkeypatch):
    _stub_probes(monkeypatch, True, True, True)
    asyncio.run(canary.run_check())
    last_check, failed = canary.last_check_info()
    assert last_check is not None
    assert failed == []
