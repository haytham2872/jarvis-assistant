"""GET /mobile/v1/commands and POST /mobile/v1/command: the phone runs the same commands as Discord."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile.test_conversation import AUTH, _isolation  # noqa: F401  (fixture)


def _client():
    from backend.mobile import routes
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_the_list_has_phone_commands_only():
    r = _client().get("/mobile/v1/commands", headers=AUTH)
    assert r.status_code == 200
    items = {c["name"]: c for c in r.json()["commands"]}
    assert "/skill" not in items and "/about" not in items
    assert set(items["/pace"]) == {"name", "usage", "description", "choices"}
    assert items["/pace"]["choices"] == ["off", "low", "normal", "high"]


def test_a_command_returns_its_reply(monkeypatch):
    from backend.brain import discord_bridge as db
    monkeypatch.setattr(db, "_status_handler", lambda: "all fine")
    r = _client().post("/mobile/v1/command", json={"text": "/status"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"reply": "all fine"}


def test_an_ordinary_message_is_a_400():
    r = _client().post("/mobile/v1/command", json={"text": "hello"}, headers=AUTH)
    assert r.status_code == 400


def test_both_routes_need_the_token():
    c = _client()
    assert c.get("/mobile/v1/commands").status_code == 401
    assert c.post("/mobile/v1/command", json={"text": "/status"}).status_code == 401
