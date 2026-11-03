"""Max reasoning effort only on real evidence that a turn is stuck (2026-10-09).

On the server one max-effort call reasoned 21,317 tokens for 79s, 35% of all thinking-tier
model time in two weeks (scripts/measure_discord_speed.py). Max was reached on signals that
did not mean "stuck": three different calls to the same tool (ordinary research, including a
parallel batch the planner asks for), or a replan triggered by any failed tool call -- one
blocked page. Thinking harder fixes neither: the turn is missing data, not reasoning.

Standard -> thinking (a misrouted turn getting reasoning) is unchanged. Thinking -> max now
needs one of: the model asked to replan (request_replan), a call identical to one already
made in an earlier step, or the last three calls to one tool, across steps, all failing.
Driven through the real _chat_compat loop with only the stream, client, tools and planner
faked, inspecting the reasoning body each request actually carried.
"""
import asyncio
import itertools

import pytest

from backend.brain import engine, escalation
from backend.brain.router import OPENROUTER


def run(coro):
    return asyncio.run(coro)


async def _send(_m):
    pass


@pytest.fixture(autouse=True)
def _reset():
    engine._reset_turn_stats()
    yield
    engine._reset_turn_stats()


def _openrouter(monkeypatch):
    monkeypatch.setattr(engine, "_provider", lambda: OPENROUTER)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())


def _efforts(calls):
    return [(c.get("reasoning") or {}).get("effort", "none") if c else "none" for c in calls]


def _drive(monkeypatch, steps, *, ok=lambda name, args: True, plan=None, user_text=""):
    """steps: list of lists of (tool, args) per model call; then the turn answers."""
    bodies = []

    async def stream(client, model, messages, send, sq, temperature=None, extra_body=None,
                     tools=None, *, purpose="chat", iteration=None, plan_active=False,
                     max_tokens=None):
        i = len(bodies)
        bodies.append(extra_body)
        if i < len(steps):
            return "", "", [{"id": f"c{i}-{j}", "name": n, "input": a}
                            for j, (n, a) in enumerate(steps[i])]
        return "done", "", []

    async def exec_tool(tc, send):
        good = ok(tc["name"], tc.get("input") or {})
        return tc, ("result" if good else "Error: blocked"), good

    async def plan_task(client, text, tools):
        return plan or []

    async def replan(client, text, completed, errors, remaining):
        return remaining or [{"id": 9, "label": "answer", "tool": None, "parallel_group": None}]

    monkeypatch.setattr(engine, "_run_stream_openai", stream)
    monkeypatch.setattr(engine, "_exec_tool", exec_tool)
    monkeypatch.setattr(engine, "_plan_task", plan_task)
    monkeypatch.setattr(engine, "_replan", replan)
    run(engine._chat_compat(OPENROUTER.thinking, "system", [{"role": "user", "content": "hi"}],
                            _send, None, user_text=user_text, channel="local",
                            use_thinking=True))
    return _efforts(bodies)


_q = itertools.count()


def _search():
    return ("web_browse", {"task": f"query {next(_q)}"})


# --- ordinary research no longer reaches max ---------------------------------------------------

def test_varied_searches_do_not_escalate_to_max(monkeypatch):
    _openrouter(monkeypatch)
    efforts = _drive(monkeypatch, [[_search()] for _ in range(7)])
    assert "max" not in efforts and efforts[0] == "high"


def test_a_parallel_batch_of_different_searches_does_not_escalate(monkeypatch):
    _openrouter(monkeypatch)
    steps = [[("code_graph", {"action": "god_nodes"})]] * 4 + [[_search(), _search(), _search()]]
    efforts = _drive(monkeypatch, steps)
    assert "max" not in efforts


def test_a_replan_after_one_failed_tool_does_not_escalate(monkeypatch):
    _openrouter(monkeypatch)
    plan = [{"id": 1, "label": "read the page", "tool": "fetch_page", "parallel_group": None},
            {"id": 2, "label": "answer", "tool": None, "parallel_group": None}]
    efforts = _drive(monkeypatch, [[("fetch_page", {"url": "https://blocked.example"})]],
                     ok=lambda n, a: False, plan=plan, user_text="read it")
    assert "max" not in efforts


