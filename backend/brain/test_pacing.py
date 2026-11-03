"""Tests for backend/brain/pacing.py — shared periodic-loop scaffolding.

Five modules (research, chronos, backup_loop, canary, rumination) delegate
their state I/O and interval gating here; each keeps its own thin wrapper
tests (test_backup_loop.py::test_due_*, test_research.py's should_tick
tests, etc.) which now exercise this module indirectly. This file covers
pacing.py's own contract directly, plus run_periodic's loop mechanics,
which no module-level test file touches.
"""

import asyncio
import json

import pytest

from backend.brain import pacing


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------

def test_load_state_missing_file_returns_empty(tmp_path):
    assert pacing.load_state(tmp_path / "nope.json") == {}


def test_load_state_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert pacing.load_state(p) == {}


def test_save_then_load_round_trips(tmp_path):
    p = tmp_path / "state.json"
    pacing.save_state(p, {"last_tick": 123.5, "nested": {"a": 1}})
    assert pacing.load_state(p) == {"last_tick": 123.5, "nested": {"a": 1}}


def test_save_state_failure_degrades_silently(tmp_path):
    # A directory where a file is expected -- write_text raises IsADirectoryError/
    # PermissionError depending on platform; either way this must not raise.
    bad_path = tmp_path / "not_a_file"
    bad_path.mkdir()
    pacing.save_state(bad_path, {"x": 1})   # must not raise


def test_save_state_overwrites_existing_content(tmp_path):
    p = tmp_path / "state.json"
    pacing.save_state(p, {"a": 1})
    pacing.save_state(p, {"b": 2})
    assert pacing.load_state(p) == {"b": 2}


# ---------------------------------------------------------------------------
# interval_elapsed — pure gate, replaces backup_loop.due/canary.due/research's
# should_tick core check
# ---------------------------------------------------------------------------

def test_interval_elapsed_true_after_full_interval():
    assert pacing.interval_elapsed(last_ts=0.0, now=100.0, interval_s=100.0)


def test_interval_elapsed_false_just_under_interval():
    assert not pacing.interval_elapsed(last_ts=0.0, now=99.0, interval_s=100.0)


def test_interval_elapsed_true_well_past_interval():
    assert pacing.interval_elapsed(last_ts=0.0, now=10_000.0, interval_s=100.0)


def test_interval_elapsed_false_immediately_after():
    assert not pacing.interval_elapsed(last_ts=100.0, now=100.0, interval_s=100.0)


def test_interval_elapsed_none_interval_never_due():
    """The 'off' pace convention -- None means disabled, not 'always due'."""
    assert not pacing.interval_elapsed(last_ts=0.0, now=1e9, interval_s=None)


# ---------------------------------------------------------------------------
# run_periodic — the while/sleep/try-except scaffold
# ---------------------------------------------------------------------------

def test_run_periodic_calls_on_cycle_repeatedly(monkeypatch):
    calls = []

    async def fake_sleep(_):
        pass   # skip real waiting

    async def on_cycle():
        calls.append(1)
        if len(calls) >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(pacing.run_periodic(name="test", check_interval_s=1.0, on_cycle=on_cycle))
    assert len(calls) == 3


def test_run_periodic_survives_ordinary_exception(monkeypatch):
    calls = []

    async def fake_sleep(_):
        pass

    async def on_cycle():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")   # one bad cycle
        if len(calls) >= 2:
            raise asyncio.CancelledError()   # stop the test

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(pacing.run_periodic(name="test", check_interval_s=1.0, on_cycle=on_cycle))
    assert len(calls) == 2, "loop must continue after a non-cancellation exception"


def test_run_periodic_cancellation_propagates_immediately(monkeypatch):
    async def fake_sleep(_):
        pass

    async def on_cycle():
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(pacing.run_periodic(name="test", check_interval_s=1.0, on_cycle=on_cycle))


def test_run_periodic_uses_the_given_check_interval(monkeypatch):
    seen = []

    async def fake_sleep(seconds):
        seen.append(seconds)
        if len(seen) >= 2:
            raise asyncio.CancelledError()

    async def on_cycle():
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(pacing.run_periodic(name="test", check_interval_s=42.0, on_cycle=on_cycle))
    assert seen == [42.0, 42.0]
