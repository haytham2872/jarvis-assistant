"""G0 / N1, D7: scheduled runs and night tasks across a crash.

N1 (code-inspected on e2467c3): `_fire_due_tasks` advanced schedule.md before the run was
admitted, and a run that could not start yet (every slot busy) was kept for a retry only in
a module list -- so a restart inside the 20-minute grace silently lost it. The [done] trail
also said "task run:" before anything had run.

D7: a night task was popped from schedule.md before it ran, so a crash mid-run lost it
without a word; and a failure was written up as a drafts/ note and trailed "done".

A crash is simulated by a BaseException raised where the process would die, then a fresh
call standing in for the restarted process (nothing in memory is relied on).
"""
import asyncio
import datetime
import json

import pytest

from backend.brain import background, chronos
from backend.brain.chronos import _TZ
from backend.storage import jobs


class Crash(BaseException):
    """The process dying at this point."""


def paris(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


def run(coro):
    return asyncio.run(coro)


TASK = "look up NVDA's live share price and report it with its source"


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    monkeypatch.setattr(chronos.pause, "is_paused", lambda *a: False)
    return tmp_path


@pytest.fixture
def dms(monkeypatch):
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(chronos, "_notifier", notifier)
    return sent


def _restart():
    """What a restarted process still has: files and SQLite, no module memory."""
    if hasattr(chronos, "_task_retries"):          # e2467c3 kept retries here
        chronos._task_retries.clear()


def _schedule_due_task(at):
    chronos._write_schedule([chronos._format_task_line(at, "daily", "", "Europe/Paris", TASK)])


def _trail():
    return [l for l in chronos._read_schedule() if "task " in l and l.lstrip().startswith("[")]


# --- N1 -------------------------------------------------------------------------------

def test_a_run_waiting_for_a_slot_survives_a_restart(files, monkeypatch, dms):
    started = []
    busy = {"n": 1}

    async def start(label, brief, skill="", occurrence=""):
        if busy["n"]:
            busy["n"] -= 1
            return "Error: 3 background tasks are already running"
        started.append(label)
        return ""
    monkeypatch.setattr(background, "start_scheduled", start)
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    assert started == []
    _restart()
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 33)))
    assert len(started) == 1 and dms == []


def test_a_crash_after_the_schedule_advanced_still_runs_after_restart(files, monkeypatch, dms):
    started = []

    async def dies(label, brief, skill="", occurrence=""):
        raise Crash()

    async def start(label, brief, skill="", occurrence=""):
        started.append(label)
        return ""
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    monkeypatch.setattr(background, "start_scheduled", dies)
    with pytest.raises(Crash):
        run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    # the schedule has moved on to tomorrow; the run itself must not be gone
    assert chronos._parse_task_line(chronos._read_schedule()[0])["due"].day == 1
    _restart()
    monkeypatch.setattr(background, "start_scheduled", start)
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 34)))
    assert len(started) == 1


def test_a_crash_while_recording_the_run_leaves_the_schedule_untouched(files, monkeypatch, dms):
    """The record comes first: dying while writing it must leave the occurrence due in
    schedule.md, so the restarted process finds it there."""
    started = []

    async def start(label, brief, skill="", occurrence=""):
        started.append(label)
        return ""
    real = chronos._record_due_runs
    monkeypatch.setattr(chronos, "_record_due_runs", lambda records: (_ for _ in ()).throw(Crash()))
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    with pytest.raises(Crash):
        run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    _restart()
    monkeypatch.setattr(chronos, "_record_due_runs", real)
    monkeypatch.setattr(background, "start_scheduled", start)
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 31)))
    assert len(started) == 1


def test_a_pending_run_found_after_the_grace_is_reported_not_run(files, monkeypatch, dms):
    started = []

    async def dies(label, brief, skill="", occurrence=""):
        raise Crash()

    async def start(label, brief, skill="", occurrence=""):
        started.append(label)
        return ""
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    monkeypatch.setattr(background, "start_scheduled", dies)
    with pytest.raises(Crash):
        run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    _restart()
    monkeypatch.setattr(background, "start_scheduled", start)
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 16, 0)))
    assert started == []
    assert len(dms) == 1 and "Wed 15:30" in dms[0] and TASK in dms[0]
    assert chronos._load_state().get("due_task_runs") in (None, [])
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 16, 1)))
    assert len(dms) == 1                                    # reported once


def test_the_trail_says_run_only_once_the_run_has_started(files, monkeypatch, dms):
    busy = {"n": 1}

    async def start(label, brief, skill="", occurrence=""):
        if busy["n"]:
            busy["n"] -= 1
            return "Error: busy"
        return ""
    monkeypatch.setattr(background, "start_scheduled", start)
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    assert not [l for l in chronos._read_schedule() if "task run:" in l]
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 31)))
    assert len([l for l in chronos._read_schedule() if "task run:" in l]) == 1


