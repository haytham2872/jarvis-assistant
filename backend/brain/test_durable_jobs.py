"""A06 (slice 1): failed searches are not evidence, and interrupted work is reported.

Two acceptance criteria from the roadmap:

- *"a research search failure never becomes synthesized evidence"* — `web_browse`
  signals total failure by **returning** a string ("SEARCH FAILED …", "Could not fetch
  …"), not by raising. `research._tick` appended every result to `findings`
  unconditionally and only skipped on an exception, so synthesis was handed the failure
  text and asked to fold "what you learned" into the note. Reproduced before fixing.

- *"a queued task survives restart"* — A04 slice 3 already gives every autonomous task
  its own durable run and A02 marks anything still `running` as `interrupted` at
  startup, but **nothing consumed that**. A background task killed by a restart was
  recorded in SQLite and never mentioned again.
"""
import pytest

from backend.brain import background
from backend.storage import db, runs, jobs
from backend.tools import outcomes


# ------------------------------------------- failed searches are not evidence
SEARCH_FAILED = ("SEARCH FAILED - no live data available for this query. Do not pass "
                 "off training-data figures as current.")
FETCH_FAILED = "Could not fetch https://example.test: timed out"
REAL_RESULT = "NVIDIA closed at $205.10, down 6.2% on the day, per three sources."


@pytest.mark.parametrize("failure", [SEARCH_FAILED, FETCH_FAILED])
def test_the_classifier_recognises_a_returned_search_failure(failure):
    """The fix rests on this: these are failures even though nothing raised."""
    assert outcomes.is_failure("web_browse", failure)


def test_a_real_result_is_not_mistaken_for_a_failure():
    assert not outcomes.is_failure("web_browse", REAL_RESULT)


def _assemble(results):
    """Mirror of _tick's findings loop, exercising the real classifier."""
    findings_parts, failed = [], []
    for q, result in results:
        if isinstance(result, Exception):
            failed.append(q)
            continue
        if outcomes.is_failure("web_browse", result):
            failed.append(q)
            continue
        findings_parts.append(f"### Query: {q}\n{result}")
    return findings_parts, failed


def test_the_reproduced_incident_every_search_failed():
    parts, failed = _assemble([("q1", SEARCH_FAILED), ("q2", SEARCH_FAILED)])
    assert parts == [], "search failures were passed to synthesis as findings"
    assert len(failed) == 2
    assert "\n\n".join(parts).strip() == "", "the all-failed guard would not fire"


def test_a_partial_failure_keeps_the_real_result():
    """All-or-nothing would throw away a good search because a sibling failed."""
    parts, failed = _assemble([("q1", SEARCH_FAILED), ("q2", REAL_RESULT)])
    assert len(parts) == 1 and REAL_RESULT in parts[0]
    assert failed == ["q1"]


def test_a_raised_search_is_still_skipped():
    parts, failed = _assemble([("q1", RuntimeError("boom")), ("q2", REAL_RESULT)])
    assert len(parts) == 1 and failed == ["q1"]


def test_tick_excludes_failures_and_flags_partial_coverage():
    """Static guards on the real function -- the loop is deep inside _tick and cannot
    be driven without a full picker/synthesis stack."""
    import inspect
    from backend.brain import research

    src = inspect.getsource(research._tick)
    assert '_outcomes.is_failure("web_browse", result)' in src, (
        "search results are appended to findings without being classified")
    append_at = src.index('findings_parts.append(f"### Query:')
    check_at = src.index('_outcomes.is_failure("web_browse", result)')
    assert check_at < append_at, "the result is appended before it is classified"
    assert "do not fill the gap from prior knowledge" in src, (
        "a partially-failed search set is presented as complete coverage")


def test_a_fully_failed_tick_does_not_advance_last_researched():
    """Nothing is written, so the subject stays eligible and is retried rather than
    being recorded as researched on no data."""
    import inspect
    from backend.brain import research

    src = inspect.getsource(research._tick)
    guard = src.index('"failed: all searches failed"')
    first_write = src.index('vault_write(filename')
    assert guard < first_write, "the no-results guard runs after a note write"


# ------------------------------------------------- interrupted work is reported
@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    background._tasks.clear()
    yield
    background._tasks.clear()
    db._reset_for_tests()


def test_open_runs_sees_what_the_previous_process_was_doing():
    runs.begin_run("background", "dig into the NVDA thesis")
    done = runs.begin_run("discord", "hey")
    runs.finish_run(done)
    assert [r["channel"] for r in runs.open_runs()] == ["background"]


def test_open_runs_can_be_scoped_to_channels():
    runs.begin_run("background", "a")
    runs.begin_run("discord", "b")
    assert [r["channel"] for r in runs.open_runs(("background",))] == ["background"]


def test_an_interrupted_background_task_is_reported_on_the_next_turn():
    """The gap: it was recorded in SQLite and never mentioned again."""
    runs.begin_run("background", "dig into whether my NVDA thesis still holds")
    interrupted = runs.open_runs(background._AUTONOMOUS_CHANNELS)
    runs.reconcile_interrupted()

    assert background.recover_interrupted(interrupted) == 1
    notice = background.pop_completed_notices()
    assert "NVDA thesis" in notice
    assert "Interrupted" in notice


