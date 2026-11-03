"""backend/brain/escalation.py -- Step 3 (2026-09-09): adaptive effort and
mid-turn model escalation for the OpenAI-compatible live-chat loop.

Pure logic only -- no I/O, no network. `_chat_compat` (engine.py) is the sole
caller and owns every actual API call and piece of loop state; this module
only decides WHAT should happen next, at the exact same clean boundary
mid-turn steering already uses (after a tool-call iteration's results are
appended, before the next model call). Kept as its own module for the same
reason steering.py/pacing.py/positions.py are: the trigger predicate has to
be "pure and unit-testable, separate from the loop calling it" -- a guardrail
named explicitly in the upgrade plan, not a style preference.

Three states, one ladder, scoped to a single turn (never persisted across
turns -- the next turn starts fresh from whatever route() decides, exactly
as before this feature existed):

  "standard"         -- provider.standard model, thinking off  (Flash)
  "thinking_normal"   -- provider.thinking model, provider.thinking_body (Pro @ normal effort)
  "thinking_max"      -- provider.thinking model, MAX_EFFORT_BODY (Pro @ raised effort)

Escalating moves up the ladder one step; de-escalating moves down one step.
Model escalation (standard -> thinking_normal) is lever #2 in the plan --
"the most common 'harder than it looked' case is a simple turn misrouted to
Flash." Effort escalation (thinking_normal -> thinking_max) is lever #1, for
a turn that was already correctly routed to Pro but is still stuck. Both
collapse to the same one-step-up-the-ladder operation.

**Live-verified against the real OpenRouter API (2026-09-09) before
hardcoding anything** -- the plan explicitly warns not to assume Astra's
low/medium/high/xhigh/max naming applies here. First pass: every
`reasoning.effort` string tried, including nonsense values like "minimal" and
"none", returned `200`/`finish_reason: stop` -- the host tolerates unknown
values rather than rejecting them, so "accepted" alone proves nothing about
whether a value does anything. Second pass, a task that actually needs
reasoning (a multi-step word problem): `reasoning_tokens` scaled
184 (low) -> 245 (medium) -> 297 (high, the provider's own normal default)
-> 388 (xhigh) -> 318 (max) on an identical prompt -- a real, substantial
difference, not noise. "max" was picked over "xhigh" for
`MAX_EFFORT_BODY` because it's the unambiguous "full effort" name and the
single-sample gap between the two is within run-to-run variance; either
would have been a legitimate escalation target.

**Max effort needs real evidence (2026-10-09).** On the server one max-effort call reasoned
21,317 tokens for 79s, 35% of all thinking-tier model time in two weeks. Max was reached on
signals that do not mean "stuck": three DIFFERENT calls to the same tool (ordinary research,
including the parallel batch the planner asks for), or a replan triggered by any failed tool
call -- one blocked page. Thinking harder fixes neither; the turn lacks data, not reasoning.
standard -> thinking_normal keeps the name-based test (a misrouted turn getting reasoning is
cheap and the point of the ladder). thinking_normal -> thinking_max now needs
`max_effort_evidence`: the model asked to replan, a call identical to one made in an earlier
step, or the last three calls to one tool, across steps, all failing.
"""

import json
from dataclasses import dataclass

# One step above the provider's own normal thinking-tier effort. A NEW dict,
# never a mutation of router.OPENROUTER_THINKING_BODY -- the upgrade plan's
# own final checklist calls out "OPENROUTER_THINKING_BODY never mutated in
# place" as a hard requirement. Mutating a shared module-level dict in place
# would leak the escalated effort into every OTHER call site that imports
# the same constant (background.py, research.py's cheap-tier calls don't use
# it, but the live chat path's own NEXT turn would inherit the mutation too),
# silently and permanently, for the rest of the process.
MAX_EFFORT_BODY = {"reasoning": {"effort": "max"}}

_LEVELS = ("standard", "thinking_normal", "thinking_max")

_STUCK_REPEAT = 3     # same tool called this many times in a row -> stuck, not progressing
_MIN_ITERATION = 4    # don't even consider escalating before this many iterations have run --
                       # a turn that finishes in 2-3 tool calls was never "stuck," just short


@dataclass(frozen=True)
class Call:
    """One executed tool call, for the max-effort evidence: what ran, with which
    arguments (`args_key`), whether it worked, and in which loop step."""
    name: str
    args: str
    ok: bool
    step: int


def args_key(args) -> str:
    """Canonical form of a call's arguments, so the same call compares equal."""
    try:
        return json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


def max_effort_evidence(iteration: int, trail: list[Call], deliberate_replan: bool) -> str | None:
    """Why a turn already reasoning at the thinking tier should get max effort, or None.

    - "replan requested": the model itself called request_replan (no iteration floor, as
      before). A replan forced by a failed tool is not evidence: one blocked page is.
    - "repeated call": a call in the latest step is identical (tool and arguments) to one
      made in an EARLIER step. The same call twice within one parallel batch is not.
    - "keeps failing": the last three calls to one tool, spread over at least two steps,
      all failed.
    The last two respect the same iteration floor as the name-based test."""
    if deliberate_replan:
        return "replan requested"
    if iteration < _MIN_ITERATION or not trail:
        return None
    latest = trail[-1].step
    earlier = {(c.name, c.args) for c in trail if c.step < latest}
    if any((c.name, c.args) in earlier for c in trail if c.step == latest):
        return "repeated call"
    tail = trail[-_STUCK_REPEAT:]
    if (len(tail) == _STUCK_REPEAT and len({c.name for c in tail}) == 1
            and len({c.step for c in tail}) >= 2 and not any(c.ok for c in tail)):
        return "keeps failing"
    return None


