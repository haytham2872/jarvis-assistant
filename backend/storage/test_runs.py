"""A02: a turn killed mid-flight leaves a durable, recoverable record.

Roadmap acceptance: "shutdown records recoverable interrupted work", and the rule that
an uncertain remote action must stay uncertain rather than be blindly repeated.
"""
import pytest

from backend.storage import db, runs


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def test_run_opens_running_and_closes_completed():
    rid = runs.begin_run("discord", "what's the weather")
    assert runs.get_run(rid)["state"] == runs.RUNNING
    runs.finish_run(rid, runs.COMPLETED, session_id="sess-1")
    row = runs.get_run(rid)
    assert row["state"] == runs.COMPLETED
    assert row["session_id"] == "sess-1"
    assert row["ended_at"] is not None


def test_failed_run_keeps_its_error():
    rid = runs.begin_run("local", "do the thing")
    runs.finish_run(rid, runs.FAILED, error="provider exploded")
    row = runs.get_run(rid)
    assert row["state"] == runs.FAILED
    assert row["error"] == "provider exploded"


def test_reconcile_marks_only_still_open_runs(monkeypatch):
    """The restart case: a process died with a turn in flight."""
    finished = runs.begin_run("local", "finished turn")
    runs.finish_run(finished, runs.COMPLETED)
    orphan_a = runs.begin_run("discord", "killed mid-tool-call")
    orphan_b = runs.begin_run("local", "also killed")

    assert runs.reconcile_interrupted() == 2

    assert runs.get_run(finished)["state"] == runs.COMPLETED      # untouched
    assert runs.get_run(orphan_a)["state"] == runs.INTERRUPTED
    assert runs.get_run(orphan_b)["state"] == runs.INTERRUPTED


def test_reconcile_is_idempotent():
    runs.begin_run("local", "x")
    assert runs.reconcile_interrupted() == 1
    assert runs.reconcile_interrupted() == 0      # nothing left running


def test_interrupted_run_is_not_deleted_so_it_stays_investigable():
    """Marking, not deleting: the record of a cut-off turn is what makes its side
    effects findable. Nothing here re-runs anything."""
    rid = runs.begin_run("discord", "send the email")
    runs.reconcile_interrupted()
    listed = runs.interrupted_runs()
    assert [r["run_id"] for r in listed] == [rid]
    assert listed[0]["user_preview"] == "send the email"


def test_user_preview_is_bounded():
    """A preview to recognise the run by -- not a second transcript store."""
    rid = runs.begin_run("local", "x" * 5000)
    assert len(runs.get_run(rid)["user_preview"]) == runs._PREVIEW_CHARS


def test_finish_run_tolerates_a_none_id():
    """begin_run returns None when storage is down; callers must need no branch."""
    assert runs.finish_run(None, runs.COMPLETED) is False


def test_everything_degrades_when_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert runs.begin_run("local", "x") is None
    assert runs.finish_run("whatever", runs.COMPLETED) is False
    assert runs.reconcile_interrupted() == 0
    assert runs.interrupted_runs() == []
    assert runs.get_run("whatever") is None


def test_turn_wiring_closes_the_run_on_every_exit_path():
    """Static check of main.py: the three deliberate exits each set a state, and the
    finally closes the run. main.py imports real audio-free now, but driving a full turn
    here would need the whole pipeline -- the states are what matter."""
    import inspect

    import backend.main as m

    src = inspect.getsource(m._execute_turn_once)
    assert "_runs.begin_run(" in src
    assert "_run_state = _runs.COMPLETED" in src
    assert "_run_state = _runs.INTERRUPTED" in src
    assert "_runs.FAILED" in src
    assert "_runs.finish_run(_run_id, _run_state" in src


def test_startup_reconciles_before_admitting_work():
    """Ordering matters: reconciliation must run before the runtime accepts a message,
    or a new run could be swept up as interrupted."""
    import inspect

    import backend.main as m

    src = inspect.getsource(m.lifespan)
    assert "_runs.reconcile_interrupted()" in src
    assert src.index("_runs.reconcile_interrupted()") < src.index("yield")
