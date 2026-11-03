"""A02 slice 2: session lookup served by index, and Discord ingress deduplicated.

Two roadmap acceptance criteria:
  - "Recent session queries do not scan all history."
  - "Duplicate message IDs create one run."
"""
import asyncio

import pytest

from backend.brain import discord_bridge, threads
from backend.storage import db, exchanges


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _add(eid, session, channel, ts):
    exchanges.record_exchange(
        exchange_id=eid, session_id=session, channel=channel, user_text="u",
        assistant_text="a", significance=0.1, high=False, timestamp=ts)


# ------------------------------------------------- session lookup off the index
def test_last_exchange_comes_from_sqlite_without_touching_qdrant(monkeypatch):
    called = {"qdrant": 0}

    async def _never(*a, **kw):
        called["qdrant"] += 1
        return []

    monkeypatch.setattr(threads, "recent_threads", _never)
    _add("e1", "sess-1", "local", 500.0)

    got = run(threads._last_exchange("local"))
    assert got == {"timestamp": 500.0, "session_id": "sess-1"}
    assert called["qdrant"] == 0, "Qdrant scan should not have been needed"


def test_falls_back_to_qdrant_when_sqlite_has_nothing(monkeypatch):
    """Pre-A02 history, an empty store, or storage down -- previous behavior exactly."""
    async def _fallback(limit=1, channel=None):
        return [{"timestamp": 42.0, "session_id": "legacy-sess"}]

    monkeypatch.setattr(threads, "recent_threads", _fallback)
    got = run(threads._last_exchange("local"))
    assert got["session_id"] == "legacy-sess"


def test_falls_back_when_storage_is_unavailable(monkeypatch):
    async def _fallback(limit=1, channel=None):
        return [{"timestamp": 7.0, "session_id": "from-qdrant"}]

    monkeypatch.setattr(threads, "recent_threads", _fallback)
    monkeypatch.setattr(db, "connect", lambda: None)
    assert run(threads._last_exchange("local"))["session_id"] == "from-qdrant"


def test_lookup_is_channel_scoped(monkeypatch):
    async def _never(*a, **kw):
        return []

    monkeypatch.setattr(threads, "recent_threads", _never)
    _add("l", "sess-local", "local", 100.0)
    _add("d", "sess-discord", "discord", 900.0)
    assert run(threads._last_exchange("local"))["session_id"] == "sess-local"
    assert run(threads._last_exchange("discord"))["session_id"] == "sess-discord"


def test_assign_session_reuses_the_session_within_the_gap(monkeypatch):
    """The real decision, driven through assign_session rather than the helper."""
    import time

    async def _no_sleep():
        return None

    monkeypatch.setattr(threads, "recent_threads", lambda *a, **kw: _empty())
    monkeypatch.setattr("backend.brain.sleep.latest_sleep", _no_sleep)

    async def _empty():
        return []

    now = time.time()
    _add("e1", "sess-current", "local", now - 60)      # one minute ago
    decision = run(threads.assign_session(now, channel="local"))
    assert decision.session_id == "sess-current"
    assert decision.ended_session_id is None


def test_assign_session_starts_a_new_one_past_the_gap(monkeypatch):
    import time

    async def _no_sleep():
        return None

    async def _empty(*a, **kw):
        return []

    monkeypatch.setattr(threads, "recent_threads", _empty)
    monkeypatch.setattr("backend.brain.sleep.latest_sleep", _no_sleep)

    now = time.time()
    _add("e1", "sess-old", "local", now - (threads.SESSION_GAP_THRESHOLD + 60))
    decision = run(threads.assign_session(now, channel="local"))
    assert decision.session_id != "sess-old"
    assert decision.ended_session_id == "sess-old"


# ------------------------------------------------------------ ingress dedup
class _Msg:
    def __init__(self, mid):
        self.id = mid


def test_duplicate_message_id_is_claimed_once():
    assert discord_bridge._claim_ingress(_Msg(12345)) is True
    assert discord_bridge._claim_ingress(_Msg(12345)) is False
    assert discord_bridge._claim_ingress(_Msg(99999)) is True


def test_ingress_admits_when_storage_is_down(monkeypatch):
    """Fails open: unable to deduplicate must never drop a real message."""
    monkeypatch.setattr(db, "connect", lambda: None)
    assert discord_bridge._claim_ingress(_Msg(1)) is True
    assert discord_bridge._claim_ingress(_Msg(1)) is True


def test_ingress_admits_when_the_storage_module_raises(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("storage exploded")

    monkeypatch.setattr(exchanges, "claim_ingress", _boom)
    assert discord_bridge._claim_ingress(_Msg(7)) is True


def test_dedup_is_wired_before_command_handling():
    """A redelivered '/new' must not rotate the session twice, so the claim has to come
    before command dispatch -- checked statically because on_message is defined inside
    start() and needs a live discord client to exercise."""
    import inspect

    src = inspect.getsource(discord_bridge)
    claim_at = src.index("if not _claim_ingress(message):")
    new_cmd_at = src.index('if text.lower() == "/new":')
    assert claim_at < new_cmd_at
