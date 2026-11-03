"""G0 / D6, N3: what a durable background job keeps across a restart.

D6: a routine's skill was set on the task AFTER `_start` had already queued its payload,
and the payload had no skill field, so a recovered routine lost "· followed: <skill>" and
the correction link. N3: the payload kept only the 1,200-char summary, not the full result
or whether it had reached Sam, so a restarted process could neither show the result
nor know it was already sent.
"""
import asyncio

import pytest

from backend.brain import background as B
from backend.storage import jobs


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(B, "_tasks", {})
    monkeypatch.setattr(B, "_next_id", 1)
    monkeypatch.setattr(B, "_shutting_down", False)
    # admit but never run: the payload at admission is what is under test
    monkeypatch.setattr(B, "_schedule_queued", lambda: None)
    monkeypatch.setattr(B._pause, "is_paused", lambda *a: False)


def test_a_routine_skill_is_in_the_payload_when_it_is_queued():
    assert run(B.start_scheduled("scheduled Mon 09:00: research TTWO", "brief",
                                 skill="stock-research")) == ""
    t = B._tasks[1]
    row = jobs.get(t.job_id)
    import json
    assert json.loads(row["payload"])["skill"] == "stock-research"


def test_a_recovered_routine_keeps_its_skill():
    run(B.start_scheduled("scheduled Mon 09:00: research TTWO", "brief", skill="stock-research"))
    B._tasks.clear()
    assert B.recover_durable_jobs() == 1
    assert B._tasks[1].skill == "stock-research"


def test_a_finished_jobs_full_result_and_delivery_survive_a_restart(monkeypatch):
    long = "x" * 5000

    async def runner(text, **kw):
        return B.RunResult(text=long)

    async def notifier(text):
        return True

    async def note(t):
        return None
    monkeypatch.setattr(B, "_run_compat_loop", runner)
    monkeypatch.setattr(B, "_write_result_note", note)
    monkeypatch.setattr(B, "_notifier", notifier)
    t, err = run(B._start("research", "brief", notify=True))
    assert err == ""
    t.status = "running"
    run(B._worker(t))
    assert t.dm_sent and len(t.summary) == B._SUMMARY_CAP
    B._tasks.clear()
    B.recover_durable_jobs()
    back = B._tasks[t.id]
    assert back.result == long                        # the full text, not the 1,200 cap
    assert back.dm_sent is True
    notice = B.pop_completed_notices()
    assert "Already sent to Sam" in notice


def test_an_old_payload_without_the_new_fields_still_recovers():
    job_id = jobs.enqueue({"display_id": 7, "description": "old job", "brief": "",
                           "started": 1.0, "finished": None, "summary": "", "note_file": "",
                           "announced": False, "deep": False, "notify": False,
                           "kind": "agent", "depth": "standard"})
    assert job_id
    assert B.recover_durable_jobs() == 1
    t = B._tasks[7]
    assert (t.skill, t.result, t.dm_sent, t.occurrence) == ("", "", False, "")