def test_a_cut_off_live_turn_gets_no_notice():
    """Sam was there and saw it stop -- only autonomous work needs reporting."""
    runs.begin_run("discord", "hey what's up")
    interrupted = runs.open_runs(background._AUTONOMOUS_CHANNELS)
    assert background.recover_interrupted(interrupted) == 0
    assert background.pop_completed_notices() == ""


def test_night_tasks_are_recovered_too():
    runs.begin_run("night_task", "research the FOMC decision overnight")
    rows = runs.open_runs(background._AUTONOMOUS_CHANNELS)
    assert background.recover_interrupted(rows) == 1
    assert "FOMC" in background.pop_completed_notices()


def test_the_notice_is_delivered_exactly_once():
    runs.begin_run("background", "a task")
    background.recover_interrupted(runs.open_runs(background._AUTONOMOUS_CHANNELS))
    assert background.pop_completed_notices() != ""
    assert background.pop_completed_notices() == ""


def test_recovery_never_retries_the_work():
    """Its side effects may have landed; the roadmap forbids blind retry."""
    runs.begin_run("background", "send the summary email")
    started = len(background._tasks)
    background.recover_interrupted(runs.open_runs(background._AUTONOMOUS_CHANNELS))

    assert len(background._tasks) == started + 1
    task = list(background._tasks.values())[-1]
    assert task.status == "failed"
    assert task._handle is None, "recovery spawned a worker"
    assert "not retried automatically" in task.summary


def test_a_run_with_no_preview_still_reports():
    runs.begin_run("background", "")
    assert background.recover_interrupted(
        runs.open_runs(background._AUTONOMOUS_CHANNELS)) == 1
    assert "unnamed task" in background.pop_completed_notices()


def test_recovery_degrades_when_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert runs.open_runs() == []
    assert background.recover_interrupted([]) == 0


def test_main_reads_open_runs_before_reconciling():
    """Reconciling first would clear the state this reads -- nothing to recover."""
    import inspect
    from backend import main

    src = inspect.getsource(main)
    read_at = src.index("_runs.open_runs(background._AUTONOMOUS_CHANNELS)")
    reconcile_at = src.index("_runs.reconcile_interrupted()")
    assert read_at < reconcile_at, (
        "open runs are read after reconciliation, so none would ever be found")


def _job_payload(display_id=7):
    return {"display_id": display_id, "description": "check the NVDA thesis",
            "brief": "check the NVDA thesis with current evidence",
            "started": 100.0, "finished": None, "summary": "",
            "note_file": "", "announced": False}


def test_queued_job_survives_restart_and_is_claimed_once(monkeypatch):
    job_id = jobs.enqueue(_job_payload())
    assert job_id
    assert background.recover_durable_jobs() == 1
    assert background._tasks[7].status == "queued"
    assert background._tasks[7].brief.endswith("current evidence")

    started = []

    async def fake_worker(task):
        started.append(task.job_id)
        assert jobs.claim(task.job_id) is True
        assert jobs.claim(task.job_id) is False

    monkeypatch.setattr(background, "_worker", fake_worker)

    async def scenario():
        background.start_queued_jobs()
        await background._tasks[7]._handle

    import asyncio
    asyncio.run(scenario())
    assert started == [job_id]


def test_started_job_is_reported_but_never_retried():
    job_id = jobs.enqueue(_job_payload())
    assert jobs.claim(job_id) is True
    runs.begin_run("background", "check the NVDA thesis", run_id=job_id)
    open_rows = runs.open_runs(background._AUTONOMOUS_CHANNELS)
    runs.reconcile_interrupted()

    assert background.recover_durable_jobs() == 1
    assert background._tasks[7].status == "failed"
    assert background._tasks[7]._handle is None
    assert background.recover_interrupted(open_rows) == 0  # no duplicate legacy notice
    assert "not retried automatically" in background.pop_completed_notices()

    background._tasks.clear()
    background.recover_durable_jobs()
    assert background.pop_completed_notices() == ""  # acknowledgement persisted


def test_finished_job_result_is_rehydrated_after_restart():
    payload = _job_payload()
    job_id = jobs.enqueue(payload)
    assert jobs.claim(job_id) is True
    payload.update(summary="The thesis needs fresh evidence.",
                   note_file="drafts/nvda.md", finished=150.0)
    assert jobs.finish(job_id, "done", payload)

    assert background.recover_durable_jobs() == 1
    notice = background.pop_completed_notices()
    assert "fresh evidence" in notice
    assert "drafts/nvda.md" in notice


def test_storage_failure_does_not_start_an_unrecoverable_job(monkeypatch):
    monkeypatch.setattr(jobs, "enqueue", lambda payload: None)
    before = len(background._tasks)
    import asyncio
    result = asyncio.run(background.run_in_background("research the release notes"))
    assert "storage is unavailable" in result
    assert len(background._tasks) == before
