"""A03: the originating turn's context, available to work spawned from it.

The roadmap's headline motivation for this package: *"A background task knows the goal,
constraints, and evidence behind the request that created it."* Today
`run_in_background(task)` receives one sentence the model wrote, plus a minimal
autonomous-work system prompt — so a job spawned out of a nuanced conversation has no
idea why it exists, and the roadmap records the consequence: background work "behaving
like unrelated mini-agents".

**Inherited, not restated.** The alternative is to widen the tool schema and make the
model re-type the goal, constraints and evidence it already had in context. That is worse
on every axis: it costs tokens, it invites the model to paraphrase (and so distort) the
user's actual words, it changes the registry contract the A00 parity fixture locks, and
it can simply be skipped. The runtime already holds all of it, so the runtime attaches it.

A `ContextVar`, for the same reason `operations.current_run_id` is one: the value is
established in `chat()` and read much later, inside a tool, with other work running
concurrently — a module global would be shared across overlapping turns, while a
ContextVar is inherited correctly by each task spawned from the turn that set it.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

_MAX_FIELD_CHARS = 1500      # a brief, not a transcript


@dataclass(frozen=True)
class Origin:
    """What the turn that spawned a job knew."""
    user_text: str = ""
    queue: str = ""
    relevant_topic: str = ""
    channel: str = "local"


current_origin: ContextVar[Origin | None] = ContextVar("current_origin", default=None)

# A06: the plan the turn is currently authorized to execute (A04 slice 5 established
# that a plan IS the turn's authorized scope, not a suggestion). A job spawned mid-plan
# inherits it as a real constraint. None on a Claude-mode turn, a simple turn, or a
# chronos night task -- all of which genuinely have no plan, so the brief simply omits
# the section rather than inventing one.
current_plan: ContextVar[list | None] = ContextVar("current_plan", default=None)


def set_plan(plan: list | None) -> None:
    """Record the plan now in force. Called when one is chosen and after each replan,
    so a job spawned later inherits the CURRENT scope rather than a superseded one."""
    current_plan.set(list(plan) if plan else None)


def _plan_constraints() -> str:
    """The active plan rendered as the constraint a spawned job must respect."""
    plan = current_plan.get()
    if not plan:
        return ""
    steps = []
    for step in plan:
        label = str(step.get("label", "")).strip()
        if label:
            steps.append(f"  {step.get('id', '?')}. {label}")
    if not steps:
        return ""
    return ("This job was started while the conversation was executing the plan below. "
            "Do your own part of it; leave the other steps to the turn that owns "
            "them.\n" + "\n".join(steps))


def set_origin(user_text: str, *, queue: str = "", relevant_topic: str = "",
               channel: str = "local") -> None:
    """Record the originating turn. Called once per turn; never raises.

    A06: also clears any plan left over from the previous turn. Without this, a simple
    turn following a planned one would hand a stale, superseded plan to any job it
    spawned — the plan is per-turn authorized scope, so it must not outlive its turn.
    `_chat_compat` sets the new one if this turn produces a plan at all.
    """
    current_plan.set(None)
    try:
        current_origin.set(Origin(
            user_text=(user_text or "")[:_MAX_FIELD_CHARS],
            queue=(queue or "")[:_MAX_FIELD_CHARS],
            relevant_topic=(relevant_topic or "")[:_MAX_FIELD_CHARS],
            channel=channel,
        ))
    except Exception:
        pass


def clear_origin() -> None:
    current_origin.set(None)


def brief_for(task: str) -> str:
    """Build the task brief a background job should inherit.

    Falls back to the bare task string when there is no origin — a night task from
    chronos genuinely has no originating conversation, and inventing one would be worse
    than omitting it.
    """
    from backend.context import profiles, service

    origin = current_origin.get()
    if origin is None:
        return task

    return service.build_task_brief(
        objective=task,
        origin_request=origin.user_text,
        evidence=_evidence(origin),
        # A06: the constraints A03 slice 3 deliberately left empty. It noted the field
        # "exists for when A04's plan scope supplies real ones" -- A04 slice 5 made the
        # plan the turn's authorized scope, so it is now a genuine constraint rather
        # than something fabricated from the queue. Empty when no plan is in force.
        constraints=_plan_constraints(),
    )


def _evidence(origin: Origin) -> str:
    parts = []
    if origin.relevant_topic.strip():
        parts.append("Relevant vault note:\n" + origin.relevant_topic.strip())
    if origin.queue.strip():
        parts.append("Open intents:\n" + origin.queue.strip())
    return "\n\n".join(parts)
