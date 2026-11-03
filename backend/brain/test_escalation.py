"""Tests for backend/brain/escalation.py -- Step 3 (2026-09-09): adaptive
effort and mid-turn model escalation.

Pure-function tests only, per the plan's own guardrail that the trigger
predicate be "pure and unit-testable, separate from the loop calling it."
No real API call, no real engine.py loop -- integration with the actual
_chat_compat loop is covered in test_engine_escalation.py.
"""

import pytest

from backend.brain import escalation
from backend.brain.router import OPENROUTER


# ---------------------------------------------------------------------------
# is_stuck / should_escalate / should_deescalate
# ---------------------------------------------------------------------------

def test_is_stuck_false_when_call_log_too_short():
    assert escalation.is_stuck(["web_browse('x')", "web_browse('x')"]) is False


def test_is_stuck_true_on_three_identical_tools_in_a_row():
    log = ["web_browse('a')", "web_browse('b')", "web_browse('c')"]
    assert escalation.is_stuck(log) is True


def test_is_stuck_ignores_call_arguments_only_tool_name_matters():
    """Repetition is about hammering the same TOOL, not the exact same
    arguments -- reformulating the same web_browse query three times in a
    row is exactly the live incident this predicate exists to catch."""
    log = ["web_browse('query one')", "web_browse('query two')", "web_browse('query three')"]
    assert escalation.is_stuck(log) is True


def test_is_stuck_false_when_tools_actually_vary():
    log = ["web_browse('x')", "code_graph", "memory_search('y')"]
    assert escalation.is_stuck(log) is False


def test_is_stuck_only_looks_at_the_tail():
    """An early repeat that the model then moved on from must not count --
    only the most recent _STUCK_REPEAT calls matter."""
    log = ["web_browse('x')", "web_browse('x')", "web_browse('x')", "code_graph", "memory_search('y')"]
    assert escalation.is_stuck(log) is False


def test_should_escalate_fires_on_replan_regardless_of_iteration_or_log():
    assert escalation.should_escalate(0, [], replanned=True) is True


def test_should_escalate_false_below_the_iteration_floor_even_if_stuck():
    stuck_log = ["web_browse('a')", "web_browse('b')", "web_browse('c')"]
    assert escalation.should_escalate(1, stuck_log, replanned=False) is False


def test_should_escalate_true_past_the_floor_when_stuck():
    stuck_log = ["web_browse('a')", "web_browse('b')", "web_browse('c')"]
    assert escalation.should_escalate(4, stuck_log, replanned=False) is True


def test_should_escalate_false_past_the_floor_when_progressing():
    progressing_log = ["web_browse('a')", "code_graph", "memory_search('b')"]
    assert escalation.should_escalate(10, progressing_log, replanned=False) is False


def test_should_deescalate_is_exactly_not_stuck_once_enough_history_exists():
    stuck_log = ["web_browse('a')", "web_browse('b')", "web_browse('c')"]
    progressing_log = ["web_browse('a')", "code_graph", "memory_search('b')"]
    assert escalation.should_deescalate(stuck_log) is False
    assert escalation.should_deescalate(progressing_log) is True


def test_should_deescalate_false_when_call_log_too_short_to_judge():
    """Regression guard for a real bug caught while writing the integration
    tests: `not is_stuck(call_log)` alone is trivially True for a call_log
    under _STUCK_REPEAT entries (is_stuck itself returns False there, for
    lack of anything to judge) -- so a turn escalated moments ago, with only
    one or two calls logged since, would de-escalate again immediately,
    before it had any real chance to prove itself recovered. Must require
    the same minimum length is_stuck itself does.
    """
    assert escalation.should_deescalate([]) is False
    assert escalation.should_deescalate(["web_browse('a')"]) is False
    assert escalation.should_deescalate(["web_browse('a')", "code_graph"]) is False


# ---------------------------------------------------------------------------
# escalate / deescalate -- pure ladder movement
# ---------------------------------------------------------------------------

def test_escalate_moves_up_one_level():
    assert escalation.escalate("standard") == "thinking_normal"
    assert escalation.escalate("thinking_normal") == "thinking_max"


def test_escalate_clamps_at_the_top():
    assert escalation.escalate("thinking_max") == "thinking_max"


def test_deescalate_moves_down_one_level():
    assert escalation.deescalate("thinking_max") == "thinking_normal"
    assert escalation.deescalate("thinking_normal") == "standard"


def test_deescalate_clamps_at_the_bottom():
    assert escalation.deescalate("standard") == "standard"


