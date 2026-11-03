"""A04 slice 3: one policy-driven tool gateway, shared by every executor.

The roadmap's issue: *"Different loops dispatch tools differently, misread errors and can
execute conflicting writes concurrently"* — and its expected benefit: *"background and
foreground behavior converge"*.

They had genuinely diverged. `engine._exec_tool` (foreground) and `background._bg_exec`
(both `run_in_background` tasks and chronos night tasks) were two separate
implementations of the same operation, and every A02/A04 correction so far had landed on
the foreground one only:

| policy | `_exec_tool` | `_bg_exec` (before) |
|---|---|---|
| capability allowlist | n/a | yes |
| argument validation (A04 slice 2) | yes | **no — malformed args executed** |
| typed outcome (A04 slice 1) | yes | **no — returned a bare string** |
| operation receipt (A02) | yes | **no** |
| `unknown` on cancellation (A02) | yes | **no** |

The middle row was a live bug of exactly the kind slice 2 had just fixed: background's
loop carried the identical `except json.JSONDecodeError: tool_input = {}` and then ran
the tool anyway — so an overnight task could act on arguments the model never
successfully expressed, with nobody watching. The bottom rows meant an autonomous task
could not tell a failed action from a successful one at all, and left no evidence either
way.

This module holds the policy once. Callers keep their own *transport* concerns — the
foreground still emits its WebSocket `tool_call` event and keeps per-turn counters, the
background still coerces to a string for its provider loop — because those genuinely
differ. What must not differ is what counts as allowed, valid, failed, or uncertain.

Deliberately NOT a registry of executors or a plugin system: one function, called from
two places, with the differences passed as arguments.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from backend.storage import operations as _ops
from backend.tools import approval_flow, outcomes, policy, validation

log = logging.getLogger(__name__)

# What each read tool's output is, for recognising text later copied out of it (P1).
_UNTRUSTED_SOURCES = {"web_browse": "a web page", "fetch_page": "a web page",
                      "gmail_action": "an email"}
# Trusted lookups: where an address may honestly have come from (never evidence he asked).
_KNOWN_SOURCES = {"memory_search": "my memory", "memory_timeline": "my memory",
                  "vault_read": "my notes", "person_profile": "a profile",
                  "calendar_action": _owner.say("«his» calendar"), "notion_action": _owner.say("«his» notes")}

# P1: a second opinion on an outward action that the owner did ask for. Injected (it is a
# model call, which lives in the brain layer): async fn(tool, args, ctx) -> a short concern,
# or None when the action looks like what he asked for. `ctx` is `policy.review_context()`:
# his words this turn, the conversation just before them, and what was read from outside.
_outward_reviewer: Callable[..., Awaitable[str | None]] | None = None


def set_outward_reviewer(fn) -> None:
    global _outward_reviewer
    _outward_reviewer = fn


# P3: tools that resolve an ambiguity into the exact action before anything is decided or stored
# (a plan `apply` without a plan id is pinned to the proposal being discussed), and tools that can
# describe their target for the approval he reads. Registered by the tool's own module.
_preparers: dict[str, Callable[[dict], dict]] = {}
_describers: dict[str, Callable[[dict], Any]] = {}


def set_preparer(tool: str, fn) -> None:
    _preparers[tool] = fn


def set_describer(tool: str, fn) -> None:
    _describers[tool] = fn


async def _label_for(name: str, args: dict) -> str:
    """What he should read about the target of an action that needs his approval. Never raises."""
    try:
        if name == "calendar_action" and args.get("action") in _EVENT_EDITS:
            from backend.tools import calendar as _calendar
            return await _calendar.describe_event(str(args.get("event_id") or ""))
        fn = _describers.get(name)
        if fn is None:
            return ""
        result = fn(args)
        if inspect.isawaitable(result):
            result = await result
        return str(result or "")
    except Exception as exc:
        log.debug("gateway: could not describe %s (%s)", name, exc)
        return ""


_EVENT_EDITS = ("update_event", "delete_event")


async def _event_risk(name: str, args: dict) -> str | None:
    """Editing or deleting a calendar event is the owner's data unless JARVIS made the event.
    Looks the event up; `None` leaves the table's level (his data) in force, which is also what
    happens when the lookup fails -- an event of unknown ownership is treated as his."""
    if name != "calendar_action" or args.get("action") not in _EVENT_EDITS:
        return None
    from backend.tools import calendar as _calendar
    owner = await _calendar.owner_of(str(args.get("event_id") or ""))
    # `gone` and `none` (no id at all): there is nothing to change, so the tool just reports that.
    return policy.OWN if owner in ("jarvis", "gone", "none") else None


@dataclass(frozen=True)
class ToolOutcome:
    """The typed result of one tool call, replacing a bare string."""
    tool: str
    result: Any
    outcome: str                 # outcomes.OK | FAILED | UNKNOWN | PENDING
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == outcomes.OK

    @property
    def pending(self) -> bool:
        """Not done, and not failed: waiting for the owner's approval (P1)."""
        return self.outcome == outcomes.PENDING


