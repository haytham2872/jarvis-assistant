"""Tests for backend/brain/threads.py's record_thread() -- Step 2 (2026-09-08):
every exchange is indexed in full now, regardless of significance, and the
exchange payload carries channel/summary/exchange_id (all previously dropped
at index time). No real Qdrant is touched -- _write_thread_sync and
semindex.index_exchange are both monkeypatched.
"""

import asyncio

import pytest

from backend.brain import threads as th


def _patch(monkeypatch):
    """Stub out real persistence; capture what index_exchange was called with."""
    monkeypatch.setattr(th, "_write_thread_sync", lambda thread: None)
    captured = {}

    async def fake_index_exchange(text, session_id, significance, timestamp,
                                   channel="local", summary="", exchange_id=""):
        captured.update(
            text=text, session_id=session_id, significance=significance,
            timestamp=timestamp, channel=channel, summary=summary,
            exchange_id=exchange_id,
        )
        return True

    monkeypatch.setattr("backend.brain.semindex.index_exchange", fake_index_exchange)
    return captured


def _record_and_capture(monkeypatch, user_text, assistant_text, session_id, channel="local"):
    """record_thread() fires semindex.index_exchange via asyncio.create_task
    (fire-and-forget) -- must yield once, in the SAME event loop, before that
    task has a chance to run. A separate asyncio.run() call afterward would
    use a different loop entirely and never see it (the task would simply be
    torn down with the first loop, unobserved)."""
    captured = _patch(monkeypatch)

    async def scenario():
        thread = await th.record_thread(user_text, assistant_text, session_id, channel=channel)
        await asyncio.sleep(0)  # let the fire-and-forget index_exchange task run
        return thread

    thread = asyncio.run(scenario())
    return thread, captured


# ---------------------------------------------------------------------------

def test_low_significance_exchange_is_still_indexed_in_full(monkeypatch):
    """The exact information loss the upgrade plan names: a low-sig turn used
    to store only its ~150-char summary in the semantic index -- "the SearXNG
    port is 8088" flattened to a summary with the number gone. Now the full
    text is indexed regardless of significance."""
    thread, captured = _record_and_capture(
        monkeypatch, "the SearXNG port is 8088", "got it, noted", "s1"
    )
    assert thread.high is False, "test assumption: this exchange must actually be low-sig"
    assert "8088" in captured["text"]
    assert "SearXNG port" in captured["text"]


def test_indexed_text_is_always_user_and_assistant_joined(monkeypatch):
    _thread, captured = _record_and_capture(monkeypatch, "user says X", "assistant replies Y", "s1")
    assert captured["text"] == "user says X\nassistant replies Y"


def test_channel_is_passed_through_to_the_exchange_payload(monkeypatch):
    _thread, captured = _record_and_capture(monkeypatch, "hi", "hello", "s1", channel="discord")
    assert captured["channel"] == "discord"


def test_channel_defaults_to_local(monkeypatch):
    _thread, captured = _record_and_capture(monkeypatch, "hi", "hello", "s1")
    assert captured["channel"] == "local"


def test_summary_is_always_computed_and_passed_regardless_of_significance(monkeypatch):
    _thread, captured = _record_and_capture(monkeypatch, "short user text", "short reply", "s1")
    assert captured["summary"] != ""
    assert captured["summary"].startswith("User:")


def test_exchange_id_is_the_threads_own_id(monkeypatch):
    thread, captured = _record_and_capture(monkeypatch, "hi", "hello", "s1")
    assert thread is not None
    assert captured["exchange_id"] == thread.id
    assert captured["exchange_id"] != ""


def test_session_id_and_significance_and_timestamp_still_flow_through(monkeypatch):
    thread, captured = _record_and_capture(monkeypatch, "hi there", "hello back", "session-xyz")
    assert captured["session_id"] == "session-xyz"
    assert captured["significance"] == thread.significance
    assert captured["timestamp"] == thread.timestamp


def test_high_significance_exchange_still_indexed_in_full_as_before(monkeypatch):
    """Regression guard: high-sig turns (which already stored full text
    pre-Step-2) must be completely unaffected."""
    long_user = "consciousness " * 60  # clears both length and keyword saturation
    long_assistant = "identity and memory " * 60
    thread, captured = _record_and_capture(monkeypatch, long_user, long_assistant, "s1")
    assert thread.high is True, "test assumption: this exchange must actually be high-sig"
    assert long_user in captured["text"]
    assert long_assistant in captured["text"]


def test_blank_exchange_does_not_index_anything(monkeypatch):
    captured = _patch(monkeypatch)
    result = asyncio.run(th.record_thread("   ", "", "s1"))
    assert result is None
    assert captured == {}
