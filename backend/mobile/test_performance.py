"""Conditional views, compression, and the one-minute calendar read cache."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from fastapi import WebSocket

from backend.mobile import today
from backend.mobile.devserver import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", "test-token")
    with TestClient(create_app()) as client:
        client.headers["Authorization"] = "Bearer test-token"
        yield client


@pytest.mark.parametrize("path,module,name", [
    ("today", "backend.mobile.today", "view"),
    ("portfolio", "backend.mobile.ledger", "view"),
    ("activity", "backend.mobile.routes", "_activity_with_trace"),
])
def test_conditional_routes_compare_data_not_construction_clocks(client, monkeypatch, path, module, name):
    import importlib
    payload = {"as_of": 1, "now": 1, "items": ["one"]}
    monkeypatch.setattr(importlib.import_module(module), name, lambda: dict(payload))
    first = client.get(f"/mobile/v1/{path}")
    assert first.status_code == 200 and first.json() == payload
    etag = first.headers["etag"]
    payload.update(as_of=2, now=2)
    same = client.get(f"/mobile/v1/{path}", headers={"If-None-Match": etag})
    assert same.status_code == 304 and same.content == b""
    payload["items"].append("two")
    changed = client.get(f"/mobile/v1/{path}", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert changed.json()["items"] == ["one", "two"]
    refused = client.get(f"/mobile/v1/{path}", headers={"Authorization": "wrong", "If-None-Match": changed.headers["etag"]})
    assert refused.status_code == 401  # validation never bypasses auth


def test_inbox_validator_changes_when_a_notice_is_read(client):
    from backend.storage import notices
    notices.record("reminder", "Call Samir")
    first = client.get("/mobile/v1/inbox")
    assert first.status_code == 200
    headers = {"If-None-Match": first.headers["etag"]}
    assert client.get("/mobile/v1/inbox", headers=headers).status_code == 304
    notices.mark_read(first.json()["notices"][0]["id"])
    changed = client.get("/mobile/v1/inbox", headers=headers)
    assert changed.status_code == 200 and changed.json()["unread"] == 0


def test_gzip_compresses_large_http_views_and_leaves_websockets_alone(client, monkeypatch):
    monkeypatch.setattr(today, "view", lambda: {"items": ["large payload" * 200]})
    response = client.get("/mobile/v1/today", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert len(response.json()["items"][0]) > 1000
    small = client.get("/mobile/v1/ping", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers
    # Exercise the same middleware on a genuine WebSocket, without starting the model loop.
    app = create_app()
    @app.websocket("/test-socket")
    async def socket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("large payload" * 200)
        await ws.close()
    with TestClient(app).websocket_connect("/test-socket") as ws:
        assert ws.receive_text() == "large payload" * 200


def test_calendar_cache_expires_is_keyed_by_day_and_can_be_invalidated(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(today.time, "monotonic", lambda: clock[0])
    def read(day):
        calls.append(day)
        return [{"title": str(len(calls))}], ""
    monkeypatch.setattr(today, "_read_calendar", read)
    day = date(2026, 10, 18)
    first, _ = today._calendar(day)
    first[0]["title"] = "modified by caller"
    assert today._calendar(day)[0][0]["title"] == "1"
    clock[0] = 159.9
    today._calendar(day)
    assert len(calls) == 1
    clock[0] = 160
    today._calendar(day)
    assert len(calls) == 2
    today._calendar(date(2026, 10, 19))
    assert len(calls) == 3
    today.clear_calendar_cache(day)
    today._calendar(day)
    assert len(calls) == 4
