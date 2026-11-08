"""The portrait view: how JARVIS sees the owner now, and which lines have faded."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.brain import portrait
from backend.mobile import portrait_view as P, routes
from backend.tools import vault

V2 = f"""# Portrait
{portrait.MARKER}

## Situation
- Preparing for an exam (as of 2026-11-06)
- Moving flat (as of 2026-10-01)

## Who they are
- Builds a model railway (as of 2026-09-02)

## How to be with them
- Be direct (as of 2026-08-01)
"""

HISTORY = """# Portrait history

## 2026-10-02
- Old plan A (as of 2026-08-10)

## 2026-11-20
- Old plan B (as of 2026-10-03)
- Old plan C (as of 2026-10-05)

## 2026-09-15
- Old plan D (as of 2026-07-30)
"""


def put(name, text):
    path = vault._VAULT / "people" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_v2_sections_in_file_order():
    put("owner.md", V2)
    v = P.view()
    assert v["migrated"] is True and v["fading_days"] == 45
    assert [s["title"] for s in v["sections"]] == ["Situation", "Who they are", "How to be with them"]
    assert v["sections"][0]["entries"] == [
        {"text": "Preparing for an exam", "as_of": "2026-11-06"},
        {"text": "Moving flat", "as_of": "2026-10-01"}]
    assert v["faded"] == []


def test_not_migrated_has_no_sections():
    put("owner.md", "# Portrait\n\nAn evolving portrait.\n\n## Situation\n- old style\n")
    v = P.view()
    assert v["migrated"] is False and v["sections"] == []


def test_history_grouped_newest_first():
    put("owner.md", V2)
    put("owner-history.md", HISTORY)
    faded = P.view()["faded"]
    assert [g["faded_on"] for g in faded] == ["2026-11-20", "2026-10-02", "2026-09-15"]
    assert faded[0]["entries"] == [{"text": "Old plan B", "as_of": "2026-10-03"},
                                   {"text": "Old plan C", "as_of": "2026-10-05"}]


def test_missing_files_give_empty_shape():
    assert P.view() == {"migrated": False, "sections": [], "faded": [], "fading_days": 45}


def test_unreadable_file_never_raises(monkeypatch):
    put("owner.md", V2)
    monkeypatch.setattr(P, "_read", lambda name: (_ for _ in ()).throw(OSError("boom")))
    assert P.view()["sections"] == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", "portrait-fixture")
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer portrait-fixture"
        yield c


def test_route_auth_and_conditional(client):
    put("owner.md", V2)
    assert client.get("/mobile/v1/mind/portrait", headers={"Authorization": ""}).status_code == 401
    r = client.get("/mobile/v1/mind/portrait")
    assert r.status_code == 200 and r.json()["sections"][0]["title"] == "Situation"
    assert client.get("/mobile/v1/mind/portrait", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
