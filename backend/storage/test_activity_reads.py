"""The read-only queries /activity added to the stores that already own the data."""
from backend.storage import operations, runs, watches


def test_operation_outcomes_are_counted_from_the_given_time():
    operations.record_operation(tool="web_browse", outcome="ok", started_at=100.0)
    operations.record_operation(tool="web_browse", outcome="ok", started_at=200.0)
    operations.record_operation(tool="vault_read", outcome="failed", started_at=210.0)
    operations.record_operation(tool="gmail_action", outcome="unknown", started_at=220.0)
    assert operations.outcome_counts(150.0) == {"ok": 1, "failed": 1, "unknown": 1}
    assert operations.outcome_counts(0.0) == {"ok": 2, "failed": 1, "unknown": 1}
    assert operations.outcome_counts(10_000.0) == {}


def test_runs_are_counted_by_state_and_channel():
    import time
    from backend.storage import db
    a = runs.begin_run("discord", "hi")
    b = runs.begin_run("discord", "hi again")
    c = runs.begin_run("local", "desk")
    d = runs.begin_run("night_task", "research")
    runs.finish_run(a)
    runs.finish_run(b, "interrupted")
    runs.finish_run(c)
    since = time.time() - 60
    assert runs.counts_since(since) == {"completed": 2, "interrupted": 1, "running": 1}
    assert runs.counts_since(since, ["discord", "local"]) == {"completed": 2, "interrupted": 1}
    assert runs.counts_since(time.time() + 60) == {}


def test_only_finished_fire_once_watches_count_as_fired():
    def add(subject):
        return watches.add(subject=subject, condition="c", method="web", query="q",
                           every_s=3600.0, expires_at=10**10, now=1.0)
    fired_recent, fired_old, active, repeating = (add("A"), add("B"), add("C"), add("D"))
    watches.update(fired_recent, state=watches.FIRED, last_checked_at=500.0)
    watches.update(fired_old, state=watches.FIRED, last_checked_at=50.0)
    watches.update(active, last_checked_at=600.0)                       # still active: not fired
    assert [w["subject"] for w in watches.fired_since(100.0)] == ["A"]
    assert [w["subject"] for w in watches.fired_since(0.0)] == ["A", "B"]


def test_the_reads_never_raise_without_a_database(monkeypatch):
    from backend.storage import db
    monkeypatch.setattr(db, "connect", lambda: None)
    assert operations.outcome_counts(0.0) == {}
    assert runs.counts_since(0.0) == {}
    assert watches.fired_since(0.0) == []
