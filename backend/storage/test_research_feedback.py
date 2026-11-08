"""Thumbs up/down on research findings: stored per notice, latest wins."""
import pytest

from backend.storage import db, notices, research_feedback as fb


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "fb.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _finding(subject="Camus notebooks", ts=1700000000):
    return notices.record("research", "A finding.", key=f"research:{subject}@{ts}")


def test_vote_is_stored_and_the_latest_wins():
    nid = _finding()
    assert fb.record(nid, "up") == {"notice_id": nid, "subject": "Camus notebooks", "vote": "up"}
    assert fb.record(nid, "down")["vote"] == "down"
    assert fb.vote_for([nid]) == {nid: "down"}
    rows = fb.votes(0)
    assert len(rows) == 1 and rows[0]["subject"] == "Camus notebooks" and rows[0]["vote"] == "down"


def test_none_removes_the_vote():
    nid = _finding()
    fb.record(nid, "up")
    assert fb.record(nid, "none")["vote"] == "none"
    assert fb.vote_for([nid]) == {} and fb.votes(0) == []


def test_subject_is_between_the_prefix_and_the_last_at():
    nid = _finding("who@what", 5)
    assert fb.record(nid, "up")["subject"] == "who@what"


def test_only_research_findings_with_a_parsable_key():
    other = notices.record("brief", "Hi", key="brief:2026-10-06")
    bare = notices.record("research", "No key")
    bad = notices.record("research", "Bad", key="research:nothing")
    for nid in (other, bare, bad, 9999):
        assert fb.record(nid, "up") is None
    assert fb.vote_for([other]) == {}


def test_votes_since_filters_by_time():
    nid = _finding()
    fb.record(nid, "up")
    assert fb.votes(2**40) == []
    assert fb.vote_for([]) == {}


def test_never_raises_without_storage(monkeypatch):
    monkeypatch.setattr(fb, "_conn", lambda: None)
    assert fb.record(1, "up") is None and fb.votes(0) == [] and fb.vote_for([1]) == {}
