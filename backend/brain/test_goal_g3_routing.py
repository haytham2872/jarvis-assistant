"""G3 Part C: Jev's `goal` question and the goal-offer route (docs/archive/GOALS-ROADMAP.md section 12).

- the question is in the same Jev call, and its threshold is the measured one;
- an offer only comes from Jev's answer, only while goals run, and never while a proposal
  waits or a goal's question asked today may be what he is answering;
- an offer turn keeps its thinking tier but runs no foreground plan (the real _chat_compat
  loop, with only the stream and the planner faked);
- an offer never authorizes a start: goal start still needs his words or a tap.
"""
import asyncio
import inspect

import pytest

from backend.brain import engine
from backend.brain import goal_runner as R
from backend.brain import goals as GT
from backend.brain import turn_signals as ts
from backend.brain.router import OPENROUTER
from backend.storage import goals as S
from backend.tools import policy

from backend.brain.test_goal_g2 import _goal, _session, world  # noqa: F401  (fixture)
from backend.brain.test_goal_runner import run


# ------------------------------------------------------------------ the question

def test_goal_is_asked_in_the_same_call_with_the_measured_threshold():
    body = ts._request_body("keep looking for a flat over the next two weeks")
    assert "goal" in body["questions"] and "complex" in body["questions"]
    assert ts.THRESHOLDS["goal"] == 0.91
    assert "goal" not in ts.KEYWORD_OR          # no keyword fallback (see CLAUDE.md, G3)


def _answers(goal_p, complex_p=0.9):
    a = {k: {"noul": 0.0} for k in ts.QUESTIONS}
    a["goal"] = {"noul": goal_p}
    a["complex"] = {"noul": complex_p}
    a["sections"] = {"probabilities": {}}
    return a


def test_interpret_flags_goal_at_the_threshold_only():
    assert ts.interpret(_answers(0.91), "x").get("goal")
    assert not ts.interpret(_answers(0.90), "x").get("goal")
    # an ongoing-sounding phrase with a low probability stays false: no keyword OR
    assert not ts.interpret(_answers(0.2), "keep looking over the next two weeks").get("goal")


# ------------------------------------------------------------------ the offer

def test_no_offer_without_a_positive_jev_answer(world):
    assert GT.offer_line(None) == ""            # Jev off, late or failed
    assert GT.offer_line(False) == ""
    assert GT.offer_line(True) == GT.OFFER_LINE


def test_no_offer_when_goals_are_off(world, monkeypatch):
    monkeypatch.setenv("JARVIS_GOALS", "0")
    assert GT.offer_line(True) == ""


def test_no_reoffer_while_a_proposal_waits(world):
    _goal(state=S.PROPOSED, words="find me a flat")
    assert GT.offer_line(True) == ""


def test_no_offer_while_a_goal_question_from_today_is_open(world):
    g = _goal()
    run(_session(g).goal_finish("ask", "x", op_key="q", question="Max rent?",
                                blocked_milestone_ids=["M1"]))
    assert GT.offer_line(True) == ""


def test_an_offer_never_authorizes_a_start(world, monkeypatch):
    """The flag only adds a line to the turn; goal start still goes through the gateway,
    which needs his words in this message (or a tap)."""
    from backend.tools import approval_flow, gateway
    asked = []

    async def request(**kw):
        asked.append(kw)
        return {"id": 7, "state": "pending"}
    monkeypatch.setattr(approval_flow, "request", request)
    monkeypatch.setattr(approval_flow, "waiting_text", lambda row, ctx: "waiting #7")
    world.say("I'm looking for a flat in Lyon", quote="")
    assert GT.offer_line(True) == GT.OFFER_LINE           # Jev said goal
    res = run(gateway.execute("goal", {"action": "start", "title": "Flat hunt",
                                       "criteria": ["a shortlist"],
                                       "quote": "keep looking for a flat for me"},
                              dispatch=engine.TOOL_DISPATCH, caller=policy.LIVE))
    assert res.pending and asked                           # a request, not a start
    assert S.list_goals((S.ACTIVE,), limit=5) == []


# ------------------------------------------------------------------ the route

async def _noop_send(msg):
    pass


@pytest.fixture
def compat(monkeypatch):
    monkeypatch.setattr(engine, "_provider", lambda: OPENROUTER)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    seen = {"planned": 0, "extra_body": []}

    async def plan(client, user_text, tools):
        seen["planned"] += 1
        return [{"id": 1, "label": "Search", "tool": "web_browse", "parallel_group": None}]
    monkeypatch.setattr(engine, "_plan_task", plan)

    async def stream(client, model, messages, send, sentence_queue, temperature=None,
                     extra_body=None, tools=None, *, purpose="chat", iteration=None,
                     plan_active=False, max_tokens=None):
        seen["extra_body"].append(extra_body)
        return "Shall I make this a goal?", "", []
    monkeypatch.setattr(engine, "_run_stream_openai", stream)
    engine._reset_turn_stats()
    yield seen
    engine._reset_turn_stats()


def _turn(allow):
    return asyncio.run(engine._chat_compat(
        OPENROUTER.thinking, "system", [{"role": "user", "content": "keep looking"}],
        _noop_send, None, user_text="keep looking for a flat for two weeks", channel="local",
        tools=[], use_thinking=True, allow_foreground_plan=allow))


def test_an_offer_turn_keeps_thinking_but_runs_no_plan(compat):
    _turn(allow=False)
    assert compat["planned"] == 0
    assert compat["extra_body"][0]["reasoning"] == OPENROUTER.thinking_body["reasoning"]


def test_an_ordinary_complex_turn_still_plans(compat):
    _turn(allow=True)
    assert compat["planned"] == 1
    assert compat["extra_body"][0]["reasoning"] == OPENROUTER.thinking_body["reasoning"]


def test_chat_wires_the_offer_into_the_turn():
    src = inspect.getsource(engine.chat)
    tier = src.index("route_tier(")
    offer = src.index("_goals.offer_line(")
    assert tier < offer                                  # decided after Jev resolves
    assert 'signals.get("goal") if signals else None' in src
    assert "system_volatile" in src[offer:] and "+ goal_offer" in src
    assert "allow_foreground_plan=not goal_offer" in src
    # the offer is used in exactly four places (computed, checked, added to the turn, passed
    # as the planning gate): nothing else -- the thinking tier above all -- depends on it
    assert src.count("goal_offer") == 4, src.count("goal_offer")
    assert "use_thinking=use_thinking_tier,\n        allow_foreground_plan=not goal_offer and" in src
