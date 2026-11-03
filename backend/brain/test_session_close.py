"""Quiet-session close (backend/brain/session_close.py) and the per-session
consolidation lock it made necessary. SQLite is the per-test database from conftest."""
import asyncio

import pytest

from backend.brain import consolidation, session_close
from backend.brain.threads import SESSION_GAP_THRESHOLD as GAP
from backend.storage import exchanges

NOW = 1_800_000_000.0


def run(coro):
    return asyncio.run(coro)


def exchange(sid, channel, ts):
    assert exchanges.record_exchange(exchange_id=f"{sid}-{ts}", session_id=sid, channel=channel,
                                     user_text="hi", assistant_text="hello", significance=0.1,
                                     high=False, timestamp=ts)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    session_close.set_busy_check(None)
    consolidation._session_locks.clear()
    yield
    session_close.set_busy_check(None)


def test_only_sessions_quiet_for_the_gap_are_due():
    exchange("quiet", "discord", NOW - GAP - 60)
    exchange("live", "discord", NOW - 600)
    exchange("canary", "canary", NOW - GAP - 60)
    exchange("ancient", "local", NOW - session_close._LOOKBACK_S - 60)
    assert [s["session_id"] for s in session_close.due(NOW)] == ["quiet"]


def test_a_session_is_closed_once_and_marked_ended(monkeypatch):
    exchange("quiet", "discord", NOW - GAP - 60)
    done = []

    async def fake(sid):
        done.append(sid)
    monkeypatch.setattr(consolidation, "consolidate_session", fake)
    assert run(session_close.close_one(NOW)) == "quiet"
    assert run(session_close.close_one(NOW)) is None          # ended: not picked again
    assert done == ["quiet"]


def test_one_per_cycle_oldest_first(monkeypatch):
    exchange("older", "local", NOW - GAP - 900)
    exchange("newer", "discord", NOW - GAP - 60)

    async def fake(sid):
        pass
    monkeypatch.setattr(consolidation, "consolidate_session", fake)
    assert run(session_close.close_one(NOW)) == "older"
    assert run(session_close.close_one(NOW)) == "newer"


def test_a_live_turn_defers_the_close(monkeypatch):
    exchange("quiet", "discord", NOW - GAP - 60)
    session_close.set_busy_check(lambda: True)
    monkeypatch.setattr(consolidation, "consolidate_session",
                        lambda sid: (_ for _ in ()).throw(AssertionError("must not run")))
    assert run(session_close.close_one(NOW)) is None
    assert [s["session_id"] for s in session_close.due(NOW)] == ["quiet"]


def test_a_failed_consolidation_is_not_retried_by_the_loop(monkeypatch):
    """One attempt, then the next-message path is the retry -- never a burst of calls."""
    exchange("quiet", "discord", NOW - GAP - 60)

    async def boom(sid):
        raise RuntimeError("model down")
    monkeypatch.setattr(consolidation, "consolidate_session", boom)
    with pytest.raises(RuntimeError):
        run(session_close.close_one(NOW))
    assert session_close.due(NOW) == []


def test_two_closes_of_one_session_never_overlap(monkeypatch):
    """The next message after a gap and the quiet-session loop can close the same session
    at the same moment; before the lock both generated and applied a proposal."""
    active, peak, calls = {}, {}, []

    async def inner(sid):
        active[sid] = active.get(sid, 0) + 1
        peak[sid] = max(peak.get(sid, 0), active[sid])
        calls.append(sid)
        await asyncio.sleep(0.01)
        active[sid] -= 1
    monkeypatch.setattr(consolidation, "_consolidate_session", inner)

    async def both():
        await asyncio.gather(consolidation.consolidate_session("s1"),
                             consolidation.consolidate_session("s1"),
                             consolidation.consolidate_session("s2"))
    run(both())
    assert peak == {"s1": 1, "s2": 1}          # the same session never runs twice at once
    assert calls.count("s1") == 2              # the second waits, then runs (and finds it done)


def test_the_loop_is_wired():
    import inspect
    from backend import main
    src = inspect.getsource(main)
    assert '"session_close": session_close.run_loop' in src
    start = src.index("bootstrap.wire_busy_check(")
    busy = src[start:src.index("]", start)]
    assert "session_close" in busy          # gets the pipeline busy check
