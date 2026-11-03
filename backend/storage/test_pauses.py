"""The pauses store (P2): one active pause, ended once, skips recorded as they happen."""
from datetime import datetime, timezone

import pytest

from backend.storage import db, pauses


def test_a_pause_is_recorded_with_the_life_log_fields():
    ts = datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc).timestamp()       # 00:30 on the 30th, Paris
    pid = pauses.start(pauses.WORK, until_at=ts + 3600, reason="  travelling\nto Lyon ", now=ts)
    row = pauses.get(pid)
    assert (row["started_at"], row["local_date"], row["scope"]) == (ts, "2026-09-30", "work")
    assert row["until_at"] == ts + 3600 and row["reason"] == "travelling to Lyon"
    assert row["ended_at"] is None and row["ended_how"] is None


def test_an_unknown_scope_is_refused():
    with pytest.raises(ValueError):
        pauses.start("everything")


def test_active_reports_the_pause_in_force_and_none_before_or_after():
    assert pauses.active(now=1000.0) is None
    pid = pauses.start(pauses.ALL, until_at=None, now=1000.0)
    assert pauses.active(now=1001.0)["id"] == pid
    pauses.end("resume", now=1002.0)
    assert pauses.active(now=1003.0) is None


def test_a_pause_that_has_run_out_is_over_even_before_the_loop_marks_it():
    """The gate must open on time: it cannot wait for a sweep."""
    pid = pauses.start(pauses.WORK, until_at=2000.0, now=1000.0)
    assert pauses.active(now=1999.0)["id"] == pid
    assert pauses.active(now=2000.0) is None
    assert pauses.get(pid)["ended_at"] is None, "not marked yet: that is expire_due's job"


def test_expire_due_marks_each_expired_pause_once():
    pid = pauses.start(pauses.WORK, until_at=2000.0, now=1000.0)
    assert pauses.expire_due(now=1500.0) == []
    ended = pauses.expire_due(now=2001.0)
    assert [r["id"] for r in ended] == [pid] and ended[0]["ended_how"] == "expired"
    assert pauses.expire_due(now=2002.0) == []


def test_an_open_ended_pause_never_expires():
    pauses.start(pauses.WORK, until_at=None, now=1000.0)
    assert pauses.expire_due(now=10**9) == []
    assert pauses.active(now=10**9) is not None


def test_starting_a_second_pause_ends_the_first_as_replaced():
    first = pauses.start(pauses.WORK, now=1000.0)
    second = pauses.start(pauses.ALL, now=1100.0)
    assert pauses.get(first)["ended_how"] == "replaced" and pauses.get(first)["ended_at"] == 1100.0
    assert pauses.active(now=1101.0)["id"] == second


def test_end_records_how_and_returns_the_row_once():
    pid = pauses.start(pauses.WORK, now=1000.0)
    row = pauses.end("resume", now=1500.0)
    assert (row["id"], row["ended_how"], row["ended_at"]) == (pid, "resume", 1500.0)
    assert pauses.end("resume", now=1501.0) is None


def test_skips_are_appended_in_order_and_attached_to_their_pause():
    a = pauses.start(pauses.WORK, now=1000.0)
    pauses.add_skip(a, "scheduled_task", "NVDA price at the open", now=1100.0)
    pauses.add_skip(a, "job_stopped", "job #3: research the ECB", now=1200.0)
    pauses.end("resume", now=1300.0)
    b = pauses.start(pauses.WORK, now=1400.0)
    pauses.add_skip(b, "scheduled_task", "another", now=1450.0)
    assert [(s["kind"], s["label"]) for s in pauses.skips(a)] == [
        ("scheduled_task", "NVDA price at the open"), ("job_stopped", "job #3: research the ECB")]
    assert [s["label"] for s in pauses.skips(b)] == ["another"]
    assert pauses.skips(a)[0]["local_date"] == "1970-01-01"


