"""A02: canonical exchanges survive independently of Qdrant and of significance.

The failure being closed: `threads.Thread` keeps `user`/`assistant` only for a
high-significance exchange, so an ordinary turn's real wording lived only in
`jarvis_semantic`, written fire-and-forget. Two async writes had to succeed for it to
stay reconstructable.
"""
import pytest

from backend.storage import db, exchanges


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Never touch the real .jarvis_runtime.db."""
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _add(eid, session="s1", channel="local", user="u", assistant="a",
         sig=0.1, high=False, ts=1000.0, summary=""):
    return exchanges.record_exchange(
        exchange_id=eid, session_id=session, channel=channel, user_text=user,
        assistant_text=assistant, significance=sig, high=high, summary=summary,
        timestamp=ts)


# --------------------------------------------------------------- schema / setup
def test_schema_is_created_on_first_connect():
    assert db.healthy() is True
    conn = db.connect()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


def test_migrations_are_idempotent():
    db.connect()
    db._reset_for_tests()
    assert db.healthy() is True          # reopening must not re-run or fail
    assert exchanges.counts().get("exchanges") == 0


def test_unusable_database_degrades_instead_of_raising(monkeypatch, tmp_path):
    """A storage failure must never be able to fail a turn."""
    target = tmp_path / "not-a-dir"
    target.write_text("this is a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("JARVIS_DB_PATH", str(target / "nested" / "x.db"))
    db._reset_for_tests()
    assert db.connect() is None
    assert db.healthy() is False
    assert _add("e1") is False           # returns False, does not raise
    assert exchanges.recent_exchanges() == []
    assert exchanges.get_exchange("e1") is None
    assert exchanges.counts() == {}


# ------------------------------------------------------- the actual A02 property
def test_full_text_is_kept_for_a_low_significance_exchange():
    """The whole point. A Thread would store user=""/assistant="" here."""
    _add("e1", user="the SearXNG port is 8088", assistant="noted", sig=0.1, high=False)
    row = exchanges.get_exchange("e1")
    assert row["user_text"] == "the SearXNG port is 8088"
    assert row["assistant_text"] == "noted"
    assert row["high"] == 0
    assert row["significance"] == pytest.approx(0.1)


def test_significance_is_recorded_but_never_gates_storage():
    _add("low", user="low sig text", sig=0.05, high=False)
    _add("high", user="high sig text", sig=0.9, high=True)
    assert exchanges.get_exchange("low")["user_text"] == "low sig text"
    assert exchanges.get_exchange("high")["user_text"] == "high sig text"


def test_record_is_idempotent_on_exchange_id():
    """A retry or a resumed derivation must not duplicate an exchange."""
    _add("e1", user="first")
    _add("e1", user="first")
    assert exchanges.counts()["exchanges"] == 1


# ------------------------------------------------------------------- retrieval
def test_exchanges_for_session_is_chronological():
    _add("b", session="s1", ts=200.0)
    _add("a", session="s1", ts=100.0)
    _add("other", session="s2", ts=150.0)
    got = [r["id"] for r in exchanges.exchanges_for_session("s1")]
    assert got == ["a", "b"]


def test_recent_exchanges_is_newest_first_and_channel_scoped():
    _add("old", channel="local", ts=100.0)
    _add("new", channel="local", ts=300.0)
    _add("disc", channel="discord", ts=200.0)
    assert [r["id"] for r in exchanges.recent_exchanges(limit=10)] == ["new", "disc", "old"]
    assert [r["id"] for r in exchanges.recent_exchanges(channel="local")] == ["new", "old"]
    assert [r["id"] for r in exchanges.recent_exchanges(channel="discord")] == ["disc"]


def test_recent_exchanges_respects_the_limit():
    for i in range(10):
        _add(f"e{i}", ts=float(i))
    assert len(exchanges.recent_exchanges(limit=3)) == 3


def test_last_activity_is_per_channel():
    _add("l", channel="local", ts=100.0)
    _add("d", channel="discord", ts=250.0)
    assert exchanges.last_activity("local") == pytest.approx(100.0)
    assert exchanges.last_activity("discord") == pytest.approx(250.0)
    assert exchanges.last_activity("nonexistent") is None


# -------------------------------------------------------------------- sessions
def test_session_row_tracks_first_and_latest_activity():
    _add("a", session="s1", ts=100.0)
    _add("b", session="s1", ts=500.0)
    row = db.connect().execute(
        "SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
    assert row["started_at"] == pytest.approx(100.0)
    assert row["last_activity_at"] == pytest.approx(500.0)
    assert row["ended_at"] is None


def test_end_session_marks_it_closed():
    _add("a", session="s1", ts=100.0)
    assert exchanges.end_session("s1", when=900.0) is True
    row = db.connect().execute(
        "SELECT ended_at FROM sessions WHERE session_id = 's1'").fetchone()
    assert row["ended_at"] == pytest.approx(900.0)


# --------------------------------------------------------------------- ingress
def test_ingress_claim_deduplicates_a_redelivered_message():
    """The roadmap's rule: a gateway redelivery must not run the request twice."""
    assert exchanges.claim_ingress("msg-1", "discord") is True
    assert exchanges.claim_ingress("msg-1", "discord") is False
    assert exchanges.claim_ingress("msg-2", "discord") is True


def test_ingress_admits_when_storage_is_unavailable(monkeypatch):
    """Failing open, deliberately: unable to deduplicate is not a reason to silently
    drop a real message the user sent."""
    monkeypatch.setattr(db, "connect", lambda: None)
    assert exchanges.claim_ingress("msg-x", "discord") is True
