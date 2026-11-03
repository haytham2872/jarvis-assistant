"""A03 slice 7: the planner no longer plans from bare task text.

Roadmap rule: "Planning/replanning: same goal/constraints as the executor ... never plan
from bare task text when context already exists." `_plan_task` saw only the request
string and a list of tool names, so it could not know a note on the subject already
existed or that related work was already open — and planned a redundant first step
accordingly.
"""
import pytest

from backend.brain import engine
from backend.context import budget, origin, profiles, service


@pytest.fixture(autouse=True)
def _clean():
    origin.clear_origin()
    yield
    origin.clear_origin()


# ---------------------------------------------------------------- the snapshot
def test_planning_snapshot_carries_goal_queue_and_topic():
    snap = service.build_planning(
        "should I add to NVDA?",
        queue="[open] review the NVDA entry",
        relevant_topic="NVDA: bought at 118, datacenter thesis.")
    ids = snap.source_ids()
    assert profiles.USER_REQUEST in ids
    assert profiles.QUEUE in ids
    assert profiles.TOPIC in ids


def test_planning_profile_excludes_identity_and_mood_layers():
    """Irrelevant to producing a step list, and pure cost on a latency-sensitive
    pre-pass."""
    for banned in (profiles.SELF_MODEL, profiles.MOOD, profiles.PREDICTIONS,
                   profiles.POSITIONS, profiles.SLEEP):
        assert not profiles.PLANNING.allows(banned)


def test_planning_window_is_smaller_than_foreground():
    """This call emits a 2-5 step JSON list, not prose."""
    assert service.PLANNING_WINDOW < budget.DEFAULT_CONTEXT_WINDOW
    assert service.PLANNING_RESERVATION.messages == 0   # single-shot, no history


def test_user_request_is_non_droppable_in_planning():
    snap = service.build_planning("do the thing", queue="q" * 100000)
    request = [f for f in snap.fragments if f.source_id == profiles.USER_REQUEST]
    assert request and request[0].droppable is False


def test_oversized_context_is_dropped_not_the_goal():
    snap = service.build_planning("do the thing", relevant_topic="z" * 400000)
    assert profiles.USER_REQUEST in snap.source_ids()
    assert profiles.TOPIC not in snap.source_ids()
    assert snap.omissions


# ---------------------------------------------------------------- rendering
def test_rendered_block_labels_each_source():
    snap = service.build_planning("q", queue="[open] item",
                                  relevant_topic="known facts here")
    text = service.render_planning_context(snap)
    assert "Open intents" in text and "[open] item" in text
    assert "already known" in text and "known facts here" in text


def test_rendered_block_is_empty_when_there_is_nothing():
    snap = service.build_planning("q")
    assert service.render_planning_context(snap) == ""


# ------------------------------------------------- wired into _plan_task
def test_planner_context_comes_from_the_turns_origin():
    origin.set_origin("should I add to NVDA?",
                      queue="[open] review the NVDA entry",
                      relevant_topic="NVDA: bought at 118.")
    text = engine._planning_context("should I add to NVDA?")
    assert "review the NVDA entry" in text
    assert "bought at 118" in text


def test_planner_context_is_empty_without_an_origin():
    """A chronos night task or a direct call has no originating turn; the planner then
    behaves exactly as it did before this slice."""
    assert engine._planning_context("anything") == ""


def test_planner_context_never_raises(monkeypatch):
    from backend.context import service as svc

    def _boom(*a, **kw):
        raise RuntimeError("context exploded")

    origin.set_origin("q", queue="x")
    monkeypatch.setattr(svc, "build_planning", _boom)
    assert engine._planning_context("q") == ""


def test_plan_prompt_includes_the_context_when_present():
    """Static check of the call site: the block must actually reach the prompt."""
    import inspect

    src = inspect.getsource(engine._plan_task)
    assert "_planning_context(user_text)" in src
    assert "Context you already have" in src
    # ...and must stay conditional, so a context-free call is byte-identical to before.
    assert 'if plan_context else ""' in src