def test_a_run_already_queued_is_never_started_twice(files, monkeypatch, dms):
    """A crash after the job was durably queued but before chronos forgot the run: the
    restarted process finds the job by its occurrence key and does not queue another."""
    monkeypatch.setattr(background, "_tasks", {})
    monkeypatch.setattr(background, "_next_id", 1)
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)
    monkeypatch.setattr(background._pause, "is_paused", lambda *a: False)
    real_forget = chronos._forget_due_run
    monkeypatch.setattr(chronos, "_forget_due_run", lambda key: (_ for _ in ()).throw(Crash()))
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    with pytest.raises(Crash):
        run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    _restart()
    background._tasks.clear()
    monkeypatch.setattr(chronos, "_forget_due_run", real_forget)
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 31)))
    conn = jobs._conn()
    n = conn.execute("SELECT count(*) FROM derivation_jobs").fetchone()[0]
    assert n == 1
    assert chronos._load_state().get("due_task_runs") in (None, [])


def test_the_occurrence_key_is_stored_with_the_queued_job(files, monkeypatch):
    monkeypatch.setattr(background, "_tasks", {})
    monkeypatch.setattr(background, "_next_id", 1)
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)
    monkeypatch.setattr(background._pause, "is_paused", lambda *a: False)
    _schedule_due_task(paris(2026, 9, 30, 15, 30))
    run(chronos._fire_due_tasks(paris(2026, 9, 30, 15, 30)))
    [t] = background._tasks.values()
    payload = json.loads(jobs.get(t.job_id)["payload"])
    assert payload["occurrence"] and jobs.has_occurrence(payload["occurrence"])


# --- D7 -------------------------------------------------------------------------------

LONG_NIGHT = ("research the history of the ECB's negative-rate period, " * 6).strip()


def _queue_night(text=LONG_NIGHT):
    chronos._write_schedule([chronos._format_night_line("2026-09-30", "", text)])


def test_a_night_task_cut_off_by_a_restart_is_reported_with_its_full_text(files, monkeypatch):
    assert len(LONG_NIGHT) > 200                     # longer than a run's stored preview

    async def dies(prompt):
        raise Crash()
    monkeypatch.setattr(background, "execute_task", dies)
    _queue_night()
    with pytest.raises(Crash):
        run(chronos._run_night_task(paris(2026, 10, 1, 3, 0)))
    assert chronos.recover_night_task(paris(2026, 10, 1, 8, 0)) is True
    [r] = chronos._load_state()["night_results"]
    assert r["task"] == LONG_NIGHT and r["status"] == "interrupted"
    assert not [n for n in map(chronos._parse_night_line, chronos._read_schedule()) if n]
    assert any("night task interrupted" in l for l in chronos._read_schedule())
    assert chronos.recover_night_task(paris(2026, 10, 1, 8, 1)) is False   # once


def test_a_crash_before_the_pop_reports_nothing_and_keeps_the_task(files, monkeypatch):
    _queue_night()
    real = chronos._write_schedule
    monkeypatch.setattr(chronos, "_write_schedule",
                        lambda lines: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        run(chronos._run_night_task(paris(2026, 10, 1, 3, 0)))
    monkeypatch.setattr(chronos, "_write_schedule", real)
    assert chronos.recover_night_task(paris(2026, 10, 1, 8, 0)) is False
    assert [n for n in map(chronos._parse_night_line, chronos._read_schedule()) if n]
    assert not chronos._load_state().get("night_results")


def test_a_failed_night_task_writes_no_draft_and_is_not_trailed_done(files, monkeypatch):
    writes = []

    async def broken(prompt):
        raise background.IncompleteRun(background.RunResult(text="", status="incomplete",
                                                            ending="cut_off"))

    async def vault_write(filename, content, folder):
        writes.append(filename)
        return "Confirmed"
    monkeypatch.setattr(background, "execute_task", broken)
    monkeypatch.setattr("backend.tools.vault.vault_write", vault_write)
    _queue_night()
    run(chronos._run_night_task(paris(2026, 10, 1, 3, 0)))
    assert writes == []
    assert not any("night task done" in l for l in chronos._read_schedule())
    assert any("night task failed" in l for l in chronos._read_schedule())
    [r] = chronos._load_state()["night_results"]
    assert r["status"] == "failed" and "cut off" in r["summary"] and r["task"] == LONG_NIGHT
    assert not chronos._load_state().get("night_running")


def test_a_successful_night_task_still_writes_its_note(files, monkeypatch):
    writes = []

    async def ok(prompt):
        return "the findings"

    async def vault_write(filename, content, folder):
        writes.append(filename)
        return "Confirmed"
    monkeypatch.setattr(background, "execute_task", ok)
    monkeypatch.setattr("backend.tools.vault.vault_write", vault_write)
    _queue_night()
    run(chronos._run_night_task(paris(2026, 10, 1, 3, 0)))
    assert len(writes) == 1
    [r] = chronos._load_state()["night_results"]
    assert r["summary"] == "the findings" and r.get("status", "done") == "done"
    assert any("night task done" in l for l in chronos._read_schedule())
    assert not chronos._load_state().get("night_running")
