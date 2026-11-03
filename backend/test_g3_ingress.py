"""G3 (D3, D11, N5): every accepted message has a durable, correctly targeted outcome.

docs/archive/GOALS-ROADMAP.md section 13, G3: "Durable insertion precedes acknowledgement; no silently
dropped fourth message." Before G3 the steering queue lived in memory with maxlen=3, the
Discord ack went out before anything was durable, and a Discord message that arrived during
a desk turn was queued for some later Discord turn while being told "I'll factor that in".
"""
import asyncio
from types import SimpleNamespace as NS

import pytest


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "TINYFISH_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("NO_TTS", "1")
    from backend.brain import steering, engine
    from backend.tools import vault
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    steering._reset_memory()
    engine._reset_turn_stats()
    yield
    steering._reset_memory()
    engine._reset_turn_stats()


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ the queue itself

def test_a_burst_of_four_loses_nothing():
    """D3: maxlen=3 silently evicted the oldest correction."""
    from backend.brain import steering
    for i in range(4):
        steering.push("discord", f"message {i}")
    assert steering.drain("discord") == [f"message {i}" for i in range(4)]


def test_an_acknowledged_message_survives_a_restart():
    """D3: the queue was memory only, so a restart after the ack lost the message."""
    from backend.brain import steering
    assert steering.push("discord", "also check the fees") > 0
    steering._reset_memory()                       # the process died; memory is gone
    assert steering.drain("discord") == ["also check the fees"]


def test_the_same_discord_message_is_queued_once():
    from backend.brain import steering
    a = steering.push("discord", "hello", key="111")
    b = steering.push("discord", "hello", key="111")
    assert a > 0 and b == a
    assert steering.drain("discord") == ["hello"]


def test_a_server_channel_message_does_not_steer_the_dm_turn():
    """N5: DM and server channels shared one generic 'discord' queue."""
    from backend.brain import steering
    steering.push("discord", "for the dm", conversation="discord:dm", target_run="r1")
    steering.push("discord", "for the server", conversation="discord:42", target_run=None)
    with steering.turn("discord:dm", "r1"):
        assert steering.drain("discord") == ["for the dm"]
    assert [r["text"] for r in steering.pending_turns()] == ["for the server"]


# ------------------------------------------------------------------ admission (main.py)

def _wire(monkeypatch, once):
    import backend.main as M
    from backend.brain import long_reply, skills

    async def shape(reply, text):
        return reply
    monkeypatch.setattr(M, "_execute_turn_once", once)
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    monkeypatch.setattr(long_reply, "shape_for_discord", shape)
    monkeypatch.setattr(skills, "pop_footer", lambda channel: "")
    delivered = []

    async def answer(context, text, run_turn):
        response = await run_turn(text)
        delivered.append((context.get("message_id"), response))
        return response
    monkeypatch.setattr(M.discord_bridge, "answer_admitted", answer)
    return M, delivered


def _ingress(message_id, channel="dm"):
    """What the bridge sets while it hands a message to run_pipeline_from_discord."""
    from backend.brain import discord_context, discord_bridge
    ctx = discord_context.TurnContext(str(message_id))
    chan = NS(id=900 if channel == "dm" else int(channel), guild=None if channel == "dm" else NS(id=1))
    return discord_context.current.set(ctx), discord_bridge.current_channel.set(chan)


def test_a_discord_message_during_a_desk_turn_is_answered_on_its_own(monkeypatch):
    """D11: it was queued for some later Discord turn while told 'I'll factor that in'."""
    import backend.main as M_
    started = asyncio.Event()
    release = asyncio.Event()

    async def once(text, channel="local", want_tts=True):
        if channel == "local":
            started.set()
            await release.wait()
            return "DESK ANSWER"
        return f"ANSWER TO {text}"
    M, delivered = _wire(monkeypatch, once)

    async def go():
        desk = asyncio.create_task(M.run_pipeline_from_text("plan my week"))
        await started.wait()
        _ingress(555)
        ack = await M.run_pipeline_from_discord("what's NVDA at?")
        assert "factor that in" not in ack           # it never joins a desk turn
        release.set()
        await desk
        await M._admit_pending()
        return ack
    run(go())
    assert delivered == [("555", "ANSWER TO what's NVDA at?")]