def _tool_name(call_log_entry: str) -> str:
    return call_log_entry.split("(", 1)[0]


def is_stuck(call_log: list[str]) -> bool:
    """True when the tail of `call_log` shows repetition, not progress: the
    same tool called `_STUCK_REPEAT` times in a row. Pure -- reuses the
    exact ground-truth call_log already built for `_build_progress_note`
    (engine.py), no separate tracking needed.
    """
    if len(call_log) < _STUCK_REPEAT:
        return False
    tail = [_tool_name(c) for c in call_log[-_STUCK_REPEAT:]]
    return len(set(tail)) == 1


def should_escalate(iteration: int, call_log: list[str], replanned: bool) -> bool:
    """The escalation trigger predicate. Fires on either of two signals: a
    replan just fired this iteration (Plan-and-Execute's own "this isn't
    working" signal -- see `_replan` in engine.py), or the loop has run long
    enough that a repeating call_log tail means real repetition rather than
    an early false positive on a turn that was always going to take a few
    tries to get going.
    """
    if replanned:
        return True
    if iteration < _MIN_ITERATION:
        return False
    return is_stuck(call_log)


def should_deescalate(call_log: list[str]) -> bool:
    """True once there's enough history to call the turn genuinely
    recovered -- not merely too short to call "stuck" yet. The naive
    `not is_stuck(call_log)` is wrong on its own: right after escalating,
    `call_log` can be as short as one entry, and `is_stuck` correctly
    returns False for anything under `_STUCK_REPEAT` entries (nothing to
    judge yet) -- which would make the naive version fire on iteration
    zero, de-escalating a turn before it had any chance to prove itself
    either way. Requiring the same minimum length `is_stuck` itself
    requires closes that gap: only once there's a real `_STUCK_REPEAT`-call
    window to look at does "not stuck" mean anything.

    Meaningful only when the caller already knows the turn is above
    "standard" on the ladder (de-escalating a turn that was never escalated
    is a no-op by construction in `step()` below). Otherwise deliberately
    the exact negation of `is_stuck()`, not a stricter separate condition
    with its own hysteresis on top of the length floor: de-escalating
    quickly is the low-risk direction (the plan's own framing -- "cheaper
    and lower-risk than escalation"), so there's no reason to delay
    stepping back down once the loop is making real progress again.
    """
    if len(call_log) < _STUCK_REPEAT:
        return False
    return not is_stuck(call_log)


def escalate(level: str) -> str:
    """One step up the ladder. Already at the top stays there."""
    idx = _LEVELS.index(level)
    return _LEVELS[min(idx + 1, len(_LEVELS) - 1)]


def deescalate(level: str) -> str:
    """One step down the ladder. Already at the bottom stays there."""
    idx = _LEVELS.index(level)
    return _LEVELS[max(idx - 1, 0)]


def step(provider, level: str, iteration: int, call_log: list[str], replanned: bool,
         floor: str = "standard", *, trail: list[Call] | None = None,
         deliberate_replan: bool = False) -> str:
    """One escalation/de-escalation decision for one iteration boundary.
    Returns the (possibly unchanged) next level.

    `floor` is the level the turn started at. De-escalation only undoes an escalation;
    it never goes below where routing put the turn. It used to stop only at "standard",
    so a turn routed to the thinking tier dropped to reasoning-off after its third
    varied tool call and ran the rest of the turn, synthesis included, without thinking.
    Found by the H0 benchmark (2026-09-28): a rebuilt research turn at effort "high" ran
    9 of its 12 calls at effort "none".
    """
    if level == "thinking_normal":
        # The step to max effort: only on real evidence (module docstring, 2026-10-09).
        if max_effort_evidence(iteration, trail or [], deliberate_replan):
            return escalate(level)
    elif level == "standard" and should_escalate(iteration, call_log, replanned):
        return escalate(level)
    if _LEVELS.index(level) > _LEVELS.index(floor) and should_deescalate(call_log):
        return deescalate(level)
    return level


def resolve(provider, level: str) -> tuple[str, bool, dict | None]:
    """(model, use_thinking, extra_body_override) for a given ladder level.

    `extra_body_override` is None except at "thinking_max", where it
    overrides whatever `_reasoning_config(provider, use_thinking)` would
    otherwise return for `extra_body` -- temperature is unaffected either
    way (live-verified: DeepSeek does not pin temperature by effort level).
    The caller still calls `_reasoning_config` for temperature and
    the normal-case extra_body; this only supplies the one override.
    """
    if level == "standard":
        return provider.standard, False, None
    if level == "thinking_max":
        return provider.thinking, True, MAX_EFFORT_BODY
    return provider.thinking, True, None
