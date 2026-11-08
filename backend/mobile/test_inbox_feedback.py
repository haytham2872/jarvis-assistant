"""The Inbox's thumbs on research findings: POST /inbox/feedback and the vote on GET /inbox."""
from backend.mobile.test_conversation import AUTH, _client, _isolation, _main  # noqa: F401 (fixture)
from backend.storage import notices as store


def _finding():
    return store.record("research", "A finding.", key="research:Camus@1700000000")


def _vote(c, nid, vote):
    return c.post("/mobile/v1/inbox/feedback", json={"id": nid, "vote": vote}, headers=AUTH)


def test_vote_round_trip_shows_on_the_inbox(monkeypatch):
    c = _client(monkeypatch)
    nid = _finding()
    other = store.record("brief", "Good morning")
    assert _vote(c, nid, "up").json() == {"vote": "up"}
    notes = {n["id"]: n for n in c.get("/mobile/v1/inbox", headers=AUTH).json()["notices"]}
    assert notes[nid]["vote"] == "up" and "vote" not in notes[other]
    assert _vote(c, nid, "down").json() == {"vote": "down"}
    assert _vote(c, nid, "none").json() == {"vote": None}
    notes = {n["id"]: n for n in c.get("/mobile/v1/inbox", headers=AUTH).json()["notices"]}
    assert notes[nid]["vote"] is None


def test_bad_bodies_and_missing_notices(monkeypatch):
    c = _client(monkeypatch)
    nid = _finding()
    brief = store.record("brief", "Hi")
    assert _vote(c, nid, "maybe").status_code == 400
    assert _vote(c, "x", "up").status_code == 400
    assert c.post("/mobile/v1/inbox/feedback", json=[1], headers=AUTH).status_code == 400
    assert _vote(c, 9999, "up").status_code == 404
    assert _vote(c, brief, "up").status_code == 404
    assert c.post("/mobile/v1/inbox/feedback", json={"id": nid, "vote": "up"}).status_code == 401
