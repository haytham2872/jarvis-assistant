"""A03 slice 3: a background job inherits the goal and evidence of the turn that spawned it.

The roadmap's headline motivation: "A background task knows the goal, constraints, and
evidence behind the request that created it." Today `run_in_background(task)` gets one
sentence and a minimal autonomous prompt, so a job from a nuanced conversation has no idea
why it exists.
"""
import asyncio

import pytest

from backend.brain import background
from backend.context import origin


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    origin.clear_origin()
    background._tasks.clear()
    yield
    origin.clear_origin()
    background._tasks.clear()


# ------------------------------------------------------------------- brief
def test_brief_carries_the_originating_request_and_evidence():
    origin.set_origin(
        "is my NVDA thesis still intact after the earnings miss?",
        queue="[open] review the NVDA entry",
        relevant_topic="NVDA: bought at 118, thesis is datacenter demand.",
    )
    brief = origin.brief_for("Compare NVDA and AMD datacenter revenue")

    assert "Compare NVDA and AMD datacenter revenue" in brief      # the objective
    assert "earnings miss" in brief                                 # why it was asked
    assert "bought at 118" in brief                                 # evidence at hand
    assert "review the NVDA entry" in brief                         # open intent


def test_brief_falls_back_to_the_bare_task_with_no_origin():
    """A chronos night task genuinely has no originating conversation; inventing one
    would be worse than omitting it."""
    assert origin.brief_for("research X tonight") == "research X tonight"


def test_brief_omits_sections_that_have_no_content():
    origin.set_origin("do the thing")
    brief = origin.brief_for("the task")
    assert "OBJECTIVE" in brief
    assert "ORIGINATING REQUEST" in brief
    assert "EVIDENCE" not in brief


def test_origin_fields_are_bounded():
    """A brief, not a transcript."""
    origin.set_origin("x" * 50000, queue="y" * 50000, relevant_topic="z" * 50000)
    got = origin.current_origin.get()
    for value in (got.user_text, got.queue, got.relevant_topic):
        assert len(value) == origin._MAX_FIELD_CHARS


def test_set_origin_never_raises_on_bad_input():
    origin.set_origin(None)          # type: ignore[arg-type]
    assert origin.brief_for("t")


# --------------------------------------------------- wired into run_in_background
def test_started_task_stores_the_inherited_brief(monkeypatch):
    monkeypatch.setattr(background, "_broadcast_safe", _noop)
    monkeypatch.setattr(background.asyncio, "create_task", lambda coro: _close(coro))

    origin.set_origin("why is the deploy flaky?",
                      relevant_topic="CI notes: the qdrant healthcheck was removed")
    run(background.run_in_background("investigate the flaky tests"))

    task = list(background._tasks.values())[-1]
    assert task.description == "investigate the flaky tests"
    assert "why is the deploy flaky?" in task.brief
    assert "qdrant healthcheck" in task.brief


def test_worker_runs_the_brief_not_the_bare_description(monkeypatch):
    """The point of the whole slice: the model must actually receive the brief."""
    seen = {}

    async def _fake_runner(text):
        seen["text"] = text
        return "done"

    monkeypatch.setattr(background, "_run_compat_loop", _fake_runner)
    monkeypatch.setattr(background, "_broadcast_safe", _noop)
    monkeypatch.setattr(background, "_write_result_note", _noop)

    task = background.BgTask(id=99, description="bare task",
                             brief="OBJECTIVE\nbare task\n\nORIGINATING REQUEST\nwhy?")
    run(background._worker(task))

    assert "ORIGINATING REQUEST" in seen["text"]
    assert seen["text"] != "bare task"


def test_worker_falls_back_when_there_is_no_brief(monkeypatch):
    seen = {}

    async def _fake_runner(text):
        seen["text"] = text
        return "done"

    monkeypatch.setattr(background, "_run_compat_loop", _fake_runner)
    monkeypatch.setattr(background, "_broadcast_safe", _noop)
    monkeypatch.setattr(background, "_write_result_note", _noop)

    run(background._worker(background.BgTask(id=98, description="bare task")))
    assert seen["text"] == "bare task"


def test_brief_failure_degrades_to_the_task_string(monkeypatch):
    """Never let context enrichment break starting a job."""
    def _boom(_task):
        raise RuntimeError("context exploded")

    monkeypatch.setattr(origin, "brief_for", _boom)
    assert background._inherited_brief("just the task") == "just the task"


async def _noop(*a, **kw):
    return None


def _close(coro):
    """Swallow the worker coroutine so run_in_background does not actually execute it."""
    coro.close()

    class _H:
        def done(self):
            return True

        def cancel(self):
            pass

    return _H()
