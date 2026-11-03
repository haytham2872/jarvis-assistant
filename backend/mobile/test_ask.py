"""/ask: the Action Button Shortcut's one request, a message in and the answer out (slice 1,
15 October 2026). Expo Go never received a deep link while it was open (M0), so the Action
Button runs an iOS Shortcut that talks to the server directly. The turn runs through
main.py's admission with a stub model; transcription is stubbed."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile.test_conversation import AUTH, _isolation, _main  # noqa: F401  (fixture)
from backend.mobile.test_routes import m4a


def _client(monkeypatch, answer="**Thursday at 2.** The notary confirmed it.", admit=True):
    M, calls = _main(monkeypatch, answer=lambda text: answer)
    from backend.mobile import conversation, routes
    if admit:      # admit as the server does: the turn runs as soon as the message is stored
        conversation.configure(admit=lambda: asyncio.get_running_loop().create_task(M._admit_pending()),
                               running=lambda: dict(M._running), new_chat=M.mobile_new_session)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), M, calls


@pytest.fixture
def heard(monkeypatch):
    from backend.audio import transcriber
    said = {"text": "when is the notary appointment"}
    monkeypatch.setattr(transcriber, "transcribe", lambda pcm: said["text"])
    return said


def test_a_dictated_question_comes_back_as_words_to_speak(monkeypatch):
    c, M, calls = _client(monkeypatch)
    r = c.post("/mobile/v1/ask", json={"text": "when is the notary appointment"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "answered" and body["heard"] == "when is the notary appointment"
    assert body["answer"] == "**Thursday at 2.** The notary confirmed it."
    assert body["say"] == "Thursday at 2. The notary confirmed it."       # no Markdown read aloud
    assert calls == [("when is the notary appointment", "mobile")]


def test_the_exchange_joins_the_phone_chat_and_jarvis_knows_it_will_be_heard(monkeypatch):
    c, M, _ = _client(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import inbox
    rid = c.post("/mobile/v1/ask", json={"text": "what's on today"}, headers=AUTH).json()["request_id"]
    assert inbox.get(conversation.message_key(rid))["context"]["spoken"] is True
    assert conversation.status(rid)["answer"]


def test_a_recording_is_transcribed_like_a_voice_message(monkeypatch, heard):
    c, M, calls = _client(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import inbox
    r = c.post("/mobile/v1/ask", content=m4a(1.2), headers={**AUTH, "Content-Type": "audio/x-m4a"})
    body = r.json()
    assert body["state"] == "answered" and body["heard"] == heard["text"]
    assert inbox.get(conversation.message_key(body["request_id"]))["context"]["input"] == "voice"


def test_nothing_heard_is_said_and_starts_no_turn(monkeypatch, heard):
    c, M, calls = _client(monkeypatch)
    heard["text"] = ""
    body = c.post("/mobile/v1/ask", content=m4a(1.0), headers={**AUTH, "Content-Type": "audio/mp4"}).json()
    assert body["state"] == "empty" and body["say"] == "I didn't hear anything." and calls == []
    body = c.post("/mobile/v1/ask", json={"text": "  "}, headers=AUTH).json()
    assert body["state"] == "empty" and calls == []


def test_a_slow_answer_says_where_it_will_be(monkeypatch):
    c, M, calls = _client(monkeypatch, admit=False)          # never admitted: still queued
    body = c.post("/mobile/v1/ask?wait=0", json={"text": "deep research please"}, headers=AUTH).json()
    assert body["state"] == "queued"
    assert body["say"] == "Still working on it. The answer will be in the app."


def test_a_long_answer_is_read_like_the_app_reads_it(monkeypatch):
    from backend.mobile import spoken
    long = "This is a sentence of the answer. " * 200
    c, M, _ = _client(monkeypatch, answer=long)
    body = c.post("/mobile/v1/ask", json={"text": "tell me everything"}, headers=AUTH).json()
    assert body["say"] == spoken.read_text(long)[0] and body["say"].endswith(spoken.REST_ON_SCREEN)


def test_an_undecodable_recording_is_still_an_error(monkeypatch):
    c, M, calls = _client(monkeypatch)
    r = c.post("/mobile/v1/ask", content=b"not audio at all" * 50, headers={**AUTH, "Content-Type": "audio/mp4"})
    assert r.status_code == 415 and calls == []
