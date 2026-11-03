"""The phone routes outside the conversation: token gating, ping, recording decoding, and the
activity view with pause and resume (slice 1, 15 October 2026).

A minimal app holding only the mobile router, so nothing of the backend's lifespan starts.
The recording is a real 16 kHz mono AAC .m4a made with PyAV (the format the phone sends);
transcription is stubbed, so no model loads. The M0 probe routes (echo socket, probe
recording, probe speech) were removed with the probe screen; their decoding checks now run
against /recordings, which uses the same decoder.
"""
import io
import math
import pathlib

import av
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile import routes

TOKEN = "mobile-" + "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def m4a(seconds: float = 1.5, rate: int = 16000) -> bytes:
    """A real AAC-in-MP4 recording, mono, like expo-audio's 16 kHz preset."""
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="ipod") as out:
        stream = out.add_stream("aac", rate=rate, layout="mono")
        n = int(seconds * rate)
        tone = (np.sin(2 * math.pi * 440 * np.arange(n) / rate) * 0.3 * 32767).astype(np.int16)
        frame_size = 1024
        for start in range(0, n, frame_size):
            frame = av.AudioFrame.from_ndarray(tone[start:start + frame_size].reshape(1, -1),
                                               format="s16", layout="mono")
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


# --- gating --------------------------------------------------------------------------------

REC = "/mobile/v1/recordings?request_id=req-r0000001"
AUDIO = {**AUTH, "Content-Type": "audio/mp4"}


def test_every_route_is_absent_without_a_token(monkeypatch):
    monkeypatch.delenv("MOBILE_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(routes.router)
    c = TestClient(app)
    assert c.get("/mobile/v1/ping", headers=AUTH).status_code == 404
    assert c.post(REC, content=b"x", headers=AUTH).status_code == 404
    assert c.get("/mobile/v1/activity", headers=AUTH).status_code == 404
    assert c.post("/mobile/v1/ask", json={"text": "hi"}, headers=AUTH).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_a_missing_or_wrong_token_is_refused(client, headers):
    assert client.get("/mobile/v1/ping", headers=headers).status_code == 401
    assert client.post(REC, content=m4a(), headers=headers).status_code == 401
    assert client.get("/mobile/v1/activity", headers=headers).status_code == 401
    assert client.post("/mobile/v1/pause", json={}, headers=headers).status_code == 401
    assert client.post("/mobile/v1/resume", headers=headers).status_code == 401
    assert client.post("/mobile/v1/ask", json={"text": "hi"}, headers=headers).status_code == 401


def test_the_probe_routes_are_gone(client):
    assert client.post("/mobile/v1/probe/recording", content=m4a(), headers=AUDIO).status_code == 404
    assert client.get("/mobile/v1/probe/speech", params={"text": "hi"}, headers=AUTH).status_code == 404


# --- ping ----------------------------------------------------------------------------------

def test_ping_answers_with_the_server_clock_and_version(client):
    body = client.get("/mobile/v1/ping", headers=AUTH).json()
    assert body["schema_version"] == 1
    assert body["server_time"] > 1.7e9
    assert body["version"]


# --- recording decoding (shared by /recordings and /ask) -----------------------------------

def test_a_16khz_mono_aac_recording_is_decoded(tmp_path):
    path = tmp_path / "r.m4a"
    path.write_bytes(m4a(1.5))
    facts = routes._inspect(str(path), False)
    assert facts["codec"] == "aac"
    assert facts["sample_rate"] == 16000 and facts["channels"] == 1
    assert 1.4 <= facts["duration_s"] <= 1.7
    assert "transcript" not in facts


def test_the_temporary_file_is_removed(client, monkeypatch, tmp_path):
    from backend.audio import transcriber
    monkeypatch.setattr(transcriber, "transcribe", lambda pcm: "")
    temp = tmp_path / "uploads"          # tmp_path also holds this test's database
    temp.mkdir()
    monkeypatch.setattr(routes.tempfile, "tempdir", str(temp))
    client.post(REC, content=m4a(), headers=AUDIO)
    client.post(REC, content=b"not audio", headers=AUTH)
    assert list(temp.iterdir()) == []


def test_an_empty_recording_is_refused(client):
    assert client.post(REC, content=b"", headers=AUTH).status_code == 400


def test_an_undecodable_recording_is_415(client):
    assert client.post(REC, content=b"\x00" * 2000, headers=AUDIO).status_code == 415


def test_an_oversized_recording_is_refused_before_decoding(client, monkeypatch):
    monkeypatch.setattr(routes, "MAX_RECORDING_BYTES", 1000)
    decoded = []
    monkeypatch.setattr(routes, "_inspect", lambda *a: decoded.append(a) or {})
    r = client.post(REC, content=b"x" * 1001, headers=AUTH)
    assert r.status_code == 413 and not decoded


def test_a_recording_over_two_minutes_is_refused(client, monkeypatch):
    monkeypatch.setattr(routes, "MAX_RECORDING_S", 1.0)
    assert client.post(REC, content=m4a(1.5), headers=AUDIO).status_code == 413


# --- activity, pause, resume ---------------------------------------------------------------

VIEW = {"as_of": 1.0, "paused": None,
        "sections": [{"title": "Running now", "items": ["job #3 (2m): research TTWO"], "empty": "nothing is running."}]}


@pytest.fixture
def control(monkeypatch):
    from backend.brain import activity, pause
    said = []
    monkeypatch.setattr(activity, "activity_view", lambda now=None: VIEW)
    monkeypatch.setattr(pause, "handle_pause_command", lambda arg: said.append(("pause", arg)) or "Paused.")
    monkeypatch.setattr(pause, "handle_resume_command", lambda arg="": said.append(("resume", arg)) or "Resumed.")
    return said


def test_the_activity_view_is_the_sections_of_slash_activity(client, control):
    view = client.get("/mobile/v1/activity", headers=AUTH).json()
    # /activity's sections, then the phone conversation's tool record (/trace on Discord).
    assert view["sections"][:-1] == VIEW["sections"] and len(VIEW["sections"]) == 1
    assert view["sections"][-1]["title"].startswith("What my tools did in our chat")


def test_the_real_activity_view_has_the_slash_activity_sections():
    from backend.brain import activity
    view = activity.activity_view()
    assert [s["title"] for s in view["sections"]][:4] == [
        "Running now", "Waiting for you", "Scheduled", "Done in the last 24 hours"]
    text = activity.activity_text(view["as_of"])
    for s in view["sections"]:
        assert s["title"] in text


@pytest.mark.parametrize("body, arg", [
    ({}, ""), ({"hours": 3}, "3"), ({"hours": 1.5, "all": True}, "1.5 all"), ({"hours": None, "all": True}, "all"),
])
def test_pause_is_the_discord_command(client, control, body, arg):
    r = client.post("/mobile/v1/pause", json=body, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"text": "Paused.", "activity": VIEW}
    assert control == [("pause", arg)]


@pytest.mark.parametrize("hours", ["3", True, [1]])
def test_pause_hours_must_be_a_number(client, control, hours):
    assert client.post("/mobile/v1/pause", json={"hours": hours}, headers=AUTH).status_code == 400
    assert control == []


def test_resume_is_the_discord_command(client, control):
    assert client.post("/mobile/v1/resume", headers=AUTH).json() == {"text": "Resumed.", "activity": VIEW}
    assert control == [("resume", "")]


def test_the_backend_mounts_the_router():
    src = (pathlib.Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert "app.include_router(mobile_routes.router)" in src