# ---------------------------------------------------------------------------
# step()
# ---------------------------------------------------------------------------

_STUCK = ["web_browse('a')", "web_browse('b')", "web_browse('c')"]
_PROGRESSING = ["web_browse('a')", "code_graph", "memory_search('b')"]


def test_step_openrouter_escalates_on_replan():
    assert escalation.step(OPENROUTER, "standard", 0, [], replanned=True) == "thinking_normal"
    # 2026-10-09: a replan forced by a failed tool no longer reaches max effort; only one
    # the model asked for does (test_escalation_evidence.py).
    assert escalation.step(OPENROUTER, "thinking_normal", 0, [], replanned=True) == "thinking_normal"
    assert escalation.step(OPENROUTER, "thinking_normal", 0, [], replanned=True,
                           deliberate_replan=True) == "thinking_max"


def test_step_openrouter_escalates_when_stuck_past_the_floor():
    assert escalation.step(OPENROUTER, "standard", 4, _STUCK, replanned=False) == "thinking_normal"


def test_step_openrouter_does_not_escalate_below_the_floor():
    assert escalation.step(OPENROUTER, "standard", 1, _STUCK, replanned=False) == "standard"


def test_step_openrouter_deescalates_once_no_longer_stuck():
    assert escalation.step(OPENROUTER, "thinking_max", 10, _PROGRESSING, replanned=False) == "thinking_normal"
    assert escalation.step(OPENROUTER, "thinking_normal", 10, _PROGRESSING, replanned=False) == "standard"


def test_step_openrouter_standard_stays_standard_when_progressing():
    """De-escalation from "standard" is a no-op by construction -- there is
    nowhere lower to go, and a turn that was never escalated should never
    be touched by this at all."""
    assert escalation.step(OPENROUTER, "standard", 10, _PROGRESSING, replanned=False) == "standard"


def test_step_moves_at_most_one_level_per_call():
    """Even a turn that is BOTH replanned AND deep into a stuck streak only
    moves one rung -- the loop calls step() once per iteration, so multiple
    consecutive stuck iterations climb the ladder one rung at a time, never
    jumping straight to the top in one decision."""
    assert escalation.step(OPENROUTER, "standard", 4, _STUCK, replanned=True) == "thinking_normal"


# ---------------------------------------------------------------------------
# resolve() -- (model, use_thinking, extra_body_override) per level
# ---------------------------------------------------------------------------

def test_resolve_standard():
    model, use_thinking, override = escalation.resolve(OPENROUTER, "standard")
    assert model == OPENROUTER.standard
    assert use_thinking is False
    assert override is None


def test_resolve_thinking_normal():
    model, use_thinking, override = escalation.resolve(OPENROUTER, "thinking_normal")
    assert model == OPENROUTER.thinking
    assert use_thinking is True
    assert override is None   # caller falls through to _reasoning_config's normal thinking_body


def test_resolve_thinking_max_overrides_extra_body():
    model, use_thinking, override = escalation.resolve(OPENROUTER, "thinking_max")
    assert model == OPENROUTER.thinking
    assert use_thinking is True
    assert override == escalation.MAX_EFFORT_BODY


# ---------------------------------------------------------------------------
# The two hard requirements from the upgrade plan's final checklist:
# "OPENROUTER_THINKING_BODY never mutated in place" and "no cheap-tier /
# standard-tier no-thinking body weakened anywhere."
# ---------------------------------------------------------------------------

def test_max_effort_body_is_a_distinct_object_from_the_normal_thinking_body():
    assert escalation.MAX_EFFORT_BODY is not OPENROUTER.thinking_body
    assert escalation.MAX_EFFORT_BODY != OPENROUTER.thinking_body


def test_repeated_escalation_never_mutates_router_module_constants():
    before_thinking = dict(OPENROUTER.thinking_body)
    before_no_thinking = dict(OPENROUTER.no_thinking_body)

    level = "standard"
    for _ in range(6):
        level = escalation.step(OPENROUTER, level, 10, _STUCK, replanned=False)
        escalation.resolve(OPENROUTER, level)

    assert OPENROUTER.thinking_body == before_thinking
    assert OPENROUTER.no_thinking_body == before_no_thinking


def test_max_effort_body_itself_is_never_mutated_by_resolve():
    before = dict(escalation.MAX_EFFORT_BODY)
    escalation.resolve(OPENROUTER, "thinking_max")
    escalation.resolve(OPENROUTER, "thinking_max")
    assert escalation.MAX_EFFORT_BODY == before
