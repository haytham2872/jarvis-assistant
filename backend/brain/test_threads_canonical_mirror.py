"""A02: record_thread mirrors every exchange into the canonical SQLite store.

Drives the real `threads.record_thread()` with only the Qdrant write and the semantic
index faked, so what is exercised is the actual wiring rather than the repository in
isolation. The property under test is the one the roadmap names: an ordinary
(low-significance) exchange must remain reconstructable in full even if indexing never
happens.
"""
import asyncio

import pytest

from backend.brain import threads
from backend.storage import db, exchanges


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    # Qdrant write and semantic indexing are not what this file is about.
    monkeypatch.setattr(threads, "_write_thread_sync", lambda thread: None)
    monkeypatch.setattr(threads, "log", threads.log)
    yield
    db._reset_for_tests()


def _no_index(monkeypatch):
    """Simulate the semantic index being unavailable -- the exchange must survive anyway."""
    import backend.brain.semindex as semindex

    async def _boom(*a, **kw):
        raise RuntimeError("index down")

    monkeypatch.setattr(semindex, "index_exchange", _boom)


def test_low_significance_exchange_is_stored_in_full(monkeypatch):
    """A Thread stores user=""/assistant="" here; the canonical record must not."""
    _no_index(monkeypatch)
    thread = run(threads.record_thread(
        "the SearXNG port is 8088", "Noted.", "sess-1", channel="local"))

    assert thread is not None
    assert thread.high is False
    assert thread.user == "" and thread.assistant == ""      # the old loss, still true

    row = exchanges.get_exchange(thread.id)
    assert row is not None, "exchange was not mirrored"
    assert row["user_text"] == "the SearXNG port is 8088"     # ...and now recovered
    assert row["assistant_text"] == "Noted."


def test_exchange_survives_a_failing_semantic_index(monkeypatch):
    """Index failure used to be able to lose the reconstructable original."""
    _no_index(monkeypatch)
    thread = run(threads.record_thread("remember 42", "ok", "sess-1"))
    assert exchanges.get_exchange(thread.id)["user_text"] == "remember 42"


def test_ids_correlate_between_the_two_stores(monkeypatch):
    """Same uuid in both, so the records can be joined without a second identifier."""
    _no_index(monkeypatch)
    thread = run(threads.record_thread("hello", "hi", "sess-1", channel="discord"))
    row = exchanges.get_exchange(thread.id)
    assert row["id"] == thread.id
    assert row["session_id"] == "sess-1"
    assert row["channel"] == "discord"
    assert row["timestamp"] == pytest.approx(thread.timestamp)


def test_high_significance_exchange_is_also_mirrored(monkeypatch):
    _no_index(monkeypatch)
    long_text = "consciousness and memory and identity " * 30
    thread = run(threads.record_thread(long_text, long_text, "sess-1"))
    assert thread.high is True
    assert exchanges.get_exchange(thread.id)["user_text"] == long_text


def test_storage_failure_does_not_break_the_turn(monkeypatch):
    """Additive means additive: if SQLite is unusable, record_thread still succeeds."""
    _no_index(monkeypatch)
    monkeypatch.setattr(db, "connect", lambda: None)
    thread = run(threads.record_thread("still works", "yes", "sess-1"))
    assert thread is not None
    assert thread.session_id == "sess-1"


def test_empty_exchange_records_nothing(monkeypatch):
    _no_index(monkeypatch)
    assert run(threads.record_thread("   ", "  ", "sess-1")) is None
    assert exchanges.counts().get("exchanges", 0) == 0


def test_session_activity_is_tracked_across_turns(monkeypatch):
    _no_index(monkeypatch)
    run(threads.record_thread("one", "a", "sess-1"))
    run(threads.record_thread("two", "b", "sess-1"))
    rows = exchanges.exchanges_for_session("sess-1")
    assert [r["user_text"] for r in rows] == ["one", "two"]
    assert exchanges.counts()["sessions"] == 1
