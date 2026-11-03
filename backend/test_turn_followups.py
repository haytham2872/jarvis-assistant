"""A Discord answer must survive a second message that arrives mid-turn.

G0 / D1 (reproduced on e2467c3): Sam asks something, sends a second message while the
turn runs (no tool calls, so no mid-loop seam), and `_execute_turn` ran the follow-up as the
next turn -- then returned only the follow-up's answer. G0 joined the answers into one reply.

G3 (2026-10-06) replaces the join: each message keeps its own turn and its own reply. The
first answer is the reply to the first message, and the second message is answered on its
own, by `_admit_pending`, under its own delivery key. These tests pin that contract.
"""
import asyncio

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


def _discord(monkeypatch, once):
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
        delivered.append((context.get("message_id"), text, response))
        return response
    monkeypatch.setattr(M.discord_bridge, "answer_admitted", answer)
    return M, delivered


def _followup(M, text, key):
    """A message that arrived during the running turn, aimed at it."""
    from backend.brain import steering
    steering.push("discord", text, key=key, conversation="discord:dm",
                  target_run=M._running.get("run_id"),
                  context={"message_id": key, "channel_id": 900})


def _go(M, first):
    async def go():
        reply = await M.run_pipeline_from_discord(first)
        await M._admit_pending()
        return reply
    return asyncio.run(go())


def test_first_reply_survives_a_followup(monkeypatch):
    calls = []

    async def once(text, channel="local", want_tts=True):
        calls.append(text)
        if len(calls) == 1:
            _followup(M, "also, what time is it?", "2")
            return "ANSWER TO THE FIRST QUESTION"
        return "ANSWER TO THE FOLLOW-UP"
    M, delivered = _discord(monkeypatch, once)
    reply = _go(M, "weather tomorrow?")
    assert reply == "ANSWER TO THE FIRST QUESTION"
    assert delivered == [("2", "also, what time is it?", "ANSWER TO THE FOLLOW-UP")]


def test_two_followups_keep_every_answer_in_order(monkeypatch):
    calls = []

    async def once(text, channel="local", want_tts=True):
        calls.append(text)
        if len(calls) == 1:
            _followup(M, "second question", "2")
        if len(calls) == 2:
            _followup(M, "third question", "3")
        return f"ANSWER {len(calls)}"
    M, delivered = _discord(monkeypatch, once)
    assert _go(M, "first question") == "ANSWER 1"
    assert [(k, r) for k, _t, r in delivered] == [("2", "ANSWER 2"), ("3", "ANSWER 3")]


def test_a_failed_followup_never_erases_the_first_answer(monkeypatch):
    from backend.brain import steering
    calls = []

    async def once(text, channel="local", want_tts=True):
        calls.append(text)
        if len(calls) == 1:
            _followup(M, "and the week after?", "2")
            return "THE FIRST ANSWER"
        return None                                    # the follow-up turn errored
    M, delivered = _discord(monkeypatch, once)
    assert _go(M, "weather tomorrow?") == "THE FIRST ANSWER"
    assert delivered == [("2", "and the week after?", None)]   # the bridge says so
    assert {r["message_key"]: r["state"] for r in steering._all_rows()} == {"2": "failed"}


def test_a_failed_first_turn_still_answers_the_followup(monkeypatch):
    """G0 ran no follow-up after a failed first turn, which left the second message
    unanswered with nothing said; G3 answers it on its own."""
    calls = []

    async def once(text, channel="local", want_tts=True):
        calls.append(text)
        if len(calls) == 1:
            _followup(M, "follow-up", "2")
            return None
        return "FOLLOW-UP ANSWER"
    M, delivered = _discord(monkeypatch, once)
    assert _go(M, "q") == "…"
    assert delivered == [("2", "follow-up", "FOLLOW-UP ANSWER")]


def test_an_ordinary_answer_is_unchanged(monkeypatch):
    async def once(text, channel="local", want_tts=True):
        return "JUST THE ANSWER"
    M, delivered = _discord(monkeypatch, once)
    assert asyncio.run(M.run_pipeline_from_discord("q")) == "JUST THE ANSWER"
    assert delivered == []


def test_history_holds_each_turn_once_and_nothing_is_spoken_twice(monkeypatch):
    """Through the real _execute_turn_once: each turn is appended to history once with its
    own answer, and nothing is spoken twice."""
    import backend.main as M
    from backend.brain import steering
    replies = iter(["FIRST", "SECOND"])
    spoken: list[str] = []

    async def fake_chat(text, history, send, sentence_queue=None, channel="local",
                        history_bridge="", history_since=None):
        answer = next(replies)
        if answer == "FIRST":
            steering.push("local", "a correction", conversation="local",
                          target_run=M._running.get("run_id"))
        await sentence_queue.put(answer)
        return answer

    class _TTS:
        async def speak_from_queue(self, q, on_state_cb=None, on_amplitude=None):
            while (item := await q.get()) is not None:
                spoken.append(item)

    monkeypatch.setattr(M, "chat", fake_chat)
    monkeypatch.setattr(M, "_no_tts", False)
    monkeypatch.setattr(M, "get_tts", lambda: _TTS())
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    monkeypatch.setitem(M.conversation_histories, "local", [])
    monkeypatch.setitem(M.history_times, "local", [])

    async def go():
        async with M._pipeline_lock:
            out = await M._execute_turn("the question", channel="local", want_tts=True)
        await M._admit_pending()
        return out
    out = asyncio.run(go())
    hist = M.conversation_histories["local"]
    assert [m["content"] for m in hist] == ["the question", "FIRST", "a correction", "SECOND"]
    assert spoken == ["FIRST", "SECOND"]
    assert out == "FIRST"
