"""A turn tells recall where its live history begins, so past exchanges already in it are
not recalled again (threads.exchange_candidates, exclude_since).

Replaying 40 Discord messages on the server (2026-10-11): 31 of 40 not-deep turns carried
~1,000 chars of exchanges repeating the live history, and 42% of deep turns' exchange text
came from the current conversation.
"""
import asyncio
import time

import pytest

import backend.main as m


@pytest.fixture
def discord_history(monkeypatch):
    for d in (m.conversation_histories, m.history_bridges, m.history_times):
        monkeypatch.setitem(d, "discord", [])
    yield


def test_history_since_is_the_oldest_turn(discord_history):
    assert m._history_since("discord") is None
    m._append_history("discord", "first", "a", started=100.0)
    m._append_history("discord", "second", "b", started=200.0)
    assert m._history_since("discord") == 100.0


def test_history_times_record_when_the_turn_started(discord_history):
    m._append_history("discord", "q", "a", started=123.0)
    assert m.history_times["discord"] == [123.0, 123.0]
    before = time.time()
    m._append_history("discord", "q2", "a2")            # no start given: now, as before
    assert m.history_times["discord"][-1] >= before


def test_the_turn_hands_its_boundary_to_chat_and_stamps_its_start(discord_history, monkeypatch):
    m._append_history("discord", "earlier", "reply", started=50.0)
    seen = {}

    async def fake_chat(text, history, send, sentence_queue=None, channel="local",
                        history_bridge="", history_since=None):
        seen["history_since"] = history_since
        seen["called_at"] = time.time()
        await asyncio.sleep(0.05)           # a reply takes time: appending after it is later
        return "answer"

    monkeypatch.setattr(m, "chat", fake_chat)
    start = time.time()
    assert asyncio.run(m._execute_turn_once("now", channel="discord", want_tts=False)) == "answer"
    assert seen["history_since"] == 50.0
    stamped = m.history_times["discord"][-1]
    # Taken before chat() ran, so the exchange chat() records is never older than its entry.
    assert start <= stamped <= seen["called_at"]


def test_chat_passes_the_boundary_and_channel_to_recall():
    import ast
    import inspect
    import textwrap
    from backend.brain import engine
    tree = ast.parse(textwrap.dedent(inspect.getsource(engine.chat)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "recall"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("history_since"), ast.Name) and kw["history_since"].id == "history_since"
    assert isinstance(kw.get("channel"), ast.Name) and kw["channel"].id == "channel"


def test_the_recall_check_points_at_the_conversation_first():
    from backend.brain import personality
    assert "in the conversation above, answer from that" in personality._RECALL_PRECISION_FRAME
