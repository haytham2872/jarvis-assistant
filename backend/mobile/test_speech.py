"""Phone speech engines: Chirp 3 HD over the pooled client, edge-tts as the key-less path.

Google is never called: the gateway's pooled client is swapped for one on an httpx
MockTransport that records what would have been sent.
"""
import asyncio
import base64
import json

import httpx
import pytest

from backend.mobile import speech
from backend.providers import gateway

KEY = "AIza-test-" + "k" * 29


def fake_google(monkeypatch, handler):
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(gateway, "get_http_client", lambda: client)
    return seen


def ok(request):
    return httpx.Response(200, json={"audioContent": base64.b64encode(b"ID3-mp3-bytes").decode()})


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GOOGLE_TTS_API_KEY", "MOBILE_TTS_ENGINE", "MOBILE_TTS_VOICE"):
        monkeypatch.delenv(name, raising=False)


def test_chirp_is_chosen_when_the_key_is_set_and_edge_otherwise(monkeypatch):
    assert speech.engine() == "edge"
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", KEY)
    assert speech.engine() == "chirp"
    monkeypatch.setenv("MOBILE_TTS_ENGINE", "edge")
    assert speech.engine() == "edge"


def test_chirp_sends_the_key_as_a_header_never_in_the_url(monkeypatch):
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", KEY)
    seen = fake_google(monkeypatch, ok)
    audio, engine = asyncio.run(speech.synthesize("Good evening, Sam."))
    assert (audio, engine) == (b"ID3-mp3-bytes", "chirp")
    req = seen[0]
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)
    body = json.loads(req.content)
    assert body["voice"] == {"languageCode": "en-US", "name": "en-US-Chirp3-HD-Fenrir"}
    assert body["audioConfig"] == {"audioEncoding": "MP3"}
    assert body["input"] == {"text": "Good evening, Sam."}


def test_the_language_follows_the_voice(monkeypatch):
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", KEY)
    seen = fake_google(monkeypatch, ok)
    asyncio.run(speech.chirp_mp3("Bonsoir", "fr-FR-Chirp3-HD-Charon"))
    assert json.loads(seen[0].content)["voice"]["languageCode"] == "fr-FR"


@pytest.mark.parametrize("voice", ["en-GB-Wavenet-B", "../x", "fr-FR-Chirp3-HD-charon", "en-GB-Chirp3-HD-"])
def test_a_voice_chirp_could_not_have_is_refused_before_any_request(monkeypatch, voice):
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", KEY)
    seen = fake_google(monkeypatch, ok)
    with pytest.raises(speech.BadVoice):
        asyncio.run(speech.chirp_mp3("hi", voice))
    assert seen == []


def test_a_google_refusal_is_a_speech_error_without_the_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", KEY)
    fake_google(monkeypatch, lambda r: httpx.Response(
        403, json={"error": {"message": "Cloud Text-to-Speech API has not been used in project 123"}}))
    with pytest.raises(speech.SpeechError) as exc:
        asyncio.run(speech.chirp_mp3("hi"))
    assert "403" in str(exc.value) and "not been used" in str(exc.value)
    assert KEY not in str(exc.value)


def test_a_network_failure_or_empty_answer_is_a_speech_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", KEY)

    def boom(request):
        raise httpx.ConnectError("no route")
    fake_google(monkeypatch, boom)
    with pytest.raises(speech.SpeechError):
        asyncio.run(speech.chirp_mp3("hi"))
    fake_google(monkeypatch, lambda r: httpx.Response(200, json={}))
    with pytest.raises(speech.SpeechError):
        asyncio.run(speech.chirp_mp3("hi"))


def test_chirp_without_a_key_says_so(monkeypatch):
    monkeypatch.setenv("MOBILE_TTS_ENGINE", "chirp")
    with pytest.raises(speech.SpeechError, match="GOOGLE_TTS_API_KEY"):
        asyncio.run(speech.synthesize("hi"))


def test_a_key_mangled_by_copy_paste_is_named_not_sent(monkeypatch):
    # The server's key had its hyphens turned into U+2011 dashes by a browser paste.
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", "AIzaSyAf‑Qnp‑U_" + "k" * 20)
    seen = fake_google(monkeypatch, ok)
    with pytest.raises(speech.SpeechError, match="not plain ASCII"):
        asyncio.run(speech.chirp_mp3("hi"))
    assert seen == []