# --- real evidence still does ------------------------------------------------------------------

def test_a_deliberate_replan_still_escalates_to_max(monkeypatch):
    _openrouter(monkeypatch)
    plan = [{"id": 1, "label": "email", "tool": "gmail_action", "parallel_group": None},
            {"id": 2, "label": "answer", "tool": None, "parallel_group": None}]
    efforts = _drive(monkeypatch, [[("request_replan", {"reason": "the plan is wrong"})]],
                     plan=plan, user_text="email him")
    assert efforts[1] == "max"


def test_repeating_an_identical_call_escalates_to_max(monkeypatch):
    _openrouter(monkeypatch)
    same = ("web_browse", {"task": "the same query"})
    efforts = _drive(monkeypatch, [[same] for _ in range(6)])
    assert "max" in efforts
    assert efforts.index("max") == 5        # after the iteration floor, as before


def test_a_tool_that_keeps_failing_escalates_to_max(monkeypatch):
    _openrouter(monkeypatch)
    efforts = _drive(monkeypatch, [[_search()] for _ in range(6)], ok=lambda n, a: False)
    assert "max" in efforts


def test_standard_turns_still_get_promoted_to_thinking_on_varied_repetition(monkeypatch):
    # The other half of the ladder is unchanged: a misrouted turn gets reasoning.
    _openrouter(monkeypatch)
    bodies = []

    async def stream(client, model, messages, send, sq, temperature=None, extra_body=None,
                     tools=None, *, purpose="chat", iteration=None, plan_active=False,
                     max_tokens=None):
        bodies.append(extra_body)
        if len(bodies) <= 6:
            return "", "", [{"id": str(len(bodies)), "name": "web_browse",
                             "input": {"task": f"q{len(bodies)}"}}]
        return "done", "", []

    async def exec_tool(tc, send):
        return tc, "result", True

    monkeypatch.setattr(engine, "_run_stream_openai", stream)
    monkeypatch.setattr(engine, "_exec_tool", exec_tool)
    run(engine._chat_compat(OPENROUTER.standard, "system", [{"role": "user", "content": "hi"}],
                            _send, None, user_text="", channel="local", use_thinking=False))
    efforts = _efforts(bodies)
    assert efforts[:5] == ["none"] * 5 and efforts[5] == "high"


# --- the evidence predicate itself ------------------------------------------------------------

def _c(name, args, ok, step):
    return escalation.Call(name, escalation.args_key(args), ok, step)


def test_evidence_requires_the_iteration_floor_except_for_a_requested_replan():
    trail = [_c("web_browse", {"q": 1}, True, 0), _c("web_browse", {"q": 1}, True, 1)]
    assert escalation.max_effort_evidence(1, trail, False) is None
    assert escalation.max_effort_evidence(4, trail, False) == "repeated call"
    assert escalation.max_effort_evidence(0, [], True) == "replan requested"


def test_an_identical_call_within_one_step_is_not_going_in_circles():
    trail = [_c("web_browse", {"q": 1}, True, 4), _c("web_browse", {"q": 1}, True, 4)]
    assert escalation.max_effort_evidence(4, trail, False) is None


def test_failures_must_span_steps_and_one_tool():
    one_step = [_c("fetch_page", {"u": i}, False, 4) for i in range(3)]
    assert escalation.max_effort_evidence(4, one_step, False) is None
    mixed = [_c("fetch_page", {"u": 1}, False, 3), _c("web_browse", {"q": 2}, False, 4),
             _c("fetch_page", {"u": 3}, False, 4)]
    assert escalation.max_effort_evidence(4, mixed, False) is None
    spread = [_c("fetch_page", {"u": 1}, False, 3), _c("fetch_page", {"u": 2}, False, 4),
              _c("fetch_page", {"u": 3}, False, 4)]
    assert escalation.max_effort_evidence(4, spread, False) == "keeps failing"
    one_ok = spread[:2] + [_c("fetch_page", {"u": 3}, True, 4)]
    assert escalation.max_effort_evidence(4, one_ok, False) is None


def test_args_key_ignores_key_order():
    assert escalation.args_key({"a": 1, "b": 2}) == escalation.args_key({"b": 2, "a": 1})
