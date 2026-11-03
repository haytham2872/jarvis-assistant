"""M2: a voice message from the phone. The recording is a real 16 kHz mono AAC file;
transcription is stubbed (no model loads), the turn runs through main.py's admission."""
import asyncio
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile.test_conversation import AUTH, _isolation, _main, run  # noqa: F401  (fixture)
from backend.mobile.test_routes import m4a


class Heard(list):
    """The stub transcriber's calls (a list), and the text it will return."""
    text = "remind me at nine to call the notary"


@pytest.fixture
def heard(monkeypatch):
    from backend.audio import transcriber
    calls = Heard()

    def fake(pcm):
        calls.append(pcm.size)
        return calls.text
    monkeypatch.setattr(transcriber, "transcribe", fake)
    return calls


def _client(monkeypatch):
    M, calls = _main(monkeypatch)
    from backend.mobile import routes
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), M, calls


def post(c, rid, audio):
    return c.post(f"/mobile/v1/recordings?request_id={rid}", content=audio,
                  headers={**AUTH, "Content-Type": "audio/mp4"})


def test_a_voice_message_is_transcribed_submitted_and_answered_as_voice(monkeypatch, heard):
    c, M, calls = _client(monkeypatch)
    from backend.brain import turn_input
    from backend.mobile import conversation
    from backend.storage import inbox
    seen_voice = []

    async def chat(text, history, send, sentence_queue=None, channel="local", **kw):
        seen_voice.append(turn_input.voice.get())
        return "Done: 9:00."
    monkeypatch.setattr(M, "chat", chat)
    r = post(c, "req-v0000001", m4a(1.5))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["transcript"] == heard.text and body["status"]["state"] == "queued"
    assert body["stt"]["duration_s"] > 1.0
    assert inbox.get("mobile:req-v0000001")["context"]["input"] == "voice"
    conversation.submit("req-t0000001", "and a typed one")      # answered in the same pass
    run(M._admit_pending())
    # The voice turn knows its text is a transcript; the typed turn after it does not.
    assert seen_voice == [True, False]
    assert conversation.status("req-v0000001")["answer"] == "Done: 9:00."


def test_a_retried_upload_is_not_transcribed_or_run_twice(monkeypatch, heard):
    c, M, calls = _client(monkeypatch)
    post(c, "req-v0000002", m4a(1.0))
    run(M._admit_pending())
    again = post(c, "req-v0000002", m4a(1.0))
    run(M._admit_pending())
    assert again.status_code == 202 and again.json()["stt"] is None
    assert len(heard) == 1 and len(calls) == 1


def test_a_recording_with_nothing_heard_creates_no_turn(monkeypatch, heard):
    c, M, calls = _client(monkeypatch)
    from backend.storage import mobile_requests
    heard.text = "   "
    r = post(c, "req-v0000003", m4a(1.0))
    assert r.status_code == 422
    run(M._admit_pending())
    assert calls == [] and mobile_requests.get("req-v0000003") is None


def test_a_bad_request_id_is_refused_before_decoding(monkeypatch, heard):
    c, M, calls = _client(monkeypatch)
    r = post(c, "bad id", m4a(1.0))
    assert r.status_code == 400 and heard == []


def test_the_prompt_tells_jarvis_a_phone_voice_message_is_a_transcript():
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert 'channel == "mobile" and _turn_input.voice.get()' in src
    assert 'getattr(target_ctx, "voice", False)' in src       # Discord's voice notes unchanged


def test_warm_loads_the_model_once_even_if_tapped_twice(monkeypatch):
    c, M, calls = _client(monkeypatch)
    from backend.audio import transcriber
    loads = []
    state = {"loaded": False}
    monkeypatch.setattr(transcriber, "engine", lambda: "parakeet" if state["loaded"] else "")

    def warm():
        loads.append(1)
        state["loaded"] = True
        return True
    monkeypatch.setattr(transcriber, "warm", warm)
    from backend.mobile import routes
    monkeypatch.setattr(routes, "_warming", True)          # a load is already under way
    assert c.post("/mobile/v1/warm", headers=AUTH).json() == {"loaded": False}
    assert loads == []                                    # no second load, no blocked thread
    monkeypatch.setattr(routes, "_warming", False)
    assert c.post("/mobile/v1/warm", headers=AUTH).json() == {"loaded": False}
    import time
    for _ in range(50):
        if loads:
            break
        time.sleep(0.02)
    assert loads == [1]
