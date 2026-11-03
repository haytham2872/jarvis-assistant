"""A08: session admission must not scan every sleep marker on every turn."""
from types import SimpleNamespace

import pytest

from backend.brain import sleep


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(sleep, "_latest_loaded", False)
    monkeypatch.setattr(sleep, "_latest_marker", None)
    monkeypatch.setattr(sleep, "_ensure_collection", lambda client: True)


def test_history_is_scanned_once_and_result_is_immutable_to_callers(monkeypatch):
    calls = []

    class Client:
        def scroll(self, **kwargs):
            calls.append(kwargs.get("offset"))
            if kwargs.get("offset") is None:
                return ([SimpleNamespace(payload={"timestamp": 1.0, "id": "old"}),
                         SimpleNamespace(payload={"timestamp": 3.0, "id": "new"})], 2)
            return ([SimpleNamespace(payload={"timestamp": 2.0, "id": "middle"})], None)

    monkeypatch.setattr(sleep, "_qdrant", lambda: Client())
    first = sleep._scroll_latest_sync()
    assert first["id"] == "new"
    first["id"] = "tampered"
    assert sleep._scroll_latest_sync()["id"] == "new"
    assert calls == [None, 2]


def test_failed_read_is_retried(monkeypatch):
    attempts = []

    class Client:
        def scroll(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("Qdrant temporarily unavailable")
            return [SimpleNamespace(payload={"timestamp": 4.0})], None

    monkeypatch.setattr(sleep, "_qdrant", lambda: Client())
    assert sleep._scroll_latest_sync() is None
    assert sleep._scroll_latest_sync()["timestamp"] == 4.0
    assert len(attempts) == 2


def test_confirmed_empty_collection_is_cached(monkeypatch):
    calls = []

    class Client:
        def scroll(self, **kwargs):
            calls.append(1)
            return [], None

    monkeypatch.setattr(sleep, "_qdrant", lambda: Client())
    assert sleep._scroll_latest_sync() is None
    assert sleep._scroll_latest_sync() is None
    assert len(calls) == 1


def test_newly_written_marker_replaces_cache(monkeypatch):
    upserts = []

    class Client:
        def scroll(self, **kwargs):
            return [SimpleNamespace(payload={"timestamp": 1.0, "id": "old"})], None

        def upsert(self, **kwargs):
            upserts.append(kwargs)

    monkeypatch.setattr(sleep, "_qdrant", lambda: Client())
    assert sleep._scroll_latest_sync()["id"] == "old"
    marker = sleep.SleepMarker(
        id="new", timestamp=5.0, session_id="s", mood_tone="calm",
        mood_intensity=0.1, mood_trajectory="stable", turn_count=2,
    )
    sleep._write_marker_sync(marker)
    assert upserts
    assert sleep._scroll_latest_sync()["id"] == "new"