def test_skip_text_is_bounded_and_a_missing_pause_records_nothing():
    a = pauses.start(pauses.WORK, now=1000.0)
    pauses.add_skip(a, "scheduled_task", "x" * 900, now=1100.0)
    assert len(pauses.skips(a)[0]["label"]) <= 200
    pauses.add_skip(0, "scheduled_task", "nowhere")
    pauses.add_skip(None, "scheduled_task", "nowhere")


def test_recent_lists_newest_first():
    a = pauses.start(pauses.WORK, now=1000.0)
    b = pauses.start(pauses.ALL, now=1100.0)
    assert [r["id"] for r in pauses.recent()] == [b, a]


def test_storage_failure_pauses_nothing_and_never_raises(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert pauses.start(pauses.WORK) is None
    assert pauses.active() is None and pauses.end("resume") is None
    assert pauses.expire_due() == [] and pauses.skips(1) == [] and pauses.recent() == []
    pauses.add_skip(1, "k", "l")


def test_a_widened_pause_is_one_stretch_and_keeps_what_the_first_one_skipped():
    a = pauses.start(pauses.WORK, now=1000.0)
    pauses.add_skip(a, "scheduled_task", "first", now=1100.0)
    b = pauses.start(pauses.ALL, now=1200.0)
    pauses.add_skip(b, "job_stopped", "second", now=1300.0)
    assert [r["id"] for r in pauses.chain(b)] == [a, b]
    assert [s["label"] for s in pauses.chain_skips(b)] == ["first", "second"]
    assert [r["id"] for r in pauses.chain(a)] == [a], "a chain only reaches back, never forward"


def test_a_pause_that_was_resumed_closes_the_chain():
    a = pauses.start(pauses.WORK, now=1000.0)
    pauses.add_skip(a, "scheduled_task", "before the resume", now=1100.0)
    pauses.end("resume", now=1200.0)
    b = pauses.start(pauses.WORK, now=1300.0)
    assert [r["id"] for r in pauses.chain(b)] == [b]
    assert pauses.chain_skips(b) == []


def test_a_pause_that_had_already_run_out_is_expired_not_replaced():
    """It only had not been swept yet. Calling it 'replaced' would hide that it ended on time
    and would fold its skips into the new pause's message."""
    a = pauses.start(pauses.WORK, until_at=2000.0, now=1000.0)
    pauses.add_skip(a, "scheduled_task", "old", now=1500.0)
    b = pauses.start(pauses.ALL, now=2500.0)
    assert (pauses.get(a)["ended_how"], pauses.get(a)["ended_at"]) == ("expired", 2000.0)
    assert [r["id"] for r in pauses.chain(b)] == [b]
    assert pauses.chain_skips(b) == []


def test_the_chain_of_a_missing_pause_is_empty():
    assert pauses.chain(999) == [] and pauses.chain_skips(999) == []


def test_a_pause_is_due_a_reminder_after_a_whole_day_and_then_a_day_after_each():
    day = pauses.NUDGE_EVERY_S
    pid = pauses.start(pauses.WORK, now=1000.0)
    assert pauses.nudge_due(now=1000.0 + day - 1) is None
    assert pauses.nudge_due(now=1000.0 + day)["id"] == pid
    pauses.mark_nudged(pid, now=1000.0 + day)
    assert pauses.nudge_due(now=1000.0 + day + 5) is None
    assert pauses.nudge_due(now=1000.0 + 2 * day)["id"] == pid


def test_no_reminder_without_a_pause_in_force():
    assert pauses.nudge_due(now=10**9) is None
    pid = pauses.start(pauses.WORK, until_at=2000.0, now=1000.0)      # ran out long ago
    assert pauses.nudge_due(now=10**9) is None
    pauses.start(pauses.WORK, now=5000.0)
    pauses.end("resume", now=5100.0)
    assert pauses.nudge_due(now=10**9) is None
