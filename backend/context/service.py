"""A03: the context service — collect sources, budget them, render the prompt.

**Rendering delegates to `personality.build_system_prompt`, on purpose.** The obvious
alternative is to reimplement prompt assembly here, and that would be a mistake: JARVIS's
voice lives in the ordering, the framing sentences and the trigger logic of that function,
and a parallel implementation would be a second competing one — exactly what the roadmap
says a migration must *not* leave behind ("A feature is migrated when it consumes the
shared contract and its old competing implementation is no longer active"). So the
service owns **which sources exist, what they cost, and what survives the budget**;
`build_system_prompt` stays the single author of how they read.

The practical consequence is testable: when the budget drops nothing, the rendered prompt
is **byte-for-byte identical** to what `chat()` produces today. When the budget does drop
something, the difference is exactly that source's absence — and the snapshot's manifest
says which and why.

Wired into the live path since slice 4: `chat()` assembles through
`build_foreground`/`render_foreground`, and `_plan_task` through
`build_planning`/`render_planning_context`.
"""
from __future__ import annotations

import logging

from backend.context import budget, profiles, sources
from backend.context.sources import Snapshot

log = logging.getLogger(__name__)

# source_id -> the build_system_prompt keyword that carries it.
#
# VAULT_SUMMARY is deliberately ABSENT: the `[Your vault]` listing is built inside
# build_system_prompt via vault.vault_summary(), not passed in, so it has no kwarg. An
# earlier draft mapped it to `vault_passage`, which is a different source entirely (the
# best semantic vault excerpt from recall()) -- that would have silently rendered the
# folder listing where the retrieved passage belongs. This mapping is the
# contract between the inventory and the renderer; the drift guard in test_context.py
# checks the other direction (that every content parameter has a profile home).
_FOREGROUND_KWARGS: dict[str, str] = {
    profiles.OWNER: "owner_md",
    profiles.QUEUE: "intent",
    profiles.TOPIC: "relevant_topic",
    profiles.LETTER: "vault_letter",
    profiles.FACTS: "facts",
    profiles.SLEEP: "sleep_info",
    profiles.EXCHANGE: "exchange",
    profiles.PREDICTIONS: "pending_predictions",
    profiles.HISTORY_BRIDGE: "history_bridge",
    profiles.VAULT_PASSAGE: "vault_passage",
    profiles.PORTFOLIO: "portfolio_context",
}

# Precedence class per source, so callers pass content and the service classifies it.
_FOREGROUND_CLASSES: dict[str, int] = {
    profiles.OWNER: sources.MEMORY,
    profiles.QUEUE: sources.MEMORY,
    profiles.TOPIC: sources.MEMORY,
    profiles.LETTER: sources.MEMORY,
    profiles.FACTS: sources.MEMORY,
    profiles.SLEEP: sources.MEMORY,
    profiles.PREDICTIONS: sources.MEMORY,
    profiles.VAULT_PASSAGE: sources.MEMORY,
    profiles.EXCHANGE: sources.CONTINUITY,
    profiles.HISTORY_BRIDGE: sources.CONTINUITY,
    profiles.PORTFOLIO: sources.CONTINUITY,   # authoritative ledger precedes fuzzy memories
}


def build_foreground(
    user_text: str,
    *,
    context_window: int = budget.DEFAULT_CONTEXT_WINDOW,
    reservation: budget.Reservation = budget.DEFAULT_RESERVATION,
    **content: str,
) -> Snapshot:
    """Collect and budget the sources for one foreground turn.

    `content` is keyed by source id (see `profiles`), e.g.
    `build_foreground(text, **{profiles.OWNER: portrait})`. Unknown keys raise rather
    than being silently ignored — a typo that quietly drops the user portrait is exactly
    the failure this package exists to prevent.
    """
    unknown = set(content) - set(_FOREGROUND_CLASSES)
    if unknown:
        raise KeyError(f"unknown context source id(s): {sorted(unknown)}")

    candidates = [
        # The user's request is non-droppable: a turn without it is not a turn. The
        # invariant core is supplied by the renderer itself and is not budgeted here --
        # it is unconditional, and pretending it could be dropped would be a lie.
        sources.fragment(profiles.USER_REQUEST, sources.INSTRUCTION, user_text,
                         droppable=False),
    ]
    for source_id, text in content.items():
        candidates.append(
            sources.fragment(source_id, _FOREGROUND_CLASSES[source_id], text))

    allowed = profiles.FOREGROUND.filter([c for c in candidates if c is not None])
    return budget.build_snapshot("foreground", allowed,
                                 context_window=context_window,
                                 reservation=reservation)


def render_foreground(snapshot: Snapshot, user_text: str, **passthrough) -> tuple[str, str]:
    """Render a foreground snapshot into the `(stable, volatile)` prompt pair.

    Sources the budget dropped are simply absent — `build_system_prompt` already treats
    every content block as optional, so an omitted source renders exactly as it would
    have if that content had never existed. No block needs a special "omitted" branch.

    `passthrough` forwards arguments that are control rather than content
    (`intent_result`), which the budget has no opinion about.
    """
    from backend.brain import personality

    kwargs: dict[str, str] = {}
    for fragment in snapshot.fragments:
        key = _FOREGROUND_KWARGS.get(fragment.source_id)
        if key:
            kwargs[key] = fragment.text
    return personality.build_system_prompt(user_text, **kwargs, **passthrough)


