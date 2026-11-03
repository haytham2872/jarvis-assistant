"""The "typing…" indicator must not delay the turn.

discord.py's `async with channel.typing()` waits for Discord to accept the typing request
before the body runs. On the server the stage between a message's arrival and its turn
starting had a 0.31s median (scripts/measure_discord_speed.py), most of it that request.
The indicator is now started alongside the turn instead of in front of it.
"""
import asyncio
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.brain import discord_bridge as bridge


def run(coro):
    return asyncio.run(coro)


class SlowTypingChannel:
    """typing() takes `delay` to be accepted, like a Discord API round trip."""
    def __init__(self, delay=0.3, fail=False):
        self.delay, self.fail = delay, fail
        self.events = []

    def typing(self):
        chan = self

        @asynccontextmanager
        async def cm():
            await asyncio.sleep(chan.delay)
            if chan.fail:
                raise RuntimeError("typing endpoint down")
            chan.events.append(("typing on", time.perf_counter()))
            try:
                yield
            finally:
                chan.events.append(("typing off", time.perf_counter()))
        return cm()

    async def send(self, text):
        self.events.append(("sent", time.perf_counter()))


@pytest.fixture
def client(monkeypatch):
    fake_discord = SimpleNamespace(
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        Client=type("Client", (), {"__init__": lambda self, intents: None}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    c = bridge._build_client()
    c.user = SimpleNamespace(id=77)
    monkeypatch.setattr(bridge, "_claim_ingress", lambda _: True)
    monkeypatch.setattr(bridge, "_ALLOWED_ID", "123")
    monkeypatch.setattr(bridge, "_approval_reply_handler", None)
    monkeypatch.setattr(bridge, "_client", SimpleNamespace(user=SimpleNamespace(id=77)))
    return c


def _message(channel, text="what time is it"):
    return SimpleNamespace(id=20, author=SimpleNamespace(id=123), content=text,
                           reference=None, attachments=[], channel=channel)


def _wire(monkeypatch, channel, turn_seconds=0.0):
    started = {}

    async def handler(text):
        started["at"] = time.perf_counter()
        await asyncio.sleep(turn_seconds)
        return "answered"

    async def send_reply(chan, text, **_):
        chan.events.append(("sent", time.perf_counter()))
        return True

    monkeypatch.setattr(bridge, "_turn_handler", handler)
    monkeypatch.setattr(bridge, "_send_reply", send_reply)
    return started


def test_turn_starts_without_waiting_for_the_typing_request(client, monkeypatch):
    # The bound also covers on_message's own setup, which took 0.14s on a busy machine, so it
    # is loose; waiting for the request would take the whole delay. The delay stays under the
    # bridge's 1s wait for a request in flight, so the indicator still lands before the reply.
    channel = SlowTypingChannel(delay=0.8)
    started = _wire(monkeypatch, channel, turn_seconds=0.5)
    t0 = time.perf_counter()
    run(client.on_message(_message(channel)))
    assert started["at"] - t0 < 0.5                     # was >= the delay before the fix
    kinds = [e[0] for e in channel.events]
    assert kinds == ["typing on", "typing off", "sent"]  # still shown, and over before the reply


def test_a_fast_turn_does_not_send_the_reply_before_the_indicator_lands(client, monkeypatch):
    # Otherwise the typing request would arrive after the reply and show "typing…" again
    # for ten seconds under an answer that is already there.
    channel = SlowTypingChannel(delay=0.2)
    _wire(monkeypatch, channel, turn_seconds=0.0)
    run(client.on_message(_message(channel)))
    kinds = [e[0] for e in channel.events]
    assert kinds.index("typing on") < kinds.index("sent")


def test_a_failing_typing_request_does_not_break_the_turn(client, monkeypatch):
    channel = SlowTypingChannel(delay=0.01, fail=True)
    started = _wire(monkeypatch, channel, turn_seconds=0.05)
    run(client.on_message(_message(channel)))
    assert "at" in started
    assert [e[0] for e in channel.events] == ["sent"]


def test_a_hung_typing_request_never_holds_the_reply_long(client, monkeypatch):
    channel = SlowTypingChannel(delay=30)
    _wire(monkeypatch, channel, turn_seconds=0.0)
    t0 = time.perf_counter()
    run(client.on_message(_message(channel)))
    assert time.perf_counter() - t0 < 2.0
    assert [e[0] for e in channel.events] == ["sent"]


def test_a_hung_typing_request_is_cancelled_not_left_to_land_later(client, monkeypatch):
    channel = SlowTypingChannel(delay=30)
    _wire(monkeypatch, channel, turn_seconds=0.0)

    async def go():
        await client.on_message(_message(channel))
        await asyncio.sleep(0.05)         # let the cancellation run
        return [t for t in bridge._typing_tasks if not t.done()]

    assert run(go()) == []                # nothing left running to land after the reply


def test_every_turn_entry_point_uses_the_non_blocking_indicator():
    import inspect
    src = inspect.getsource(bridge)
    # `async with ...typing():` waits for Discord before the body runs; only the helper may.
    assert src.count(".typing():") == 1
    assert "async with _typing(" in src
