"""G3 Part B: steering a running goal, /goal control, and what the brief shows.

Same fixture as G2 (real runner, storage, policy; clock, notifier and provider faked).
"""
import asyncio

from backend.brain import chronos
from backend.brain import goal_runner as R
from backend.brain import goals as GT
from backend.brain import steering
from backend.storage import goals as S
from backend.tools import policy

from backend.brain.test_goal_g2 import _goal, _session, world  # noqa: F401  (fixture)
from backend.brain.test_goal_runner import run


def _steer(g, text="only 2-bedroom flats", key="m-1"):
    with steering.turn("discord:dm", "run-1", key):
        return run(GT._steer(goal_id=g["goal_id"], text=text))


def test_steer_needs_his_words(world):
    g = _goal()
    world.say("what's the weather", quote="only 2-bedroom flats")
    assert "needs his words" in _steer(g)
    assert S.events(g["goal_id"], "steer") == []


def test_steer_is_recorded_once_per_message_and_reaches_the_next_session(world):
    g = _goal()
    world.say("only 2-bedroom flats please")
    assert "Noted" in _steer(g)
    assert "Already noted" in _steer(g)                       # a redelivered message
    [e] = S.events(g["goal_id"], "steer")
    assert e["source_message_id"] == "m-1" and e["conversation_id"] == "discord:dm"
    ms = next(m for m in S.plan(g["goal_id"]) if m["milestone_id"] == "M1")
    text = R.brief(S.get(g["goal_id"]), ms, "sess-x")
    assert "Sam's direction" in text and "only 2-bedroom flats" in text
    # it changes no requirement
    assert [c["text"] for c in S.active_criteria(S.get(g["goal_id"]))] == ["a shortlist of flats"]


def test_a_second_message_is_a_second_steer(world):
    g = _goal()
    world.say("only 2-bedroom flats please")
    _steer(g, key="m-1")
    world.say("and nothing above the 4th floor")
    assert "Noted" in _steer(g, text="nothing above the 4th floor", key="m-2")
    assert len(S.events(g["goal_id"], "steer")) == 2


def test_an_ended_or_proposed_goal_is_not_steered(world):
    g = _goal()
    world.say("cancel the flat hunt")
    run(GT._cancel(goal_id=g["goal_id"]))
    world.say("only 2-bedroom flats please")
    assert "already ended" in _steer(g)
    p = _goal(state=S.PROPOSED, words="another one")
    assert not S.add_steer(p["goal_id"], text="x", words="x", key="k")


def test_goal_control_carries_his_authority(world):
    g = _goal()
    out = run(GT.handle_control(f"{g['goal_id']} pause"))
    assert S.get(g["goal_id"])["state"] == S.PAUSED, out
    run(GT.handle_control(f"{g['goal_id']} resume"))
    assert S.get(g["goal_id"])["state"] == S.ACTIVE
    run(GT.handle_control(f"{g['goal_id']} cancel"))
    assert S.get(g["goal_id"])["state"] == S.CANCELLED
    assert "Usage" in run(GT.handle_control(f"{g['goal_id']} explode"))
    assert "Usage" in run(GT.handle_control(""))


def test_goal_control_authority_ends_with_the_command(world):
    """Checked inside one coroutine: asyncio.run copies the context, so a leak could never be
    seen from outside it."""
    g = _goal()

    async def go():
        before = policy.current_caller.get()
        await GT.handle_control(f"{g['goal_id']} pause")
        return before, policy.current_caller.get(), policy.call_quote.get()
    before, after, quote = run(go())
    assert after == before and after != policy.APPROVED
    assert not (quote or "").startswith("/goal")


def test_the_brief_names_waiting_goals_and_their_questions(world):
    g = _goal()
    run(_session(g).goal_finish("ask", "x", op_key="q", question="Max rent?",
                                blocked_milestone_ids=["M1", "M2"]))
    S.set_milestone(g["goal_id"], "M1", 1, S.WAITING_MS, expect=(S.READY,))
    run(R.reconcile_waits())
    assert S.get(g["goal_id"])["state"] == S.WAITING
    line = chronos._collect_goals_line()
    assert "Flat hunt (waiting)" in line and "Waiting on your answer: Max rent?" in line


def test_the_discord_bridge_routes_goal_not_goals():
    from backend.brain import discord_bridge as D
    assert any(c.startswith("/goal <id>") for c, _ in D._COMMAND_HELP)
    calls = []

    async def ctl(arg):
        calls.append(arg)
        return "ok"
    D.set_goal_control_handler(ctl)
    try:
        assert asyncio.run(D._goal_control_handler("g1 pause")) == "ok"
    finally:
        D.set_goal_control_handler(None)
