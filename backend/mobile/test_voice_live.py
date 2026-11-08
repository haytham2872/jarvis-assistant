"""The /voice/live WebSocket: auth, ids, idempotency, a full fake session, rejection."""
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.mobile.test_conversation import AUTH, _isolation  # noqa: F401  (fixture)

SR = 16000


def tone(ms):
    n = SR * ms // 1000
    return (np.sin(2 * np.pi * 220 * np.arange(n) / SR) * 4500).astype("<i2").tobytes()


@pytest.fixture
def client(monkeypatch):
    from backend.audio import live
    from backend.mobile import conversation, routes
    monkeypatch.setattr(live, "default_vad_factory",
                        lambda: (lambda f: 1.0 if float(np.sqrt(np.mean(f ** 2))) > 0.01 else 0.0))
    monkeypatch.setattr(live, "_default_asr", lambda pcm: "remind me tomorrow")
    monkeypatch.setattr(live, "_default_warm", lambda: None)
    monkeypatch.setattr(live, "_default_turn_prob", lambda pcm: 0.9)
    monkeypatch.setattr(live, "_default_commit_after", lambda p: 250)
    submits = []
    monkeypatch.setattr(conversation, "status", lambda rid: None)

    def submit(rid, text, kind="text", spoken=False, reply_to=None):
        submits.append((rid, text, kind, spoken, reply_to))
        return {"request_id": rid, "state": "queued", "text": text}
    monkeypatch.setattr(conversation, "submit", submit)
    app = FastAPI()
    app.include_router(routes.router)
    c = TestClient(app)
    c.submits = submits
    return c


URL = "/mobile/v1/voice/live?request_id=req-live0001"


def test_refusals(client, monkeypatch):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(URL):
            pass
    assert e.value.code == 4401
    monkeypatch.delenv("MOBILE_TOKEN")
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(URL, headers=AUTH):
            pass
    assert e.value.code == 4404


def test_invalid_request_id(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/mobile/v1/voice/live?request_id=x", headers=AUTH):
            pass
    assert e.value.code == 4400


def test_known_request_id_returns_status_without_transcribing(client, monkeypatch):
    from backend.mobile import conversation
    monkeypatch.setattr(conversation, "status",
                        lambda rid: {"request_id": rid, "state": "done", "text": "old"})
    with client.websocket_connect(URL, headers=AUTH) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "turn" and msg["status"]["state"] == "done" and msg["text"] == "old"
    assert client.submits == []


@pytest.mark.parametrize("spoken,flag", [(1, True), (0, False)])
def test_full_session_submits_once(client, spoken, flag):
    with client.websocket_connect(f"{URL}&mode=note&spoken={spoken}&reply_to=7", headers=AUTH) as ws:
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(tone(1000))
        ws.send_bytes(bytes(2 * SR // 2))
        ws.send_text('{"type":"end"}')
        got = [ws.receive_json(), ws.receive_json()]
    assert got[0] == {"type": "heard", "text": "remind me tomorrow"}
    assert got[1]["type"] == "turn" and got[1]["text"] == "remind me tomorrow"
    assert client.submits == [("req-live0001", "remind me tomorrow", "voice", flag, 7)]


def test_conversation_mode_commits_by_itself(client):
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()
        ws.send_bytes(tone(1000))
        ws.send_bytes(bytes(2 * SR))
        kinds = [ws.receive_json()["type"], ws.receive_json()["type"]]
    assert kinds == ["heard", "turn"] and len(client.submits) == 1


def test_cancel_and_empty(client, monkeypatch):
    with client.websocket_connect(URL, headers=AUTH) as ws:
        ws.receive_json()
        ws.send_bytes(tone(500))
        ws.send_text('{"type":"cancel"}')
    assert client.submits == []
    from backend.audio import live
    monkeypatch.setattr(live, "_default_asr", lambda pcm: "")
    with client.websocket_connect(URL + "&mode=note", headers=AUTH) as ws:
        ws.receive_json()
        ws.send_bytes(tone(500))
        ws.send_text('{"type":"end"}')
        assert ws.receive_json() == {"type": "empty"}
    assert client.submits == []


def test_rejected_submit_is_an_error(client, monkeypatch):
    from backend.mobile import conversation

    def submit(*a, **k):
        raise conversation.Rejected(429, "too many")
    monkeypatch.setattr(conversation, "submit", submit)
    with client.websocket_connect(URL + "&mode=note", headers=AUTH) as ws:
        ws.receive_json()
        ws.send_bytes(tone(500))
        ws.send_text('{"type":"end"}')
        ws.receive_json()
        assert ws.receive_json() == {"type": "error", "detail": "too many"}
