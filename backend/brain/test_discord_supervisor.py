"""The Discord bridge keeps itself connected.

Live, 6 November 2026: the server restarted, the gateway connected 12 s in and became
ready ~3 s later, and the bridge's 15 s give-up closed it ("Discord bridge failed to
start ()" then discord.py's _MissingSentinel traceback). Nothing retried, so Discord
stayed off for a day while the phone app worked.
"""
import asyncio

import pytest

from backend.brain import discord_bridge as bridge


class FakeClient:
    """Stands in for discord.Client: becomes ready after `ready_after` s, or fails."""

    built: list["FakeClient"] = []

    def __init__(self, ready_after=0.05, fail=None, end_after=None):
        self.ready_after, self.fail, self.end_after = ready_after, fail, end_after
        self._ready = False
        self.closed = False
        self._done = asyncio.Event()
        FakeClient.built.append(self)

    def is_ready(self):
        return self._ready

    async def start(self, token):
        if self.fail:
            raise self.fail
        await asyncio.sleep(self.ready_after)
        self._ready = True
        if self.end_after is not None:
            await asyncio.sleep(self.end_after)
            raise ConnectionError("gateway gone")
        await self._done.wait()

    async def close(self):
        self.closed = True
        self._done.set()


@pytest.fixture
def bridge_env(monkeypatch):
    FakeClient.built = []
    plans: list[dict] = []

    def build():
        return FakeClient(**(plans.pop(0) if plans else {}))

    monkeypatch.setattr(bridge, "_BOT_TOKEN", "token")
    monkeypatch.setattr(bridge, "_ALLOWED_ID", "1")
    monkeypatch.setattr(bridge, "_build_client", build)
    monkeypatch.setattr(bridge, "_RETRY_DELAYS_S", (0.05, 0.05))
    monkeypatch.setattr(bridge._delivery, "reconcile_inflight", lambda: 0)

    async def no_resume():
        return None
    monkeypatch.setattr(bridge, "_resume_pending", no_resume)
    yield plans
    bridge._supervisor = None
    bridge._client = None
    bridge._client_task = None
    bridge._stopping = False
    bridge._link.update(state="off", error="", retry_at=0.0)


def run(coro):
    return asyncio.run(coro)


async def _wait(cond, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def test_a_slow_connection_is_not_closed(bridge_env, monkeypatch):
    """The 6 November shape: ready after start() stopped waiting."""
    monkeypatch.setattr(bridge, "_READY_TIMEOUT", 0.05)
    bridge_env.append({"ready_after": 0.3})

    async def scenario():
        await bridge.start()
        assert bridge._client is None              # start() returned before ready
        first = FakeClient.built[0]
        assert not first.closed                    # ...and left it connecting
        assert bridge.link_status() == "Discord: connecting"
        await _wait(lambda: bridge._client is first)
        assert bridge.link_status() == ""
        assert len(FakeClient.built) == 1
        await bridge.stop()
    run(scenario())


def test_a_failed_login_is_retried(bridge_env, monkeypatch):
    monkeypatch.setattr(bridge, "_READY_TIMEOUT", 0.05)
    bridge_env.extend([{"fail": OSError("network unreachable")}, {}])

    async def scenario():
        await bridge.start()
        await _wait(lambda: bridge._link["state"] == "down" or bridge._client is not None)
        if bridge._link["state"] == "down":
            assert "network unreachable" in bridge.link_status()
            assert bridge.link_status().startswith("Discord: DOWN")
        await _wait(lambda: bridge._client is not None)
        assert len(FakeClient.built) == 2
        await bridge.stop()
    run(scenario())


def test_a_lost_connection_reconnects(bridge_env, monkeypatch):
    bridge_env.extend([{"end_after": 0.6}, {}])

    async def scenario():
        await bridge.start()
        first = FakeClient.built[0]
        assert bridge._client is first
        await _wait(lambda: len(FakeClient.built) == 2 and bridge._client is FakeClient.built[1])
        assert first.closed
        await bridge.stop()
    run(scenario())


def test_stop_ends_reconnecting(bridge_env):
    async def scenario():
        await bridge.start()
        await bridge.stop()
        built = len(FakeClient.built)
        await asyncio.sleep(0.2)
        assert len(FakeClient.built) == built
        assert bridge._client is None
        assert bridge.link_status() == ""
        assert FakeClient.built[0].closed
    run(scenario())


def test_not_configured_starts_nothing(monkeypatch):
    monkeypatch.setattr(bridge, "_BOT_TOKEN", "")
    FakeClient.built = []
    monkeypatch.setattr(bridge, "_build_client", lambda: FakeClient())
    run(bridge.start())
    assert FakeClient.built == []
    assert bridge.link_status() == ""


def test_status_shows_a_down_link(monkeypatch):
    import time
    monkeypatch.setitem(bridge._link, "state", "down")
    monkeypatch.setitem(bridge._link, "error", "LoginFailure: bad token")
    monkeypatch.setitem(bridge._link, "retry_at", time.time() + 125)
    from backend.brain import runtime_status
    text = runtime_status.runtime_status_text()
    assert "Discord: DOWN (LoginFailure: bad token), retrying in 2 min" in text


def test_startup_notices_wait_for_a_slow_connection(bridge_env, monkeypatch):
    """Review finding: main.py's one-time startup notices waited only for start(), which now
    returns before a slow connection finishes; they would have been lost."""
    monkeypatch.setattr(bridge, "_READY_TIMEOUT", 0.05)
    bridge_env.append({"ready_after": 0.4})

    async def scenario():
        await bridge.start()
        assert bridge._client is None
        assert await bridge.wait_connected(timeout=3.0) is True
        assert bridge._client is not None
        await bridge.stop()
        assert await bridge.wait_connected(timeout=0.5) is False   # stopped: not waited on
    run(scenario())
