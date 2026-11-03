"""Regression suite for backend/brain/threads.py's assign_session().

Written specifically to pin down session-boundary behavior after a gap in
conversation — the exact scenario the rumination and predictive-portrait
features both depend on getting right ("I leave him for a few hours and come
back, does he correctly detect a new session?"). No real Qdrant is touched:
recent_threads and the sleep-marker lookup are both monkeypatched. Run with:
    .venv\\Scripts\\pytest backend/brain/test_threads_session.py -v
"""

import asyncio

import pytest

from backend.brain import threads as th


def _patch_recent(monkeypatch, payload):
    async def fake_recent_threads(limit, channel=None):
        return payload

    monkeypatch.setattr(th, "recent_threads", fake_recent_threads)


def _patch_sleep(monkeypatch, marker):
    async def fake_latest_sleep():
        return marker

    monkeypatch.setattr("backend.brain.sleep.latest_sleep", fake_latest_sleep)


def test_gap_just_under_threshold_reuses_session(monkeypatch):
    last_ts = 1_000_000.0
    _patch_recent(monkeypatch, [{"timestamp": last_ts, "session_id": "sess-old"}])
    _patch_sleep(monkeypatch, None)

    now = last_ts + th.SESSION_GAP_THRESHOLD - 60
    decision = asyncio.run(th.assign_session(now, channel="local"))

    assert decision.session_id == "sess-old"
    assert decision.ended_session_id is None


def test_gap_just_over_threshold_starts_new_session(monkeypatch):
    """The 'back after 3 hours' case: gap exceeds the 2h default -- a NEW
    session must be minted and the old one flagged as ended so consolidation
    fires for it."""
    last_ts = 1_000_000.0
    _patch_recent(monkeypatch, [{"timestamp": last_ts, "session_id": "sess-old"}])
    _patch_sleep(monkeypatch, None)

    now = last_ts + th.SESSION_GAP_THRESHOLD + 60
    decision = asyncio.run(th.assign_session(now, channel="local"))

    assert decision.session_id != "sess-old"
    assert decision.ended_session_id == "sess-old"


def test_tiny_gap_but_newer_sleep_marker_starts_new_session(monkeypatch):
    """A graceful shutdown happened between turns, even though the wall-clock
    gap is brief -- must still count as a session boundary."""
    last_ts = 1_000_000.0
    _patch_recent(monkeypatch, [{"timestamp": last_ts, "session_id": "sess-old"}])
    _patch_sleep(monkeypatch, {"timestamp": last_ts + 30})  # slept AFTER the last thread

    now = last_ts + 60  # tiny gap, well under threshold
    decision = asyncio.run(th.assign_session(now, channel="local"))

    assert decision.session_id != "sess-old"
    assert decision.ended_session_id == "sess-old"


def test_sleep_marker_before_last_thread_does_not_force_new_session(monkeypatch):
    """A stale sleep marker from a PRIOR shutdown, already before the last
    recorded thread, must not force a boundary on every subsequent turn."""
    last_ts = 1_000_000.0
    _patch_recent(monkeypatch, [{"timestamp": last_ts, "session_id": "sess-old"}])
    _patch_sleep(monkeypatch, {"timestamp": last_ts - 500})  # slept BEFORE the last thread

    now = last_ts + 60
    decision = asyncio.run(th.assign_session(now, channel="local"))

    assert decision.session_id == "sess-old"
    assert decision.ended_session_id is None


def test_no_prior_threads_mints_fresh_session(monkeypatch):
    _patch_recent(monkeypatch, [])
    _patch_sleep(monkeypatch, None)

    decision = asyncio.run(th.assign_session(1_000_000.0, channel="local"))

    assert decision.session_id
    assert decision.ended_session_id is None


def test_legacy_thread_without_session_id_mints_fresh_id_no_crash(monkeypatch):
    last_ts = 1_000_000.0
    _patch_recent(monkeypatch, [{"timestamp": last_ts}])  # no "session_id" key at all
    _patch_sleep(monkeypatch, None)

    now = last_ts + 60  # small gap -- would normally reuse the session
    decision = asyncio.run(th.assign_session(now, channel="local"))

    assert decision.session_id  # a real id was minted, no KeyError
    assert decision.ended_session_id is None


def test_channel_argument_propagates_to_recent_threads(monkeypatch):
    captured = {}

    async def fake_recent_threads(limit, channel=None):
        captured["channel"] = channel
        return []

    monkeypatch.setattr(th, "recent_threads", fake_recent_threads)
    _patch_sleep(monkeypatch, None)

    asyncio.run(th.assign_session(1_000_000.0, channel="discord"))
    assert captured["channel"] == "discord"


def test_session_gap_threshold_reflects_env_override(monkeypatch):
    # Documents that SESSION_GAP_THRESHOLD is read once from
    # JARVIS_SESSION_GAP_HOURS at import time -- this test pins the default
    # (2.0h) rather than re-importing with a different env var, since the
    # module-level constant is computed at import time, not per-call.
    assert th.SESSION_GAP_THRESHOLD == pytest.approx(2.0 * 3600.0)
