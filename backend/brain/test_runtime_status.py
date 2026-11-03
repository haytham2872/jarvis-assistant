"""Tests for backend/brain/runtime_status.py — the "vital signs" aggregator.

Pure logic + turn counting, plus runtime_status_text() with every external
dependency (research/background/canary/usage) monkeypatched out. Run with:
    .venv\\Scripts\\pytest backend/brain/test_runtime_status.py -v
"""

import time
import datetime

import pytest

from backend.brain import runtime_status


@pytest.fixture(autouse=True)
def _reset_turn_counts():
    runtime_status._turn_counts.clear()
    yield
    runtime_status._turn_counts.clear()


# ---------------------------------------------------------------------------
# turn counting
# ---------------------------------------------------------------------------

def test_turns_today_zero_when_nothing_recorded():
    assert runtime_status.turns_today() == 0


def test_record_turn_increments_todays_count():
    runtime_status.record_turn()
    runtime_status.record_turn()
    assert runtime_status.turns_today() == 2


# ---------------------------------------------------------------------------
# _host_label — which machine this process is actually on (2026-09-08
# hardening: JARVIS had no reliable way to tell Linux from Windows mid-turn)
# ---------------------------------------------------------------------------

def test_host_label_linux(monkeypatch):
    monkeypatch.setattr(runtime_status.platform, "system", lambda: "Linux")
    label = runtime_status._host_label()
    assert "Linux" in label
    assert "always-on" in label
    assert "no microphone" in label
    assert "screen or terminal tool" in label


def test_host_label_windows(monkeypatch):
    monkeypatch.setattr(runtime_status.platform, "system", lambda: "Windows")
    label = runtime_status._host_label()
    assert "Windows" in label
    assert "desk" in label
    assert "wake word" in label
    assert "no screen or terminal tool" in label


def test_host_label_unknown_platform_degrades_to_raw_name(monkeypatch):
    monkeypatch.setattr(runtime_status.platform, "system", lambda: "Darwin")
    assert runtime_status._host_label() == "Darwin"


def test_runtime_status_text_includes_host_line(monkeypatch):
    monkeypatch.setattr(runtime_status.platform, "system", lambda: "Linux")
    text = runtime_status.runtime_status_text()
    assert "Host: Linux" in text
    assert f"Version: {runtime_status.VERSION}" in text
    assert f"Model: {runtime_status.OPENROUTER_STANDARD} via OpenRouter" in text


def test_runtime_identity_text_reports_live_host_and_mode(monkeypatch):
    monkeypatch.setattr(runtime_status.platform, "system", lambda: "Windows")
    text = runtime_status.runtime_identity_text()
    assert "[Runtime identity]" in text
    assert "Host: Windows" in text
    assert f"Version: {runtime_status.VERSION}" in text
    assert f"Model: {runtime_status.OPENROUTER_STANDARD} via OpenRouter" in text


def test_turns_today_isolated_from_other_days(monkeypatch):
    real_date = datetime.date

    class _FakeDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2020, 1, 1)

    monkeypatch.setattr(datetime, "date", _FakeDate)
    runtime_status.record_turn()
    runtime_status.record_turn()
    assert runtime_status._turn_counts == {"2020-01-01": 2}

    class _FakeDateNextDay(real_date):
        @classmethod
        def today(cls):
            return real_date(2020, 1, 2)

    monkeypatch.setattr(datetime, "date", _FakeDateNextDay)
    assert runtime_status.turns_today() == 0   # yesterday's count doesn't leak into today
    runtime_status.record_turn()
    assert runtime_status.turns_today() == 1
    assert runtime_status._turn_counts["2020-01-01"] == 2   # yesterday's entry untouched


# ---------------------------------------------------------------------------
# uptime
# ---------------------------------------------------------------------------

def test_uptime_seconds_is_nonnegative_and_monotonic():
    a = runtime_status.uptime_seconds()
    time.sleep(0.01)
    b = runtime_status.uptime_seconds()
    assert a >= 0
    assert b >= a


def test_format_uptime_minutes_only():
    assert runtime_status._format_uptime(59 * 60) == "59m"


def test_format_uptime_hours_and_minutes():
    assert runtime_status._format_uptime(3 * 3600 + 42 * 60) == "3h 42m"


# ---------------------------------------------------------------------------
# memory health formatting
# ---------------------------------------------------------------------------

def test_format_memory_health_never_checked():
    assert runtime_status._format_memory_health(None, []) == "not checked yet this session"


def test_format_memory_health_all_healthy():
    text = runtime_status._format_memory_health(time.time() - 60, [])
    assert "healthy" in text
    assert "FAILED" not in text


def test_format_memory_health_names_failed_layers():
    text = runtime_status._format_memory_health(time.time() - 60, ["mem0"])
    assert "mem0" in text
    assert "FAILED" in text


# ---------------------------------------------------------------------------
# runtime_status_text() — full aggregation, all dependencies stubbed
# ---------------------------------------------------------------------------

def test_runtime_status_text_includes_every_expected_field(monkeypatch):
    import backend.brain.research as research
    import backend.brain.background as background
    import backend.brain.canary as canary
    import backend.brain.usage as usage_mod
    import backend.brain.semindex as semindex

    monkeypatch.setattr(research, "get_pace", lambda: "normal")
    monkeypatch.setattr(background, "running_count", lambda: 1)
    monkeypatch.setattr(background, "finished_count", lambda: 3)
    monkeypatch.setattr(canary, "last_check_info", lambda: (time.time() - 120, []))
    monkeypatch.setattr(usage_mod, "session_summary", lambda: "kimi-k2.5: 10 in / 5 out")
    monkeypatch.setattr(semindex, "cached_point_count", lambda: 1234)

    text = runtime_status.runtime_status_text()
    assert "[Runtime status]" in text
    assert "Pace: normal" in text
    assert "1 running, 3 completed" in text
    assert "all layers healthy" in text
    assert "kimi-k2.5: 10 in / 5 out" in text
    assert "Turns today:" in text
    assert "Uptime:" in text
    assert "1,234 points" in text


def test_runtime_status_text_semantic_size_unknown_when_cache_unseeded(monkeypatch):
    import backend.brain.semindex as semindex
    monkeypatch.setattr(semindex, "cached_point_count", lambda: None)
    text = runtime_status.runtime_status_text()
    assert "Semantic index size: unknown" in text


def test_runtime_status_text_degrades_gracefully_when_a_source_errors(monkeypatch):
    import backend.brain.research as research

    def _broken():
        raise RuntimeError("pace file corrupt")

    monkeypatch.setattr(research, "get_pace", _broken)
    text = runtime_status.runtime_status_text()   # must not raise
    assert "[Runtime status]" in text
    assert "Pace: unknown" in text
