"""Tests for backend/brain/backup_loop.py — the periodic backup safety net.

Pure-function + state-file tests, plus _tick with create_backup monkeypatched
out (no real Qdrant/network/filesystem backup work). Run with:
    .venv\\Scripts\\pytest backend/brain/test_backup_loop.py -v
"""

import time
import asyncio

import pytest

from backend.brain import backup_loop


# ---------------------------------------------------------------------------
# due — pure gating
# ---------------------------------------------------------------------------

def test_due_false_immediately_after_a_backup():
    now = time.time()
    assert not backup_loop.due(now, now)


def test_due_true_after_a_full_interval():
    now = time.time()
    last = now - (backup_loop._BACKUP_INTERVAL_HOURS * 3600.0) - 1.0
    assert backup_loop.due(last, now)


def test_due_false_just_under_the_interval():
    now = time.time()
    last = now - (backup_loop._BACKUP_INTERVAL_HOURS * 3600.0) + 60.0
    assert not backup_loop.due(last, now)


# ---------------------------------------------------------------------------
# state persistence (mirrors research.py's tmp_pace_file fixture pattern)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_loop, "_STATE_FILE", tmp_path / "backup_state.json")
    return tmp_path / "backup_state.json"


def test_load_state_missing_file_returns_empty(tmp_state_file):
    assert backup_loop._load_state() == {}


def test_save_then_load_round_trips(tmp_state_file):
    backup_loop._save_state({"last_backup": 12345.0})
    assert backup_loop._load_state() == {"last_backup": 12345.0}


def test_corrupt_state_file_degrades_to_empty(tmp_state_file):
    tmp_state_file.write_text("{not json", encoding="utf-8")
    assert backup_loop._load_state() == {}


# ---------------------------------------------------------------------------
# _tick — alerts only on TOTAL failure, stays silent on success/partial
# ---------------------------------------------------------------------------

def test_tick_silent_on_full_success(monkeypatch):
    monkeypatch.setattr(backup_loop, "create_backup",
                         lambda: {"vault_ok": True, "collections_ok": ["a"], "collections_failed": []})
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    backup_loop.set_notifier(fake_notifier)
    try:
        asyncio.run(backup_loop._tick())
        assert sent == []
    finally:
        backup_loop.set_notifier(None)


def test_tick_silent_on_partial_failure(monkeypatch):
    # Vault ok but some collections failed -- still a usable backup, not an alert.
    monkeypatch.setattr(backup_loop, "create_backup",
                         lambda: {"vault_ok": True, "collections_ok": [], "collections_failed": ["a"]})
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    backup_loop.set_notifier(fake_notifier)
    try:
        asyncio.run(backup_loop._tick())
        assert sent == []
    finally:
        backup_loop.set_notifier(None)


def test_tick_alerts_on_total_failure(monkeypatch):
    monkeypatch.setattr(backup_loop, "create_backup",
                         lambda: {"vault_ok": False, "collections_ok": [], "collections_failed": ["a", "b"]})
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    backup_loop.set_notifier(fake_notifier)
    try:
        asyncio.run(backup_loop._tick())
        assert len(sent) == 1
        assert "backup" in sent[0].lower()
    finally:
        backup_loop.set_notifier(None)


def test_tick_total_failure_without_notifier_does_not_raise(monkeypatch):
    monkeypatch.setattr(backup_loop, "create_backup",
                         lambda: {"vault_ok": False, "collections_ok": [], "collections_failed": []})
    backup_loop.set_notifier(None)
    asyncio.run(backup_loop._tick())  # must not raise


def test_tick_alerts_when_only_the_database_copy_failed(monkeypatch):
    """The vault and Qdrant saved, the database not: the plan log, approvals and receipts
    have no copy, so this is not a routine success."""
    monkeypatch.setattr(backup_loop, "create_backup",
                         lambda: {"vault_ok": True, "collections_ok": ["a"],
                                  "collections_failed": [], "database": "failed"})
    sent = []

    async def fake_notifier(text):
        sent.append(text)
        return True

    backup_loop.set_notifier(fake_notifier)
    try:
        asyncio.run(backup_loop._tick())
        assert len(sent) == 1 and "database" in sent[0]
    finally:
        backup_loop.set_notifier(None)