def test_a_followup_gets_its_own_reply(monkeypatch):
    """G3 replaces G0's joined reply: each message keeps its own response identity."""
    calls = []
    from backend.brain import steering

    async def once(text, channel="local", want_tts=True):
        calls.append(text)
        if len(calls) == 1:
            steering.push("discord", "also, what time is it?", key="222",
                          conversation="discord:dm", target_run=M._running.get("run_id"),
                          context={"message_id": "222", "channel_id": 900})
        return f"ANSWER {len(calls)}"
    M, delivered = _wire(monkeypatch, once)

    async def go():
        _ingress(111)
        first = await M.run_pipeline_from_discord("weather tomorrow?")
        await M._admit_pending()
        return first
    first = run(go())
    assert first == "ANSWER 1"                       # only its own answer
    assert delivered == [("222", "ANSWER 2")]


def test_no_ack_without_a_durable_insert(monkeypatch):
    """Ack before insert is the mutation G3 must catch: with storage down the message
    waits for the lock instead of being acknowledged and lost."""
    from backend.brain import steering
    monkeypatch.setattr(steering, "_store_insert", lambda **kw: 0)
    started, release = asyncio.Event(), asyncio.Event()

    async def once(text, channel="local", want_tts=True):
        if text == "first":
            started.set()
            await release.wait()
        return f"ANSWER TO {text}"
    M, delivered = _wire(monkeypatch, once)

    async def go():
        _ingress(1)
        a = asyncio.create_task(M.run_pipeline_from_discord("first"))
        await started.wait()
        _ingress(2)
        b = asyncio.create_task(M.run_pipeline_from_discord("second"))
        await asyncio.sleep(0.05)
        assert not b.done()                          # no ack went out
        release.set()
        return await a, await b
    assert run(go()) == ("ANSWER TO first", "ANSWER TO second")


def test_a_failed_admitted_turn_is_held_not_lost(monkeypatch):
    from backend.brain import steering
    steering.push("discord", "will fail", key="7", conversation="discord:dm",
                  context={"message_id": "7", "channel_id": 900})
    steering.push("discord", "will work", key="8", conversation="discord:dm",
                  context={"message_id": "8", "channel_id": 900})

    async def once(text, channel="local", want_tts=True):
        return None if text == "will fail" else "OK"
    M, delivered = _wire(monkeypatch, once)
    run(M._admit_pending())
    states = {r["message_key"]: r["state"] for r in steering._all_rows()}
    assert states == {"7": "failed", "8": "processed"}
    assert ("7", None) in delivered and ("8", "OK") in delivered


def test_a_message_absorbed_into_a_cut_off_turn_is_reported_not_rerun():
    from backend.brain import steering
    steering.push("discord", "also the fees", key="9", conversation="discord:dm",
                  target_run="r9", context={"message_id": "9"})
    with steering.turn("discord:dm", "r9"):
        assert steering.drain("discord") == ["also the fees"]
    interrupted = steering.recover()                  # the process died mid-turn
    assert [r["message_key"] for r in interrupted] == ["9"]
    assert steering.pending_turns() == []            # never rerun on a guess


def test_the_drain_takes_only_messages_aimed_at_the_running_turn():
    """Same conversation, other owners: a message aimed at an earlier run, or at no run (its
    own turn), is never folded into this one."""
    from backend.brain import steering
    steering.push("discord", "for the old turn", key="a", conversation="discord:dm", target_run="r0")
    steering.push("discord", "its own turn", key="b", conversation="discord:dm")
    steering.push("discord", "for this turn", key="c", conversation="discord:dm", target_run="r1")
    with steering.turn("discord:dm", "r1"):
        assert steering.drain("discord") == ["for this turn"]
    states = {r["message_key"]: r["state"] for r in steering._all_rows()}
    assert states == {"a": "pending", "b": "pending", "c": "claimed"}


def test_a_completed_turn_closes_the_messages_it_absorbed(monkeypatch):
    """Through the real _execute_turn_once: what the turn drained is processed when the turn
    completes, so a restart never reports it as cut off."""
    import backend.main as M
    from backend.brain import steering

    async def fake_chat(text, history, send, sentence_queue=None, channel="local",
                        history_bridge="", history_since=None):
        steering.push("local", "a correction", conversation="local",
                      target_run=M._running.get("run_id"))
        assert steering.drain("local") == ["a correction"]
        return "ANSWER"
    monkeypatch.setattr(M, "chat", fake_chat)
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    monkeypatch.setitem(M.conversation_histories, "local", [])
    monkeypatch.setitem(M.history_times, "local", [])

    async def go():
        async with M._pipeline_lock:
            return await M._execute_turn_once("the question", channel="local", want_tts=False)
    assert run(go()) == "ANSWER"
    assert [r["state"] for r in steering._all_rows()] == ["processed"]
    assert steering.recover() == []