def _record(tool: str, outcome: str, started: float, args=None, result=None,
            error: str | None = None) -> None:
    """Durable evidence. Never raises, never delays the caller's result."""
    try:
        _ops.record_operation(tool=tool, outcome=outcome, started_at=started,
                              args=args, result=result, error=error)
    except Exception:
        pass


def deterministic_concern(name: str, args: dict, ctx: dict) -> str | None:
    """The checks that need no model, and cannot be talked round.

    An email's recipient must be an address JARVIS actually had (`policy.unverified_recipients`),
    and its text must not lift a passage out of an email or page read this turn
    (`policy.copied_from`, what an injected "forward this" produces).
    """
    if name == "gmail_action" and args.get("action") == "send_email":
        guessed = policy.unverified_recipients(args.get("to"), ctx)
        if guessed:
            return (f"I don't see {', '.join(guessed)} in what you told me, in the conversation, or "
                    "in anything I looked up, so I may have guessed or mistyped it.")
    outgoing = " ".join(str(args.get(k) or "") for k in ("subject", "body"))
    source = policy.copied_from(outgoing, ctx.get("seen") or [])
    if source:
        return f"The message repeats a passage from {source} that I read while working on this."
    return None


async def _outward_concern(name: str, args: dict) -> str | None:
    """Anything that makes an outward action he DID ask for worth a second look, or None.

    The deterministic checks first, then the injected reviewer, which compares recipient and
    content with what he asked for. If a reviewer is wired and fails, the action goes to him:
    a check that could not run is not a pass.
    """
    ctx = policy.review_context()
    concern = deterministic_concern(name, args, ctx)
    if concern:
        return concern
    if _outward_reviewer is None:
        return None
    try:
        return await _outward_reviewer(name, args, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("gateway: outward review failed (%s) -- asking the owner instead", exc)
        return "I couldn't run my usual check on this one, so I'd rather you look at it."


async def _ask(name: str, args: dict, *, risk: str, why: str, caller: str, started: float,
               record: bool) -> ToolOutcome:
    """Turn a call that needs the owner into a stored request, and say so plainly."""
    row = await approval_flow.request(context=caller, tool=name, args=args, risk=risk, why=why,
                                      label=await _label_for(name, args))
    if row is None:
        msg = (_owner.say("Not done: this needs «owner's» approval, but I could not record the request, "
               "so nothing was sent or changed. Tell «him» it did not go through; do not try "
               "it again."))
        if record:
            _record(name, outcomes.FAILED, started, args=args, error="approval could not be recorded")
        return ToolOutcome(name, msg, outcomes.FAILED, "approval could not be recorded")
    text = approval_flow.waiting_text(row, caller)
    if record:
        _record(name, outcomes.PENDING, started, args=args, result=text,
                error=f"awaiting approval #{row['id']}")
    return ToolOutcome(name, text, outcomes.PENDING, f"awaiting approval #{row['id']}")


async def execute(
    name: str,
    args: Any,
    *,
    dispatch: dict[str, Callable[..., Awaitable[Any]]],
    capabilities: Iterable[str] | None = None,
    invalid_args: str | None = None,
    denied_message: str | None = None,
    record: bool = True,
    caller: str | None = None,
) -> ToolOutcome:
    """Run one tool call under the shared policy. Never raises except CancelledError.

    Order matters and is the same for every caller:
      1. capability  — a denied tool must not even be looked up
      2. existence   — unknown tool
      3. arguments   — malformed JSON, then schema validation (A04 slice 2)
      4. policy      — does this need the owner? (P1) Reads and his own space run; anything
                       that reaches another person runs only when he asked in this message
                       (a verified quote) and a second look agrees, otherwise it becomes an
                       approval request and is NOT run
      5. dispatch    — cancellation stays `unknown`, never `failed` (A02)
      6. classify    — typed outcome from the result string (A04 slice 1)

    `capabilities=None` means "no restriction" and is NOT the same as an empty set,
    which denies everything. The foreground passes None; background passes its allowlist.

    `caller` is who is asking: "live" (a turn the owner is in), an unattended kind, or
    "approved" (carrying out a request he approved). Left out, it is the strict default --
    whatever the job scope set, else "unattended" -- so a new caller that forgets to say is
    treated as nobody watching.
    """
    started = time.time()

    if capabilities is not None and name not in capabilities:
        msg = denied_message or f"Error: tool '{name}' is not available here."
        if record:
            _record(name, outcomes.FAILED, started, args=args, error="capability denied")
        return ToolOutcome(name, msg, outcomes.FAILED, "capability denied")

    fn = dispatch.get(name)
    if fn is None:
        if record:
            _record(name, outcomes.FAILED, started, args=args,
                    error=f"unknown tool {name}")
        return ToolOutcome(name, f"Unknown tool: {name}", outcomes.FAILED,
                           f"unknown tool {name}")

    if invalid_args is not None:
        reason = "its arguments were not valid JSON"
        if record:
            _record(name, outcomes.FAILED, started, args=invalid_args, error=reason)
        return ToolOutcome(name, validation.rejection_message(name, reason),
                           outcomes.FAILED, reason)

    problem = validation.validate(name, args)
    if problem:
        if record:
            _record(name, outcomes.FAILED, started, args=args, error=problem)
        return ToolOutcome(name, validation.rejection_message(name, problem),
                           outcomes.FAILED, problem)

    # P1. `quote` is evidence for the check (the words of his that ask for this), not an
    # argument of the tool: it is removed here, before dispatch and before anything is stored.
    args = dict(args)
    quote = str(args.pop("quote", "") or "") if name in policy.QUOTED_TOOLS else ""
    prepare = _preparers.get(name)
    if prepare is not None:
        try:
            args = dict(prepare(args))
        except Exception as exc:
            log.debug("gateway: could not prepare %s (%s)", name, exc)
    who = caller or policy.current_caller.get()
    decision = policy.decide(name, args, caller=who, words=policy.owner_words(), quote=quote,
                             risk=await _event_risk(name, args))
    if decision.verdict == policy.HANDOFF:
        msg = _owner.say("Not done: this is always «owner's» to do «himself»; I don't do it for «him».")
        if record:
            _record(name, outcomes.FAILED, started, args=args, error="hand-off")
        return ToolOutcome(name, msg, outcomes.FAILED, "hand-off")
    if decision.verdict == policy.APPROVE:
        return await _ask(name, args, risk=decision.risk, why=decision.reason, caller=who,
                          started=started, record=record)
    if decision.asked and who == policy.LIVE:
        concern = await _outward_concern(name, args)
        if concern:
            return await _ask(name, args, risk=decision.risk, why=concern, caller=who,
                              started=started, record=record)

    started = time.time()          # re-taken: policy checks are not the tool's duration
    # The tool can see who is calling: a request the owner approved runs as "approved", which is
    # how the portfolio knows a split confirmation came from his tap rather than a model.
    caller_token = policy.current_caller.set(who)
    quote_token = policy.call_quote.set(quote)
    try:
        result = await fn(**args)
    except asyncio.CancelledError:
        # A02: whether a remote side effect landed cannot be established from here, so
        # the receipt says `unknown` rather than guessing. The roadmap forbids resolving
        # that by blind retry.
        if record:
            _record(name, outcomes.UNKNOWN, started, args=args,
                    error="cancelled mid-execution")
        raise
    except Exception as e:
        if record:
            _record(name, outcomes.FAILED, started, args=args, error=str(e))
        return ToolOutcome(name, f"Error: {e}", outcomes.FAILED, str(e))
    finally:
        policy.current_caller.reset(caller_token)
        policy.call_quote.reset(quote_token)

    verdict = outcomes.classify(name, result)
    if verdict == outcomes.OK:
        outcomes.note_unrecognised(name, result)
        # Remember what came from outside, so text later lifted out of it into an outgoing
        # message can be recognised (`_outward_concern`).
        source = _UNTRUSTED_SOURCES.get(name)
        if source and isinstance(result, str) and policy.risk_of(name, args) == policy.READ:
            policy.note_untrusted(source, result)
        known = _KNOWN_SOURCES.get(name)
        if known and isinstance(result, str):
            policy.note_known(known, result)
    if record:
        _record(name, verdict, started, args=args, result=result,
                error=None if verdict == outcomes.OK else _ops._preview(result))
    return ToolOutcome(name, result, verdict,
                       None if verdict == outcomes.OK else _ops._preview(result))