def shadow_diff(user_text: str, *, content: dict[str, str], **passthrough) -> dict:
    """Compare service-rendered output against `build_system_prompt` called directly.

    The migration check the roadmap asks for: an offline shadow build that proves the new
    path reproduces the old one before anything is switched over. Returns a report rather
    than asserting, so it can be run against real traffic and logged.
    """
    from backend.brain import personality

    snapshot = build_foreground(user_text, **content)
    new_stable, new_volatile = render_foreground(snapshot, user_text, **passthrough)

    direct_kwargs = {
        _FOREGROUND_KWARGS[sid]: text
        for sid, text in content.items() if sid in _FOREGROUND_KWARGS
    }
    old_stable, old_volatile = personality.build_system_prompt(
        user_text, **direct_kwargs, **passthrough)

    return {
        "identical": new_stable == old_stable and new_volatile == old_volatile,
        "stable_match": new_stable == old_stable,
        "volatile_match": new_volatile == old_volatile,
        "omissions": [(o.source_id, o.reason) for o in snapshot.omissions],
        "token_estimate": snapshot.token_estimate,
        "old_chars": len(old_stable) + len(old_volatile),
        "new_chars": len(new_stable) + len(new_volatile),
    }


# Planning gets a deliberately small allowance: it produces a 2-5 step JSON list, not
# prose, so context beyond the goal and what is already known is pure latency and cost
# on a pre-pass that runs before the real turn.
#
# Sized against the actual call, not copied from the foreground defaults. A first draft
# reused `DEFAULT_RESERVATION.answer` (4096) and 1500 for tools, giving a reservation of
# 6096 against a 6000 window -- zero budget, so every source was dropped as
# "budget-exhausted" and the planner silently got no context at all: precisely the bug
# this slice exists to fix. Its own test caught it.
#   tools    the planner is given tool NAMES only, ~22 of them  -> ~100 tokens
#   messages single-shot, no conversation history               -> 0
#   answer   engine._PLAN_TASK_MAX_TOKENS                       -> 4000
PLANNING_WINDOW = 8000
PLANNING_RESERVATION = budget.Reservation(
    tools=200,
    messages=0,
    answer=4000,
    margin=300,
)


def build_planning(user_text: str, *, queue: str = "", relevant_topic: str = "",
                   plan: str = "") -> Snapshot:
    """Sources for a planning call, under the PLANNING profile."""
    candidates = [
        sources.fragment(profiles.USER_REQUEST, sources.INSTRUCTION, user_text,
                         droppable=False),
        sources.fragment(profiles.PLAN, sources.LIVE_STATE, plan),
        sources.fragment(profiles.QUEUE, sources.MEMORY, queue),
        sources.fragment(profiles.TOPIC, sources.MEMORY, relevant_topic),
    ]
    allowed = profiles.PLANNING.filter([c for c in candidates if c is not None])
    return budget.build_snapshot("planning", allowed,
                                 context_window=PLANNING_WINDOW,
                                 reservation=PLANNING_RESERVATION)


_PLANNING_LABELS = {
    profiles.QUEUE: "Open intents (work already in progress)",
    profiles.TOPIC: "What is already known about this subject",
    profiles.PLAN: "Current plan",
}


def render_planning_context(snapshot: Snapshot) -> str:
    """A compact context block for the planner prompt. Empty when there is nothing.

    The roadmap's rule -- "never plan from bare task text when context already exists".
    Today `_plan_task` sees only the request string and a list of tool names, so it
    cannot know a note on the subject already exists or that related work is already
    open, and plans a redundant first step accordingly.
    """
    blocks = []
    for fragment in snapshot.fragments:
        label = _PLANNING_LABELS.get(fragment.source_id)
        if label and fragment.text.strip():
            blocks.append(f"{label}:\n{fragment.text.strip()}")
    if not blocks:
        return ""
    return "\n\n".join(blocks)


def build_task_brief(
    *,
    objective: str,
    origin_request: str = "",
    constraints: str = "",
    evidence: str = "",
    completion: str = "",
) -> str:
    """Compose the brief a background job inherits instead of a bare task string.

    The roadmap's headline motivation for A03: *"A background task knows the goal,
    constraints, and evidence behind the request that created it."* The originating
    turn now supplies this brief so nuanced work keeps its relevant context.

    Deliberately a plain formatted string rather than a structured object: the consumer
    is a model prompt, and `background._BG_SYSTEM` expects text. Foreground tool calls
    supply the originating request and relevant evidence before the worker starts.
    """
    parts = [f"OBJECTIVE\n{objective.strip()}"]
    if origin_request.strip():
        parts.append(f"ORIGINATING REQUEST\n{origin_request.strip()}")
    if constraints.strip():
        parts.append(f"CONSTRAINTS ACCEPTED\n{constraints.strip()}")
    if evidence.strip():
        parts.append(f"EVIDENCE ALREADY GATHERED\n{evidence.strip()}")
    if completion.strip():
        parts.append(f"DONE WHEN\n{completion.strip()}")
    return "\n\n".join(parts)
