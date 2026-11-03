"""Stop (20 October 2026): a voice note transcribed as "St. Wam" sent JARVIS into one model
call that reasoned for six and a half minutes. Nothing could stop it: a message waits behind
the running answer, and the only stop (the desk's interrupt) cancelled the task the turn ran
in, which for a phone message is the loop that answers every phone message. Sam had to
restart the server twice."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


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


def _turns(monkeypatch):
    """The real _execute_turn_once, with a chat that hangs on "think forever"."""
    import backend.main as M
    started = asyncio.Event()

    async def fake_chat(text, history, send, sentence_queue=None, channel="local",
                        history_bridge="", history_since=None):
        if text == "think forever":
            started.set()
            await asyncio.sleep(3600)
        return f"ANSWER to {text}"
    monkeypatch.setattr(M, "chat", fake_chat)
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    monkeypatch.setitem(M.conversation_histories, "local", [])
    monkeypatch.setitem(M.history_times, "local", [])
    return M, started


def test_stop_ends_the_answer_and_the_loop_that_ran_it_carries_on(monkeypatch):
    """Like the phone's admission loop: one task answers message after message."""
    M, started = _turns(monkeypatch)
    from backend.storage import runs

    async def admission_loop():
        out = []
        for text in ("think forever", "next message"):
            async with M._pipeline_lock:
                out.append(await M._execute_turn_once(text, channel="local", want_tts=False))
        return out

    async def go():
        loop = asyncio.create_task(admission_loop())
        await started.wait()
        assert M.stop_turn() is True
        return await loop
    assert asyncio.run(go()) == [None, "ANSWER to next message"]
    from backend.storage import db
    states = [row[0] for row in db.connect().execute("SELECT state FROM runs ORDER BY started_at")]
    assert states == [runs.INTERRUPTED, runs.COMPLETED]


def test_stop_with_nothing_running_does_nothing(monkeypatch):
    M, _ = _turns(monkeypatch)
    assert M.stop_turn() is False


def test_discord_stop_words():
    from backend.brain import discord_bridge as D
    calls = []
    D.set_stop_handler(lambda: calls.append(1) or True)
    try:
        assert D.stop_reply("Stop everything now.") == "Stopped."
        assert D.stop_reply("/stop") == "Stopped."
        assert D.stop_reply("stop researching the weather") is None    # a sentence, not a stop
        D.set_stop_handler(lambda: False)
        assert D.stop_reply("/stop") == "Nothing is running right now."
        assert D.stop_reply("stop") is None                             # nothing running: ordinary
    finally:
        D.set_stop_handler(None)
    assert len(calls) == 2


def test_the_phone_stop_button(monkeypatch):
    from backend.mobile import conversation, routes
    monkeypatch.setenv("MOBILE_TOKEN", "stop-fixture")
    monkeypatch.setattr(conversation, "_stop", lambda: True)
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as c:
        assert c.post("/mobile/v1/stop").status_code == 401
        r = c.post("/mobile/v1/stop", headers={"Authorization": "Bearer stop-fixture"})
        assert r.status_code == 200 and r.json() == {"stopped": True}
