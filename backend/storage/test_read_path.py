"""A02's deferred read-path work: the canonical store becomes complete, then leading.

CLAUDE.md deferred this as "the only step that can actually lose something". Measuring
first showed why it could not simply be flipped:

    SQLite exchanges : 0
    Qdrant threads   : 345

A02 mirrored every NEW exchange, but an existing install has none of its past in SQLite
— so consulting it first for anything historical would have found nothing. The fix is
two ordered steps: **backfill**, then **lead with it, still falling back**.

Measured on the real history: a Qdrant full scroll (what both readers did) costs
**4,671ms**; the indexed equivalents cost **3.7ms** and effectively 0ms.
`threads_for_session` runs at every session end — consolidation on the shutdown path,
where teardown allows 20s in total.
"""
import asyncio

import pytest

from backend.storage import db, exchanges


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "t.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _thread(i, *, session="s1", high=True, ts=None):
    return {
        "id": f"ex-{i}", "session_id": session, "channel": "local",
        "timestamp": float(ts if ts is not None else 1000 + i),
        "user": f"user {i}" if high else "",
        "assistant": f"assistant {i}" if high else "",
        "significance": 0.9 if high else 0.1,
        "high": high,
        "summary": "" if high else f"User: u{i} | JARVIS: a{i}",
    }


# ------------------------------------------------------------------ backfill
def test_backfill_inserts_missing_exchanges():
    assert exchanges.backfill_missing([_thread(1), _thread(2)]) == 2
    assert len(exchanges.exchanges_for_session("s1")) == 2


def test_backfill_is_idempotent():
    exchanges.backfill_missing([_thread(1), _thread(2)])
    assert exchanges.backfill_missing([_thread(1), _thread(2)]) == 0


def test_backfill_never_replaces_a_live_recorded_row():
    """A backfilled row is strictly POORER than a live one: a low-significance Qdrant
    thread only ever kept a ~150-char summary, never the full text. `record_exchange`
    uses INSERT OR REPLACE, which would have destroyed the better copy."""
    exchanges.record_exchange(
        exchange_id="ex-1", session_id="s1", channel="local",
        user_text="the full live text", assistant_text="full reply",
        significance=0.2, high=False, summary="short summary", timestamp=1001.0)

    exchanges.backfill_missing([_thread(1, high=False)])

    got = exchanges.get_exchange("ex-1")
    assert got["user_text"] == "the full live text", (
        "the backfill overwrote a richer live-recorded exchange")


def test_backfill_preserves_the_low_significance_distinction():
    """It makes the store COMPLETE, not RICHER. Full text for an old low-significance
    exchange was never written anywhere and is simply gone."""
    exchanges.backfill_missing([_thread(1, high=True), _thread(2, high=False)])
    rows = {r["id"]: r for r in exchanges.exchanges_for_session("s1")}
    assert rows["ex-1"]["user_text"] == "user 1"
    assert rows["ex-2"]["user_text"] == ""
    assert rows["ex-2"]["summary"].startswith("User: u2")


def test_backfill_reconstructs_sessions():
    exchanges.backfill_missing([_thread(1, session="a"), _thread(2, session="b")])
    conn = db.connect()
    assert conn.execute("select count(*) from sessions").fetchone()[0] == 2


def test_backfill_skips_rows_with_no_id():
    assert exchanges.backfill_missing([{"session_id": "s1"}]) == 0


def test_backfill_degrades_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert exchanges.backfill_missing([_thread(1)]) == 0


# ------------------------------------------------------------- the range query
def test_the_range_query_is_half_open():
    exchanges.backfill_missing([_thread(i, ts=1000 + i) for i in range(5)])
    got = exchanges.exchanges_in_range(1001, 1003)
    assert [r["id"] for r in got] == ["ex-1", "ex-2"]


def test_the_range_query_is_chronological():
    exchanges.backfill_missing([_thread(3, ts=3000), _thread(1, ts=1000),
                                _thread(2, ts=2000)])
    assert [r["id"] for r in exchanges.exchanges_in_range(0, 9e9)] == \
        ["ex-1", "ex-2", "ex-3"]


def test_the_range_query_degrades_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert exchanges.exchanges_in_range(0, 9e9) == []


# ------------------------------------------------ the readers lead, then fall back
def test_the_session_reader_prefers_the_canonical_store(monkeypatch):
    from backend.brain import threads

    exchanges.backfill_missing([_thread(1), _thread(2)])

    def _must_not_run(*a, **k):
        raise AssertionError("fell back to Qdrant when the index had the answer")

    monkeypatch.setattr(threads, "_scroll_session_sync", _must_not_run)
    got = run(threads.threads_for_session("s1"))
    assert [t["id"] for t in got] == ["ex-1", "ex-2"]
    assert got[0]["user"] == "user 1"


def test_the_session_reader_falls_back_when_the_store_is_empty(monkeypatch):
    """A pre-backfill install must behave exactly as it did before — this is the step
    CLAUDE.md flagged as the only one that can lose something."""
    from backend.brain import threads

    called = {"n": 0}

    def _fallback(session_id):
        called["n"] += 1
        return [{"id": "from-qdrant", "session_id": session_id}]

    monkeypatch.setattr(threads, "_scroll_session_sync", _fallback)
    got = run(threads.threads_for_session("s1"))
    assert called["n"] == 1 and got[0]["id"] == "from-qdrant"


def test_the_range_reader_prefers_the_canonical_store(monkeypatch):
    from backend.brain import threads

    exchanges.backfill_missing([_thread(i, ts=1000 + i) for i in range(3)])

    def _must_not_run(*a, **k):
        raise AssertionError("fell back to Qdrant when the index had the answer")

    monkeypatch.setattr(threads, "_scroll_range_sync", _must_not_run)
    assert len(run(threads.threads_in_range(0, 9e9))) == 3


def test_the_range_reader_falls_back_when_the_store_is_empty(monkeypatch):
    from backend.brain import threads

    monkeypatch.setattr(threads, "_scroll_range_sync",
                        lambda a, b: [{"id": "from-qdrant"}])
    assert run(threads.threads_in_range(0, 9e9))[0]["id"] == "from-qdrant"


def test_a_storage_failure_falls_back_rather_than_raising(monkeypatch):
    from backend.brain import threads

    monkeypatch.setattr(exchanges, "exchanges_for_session",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    monkeypatch.setattr(threads, "_scroll_session_sync",
                        lambda sid: [{"id": "from-qdrant"}])
    assert run(threads.threads_for_session("s1"))[0]["id"] == "from-qdrant"


def test_the_returned_shape_matches_what_callers_expect():
    """consolidation/rumination/memory_timeline read these keys directly."""
    from backend.brain import threads

    rows = threads._as_thread_rows([{
        "id": "x", "session_id": "s", "channel": "discord", "timestamp": 5.0,
        "user_text": "u", "assistant_text": "a", "significance": 0.7,
        "high": 1, "summary": "sum",
    }])
    assert rows == [{"id": "x", "session_id": "s", "channel": "discord",
                     "timestamp": 5.0, "user": "u", "assistant": "a",
                     "significance": 0.7, "high": True, "summary": "sum"}]
