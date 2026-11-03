"""P1: the life of an approval request -- asking, deciding, carrying it out.

`policy.py` decides *whether* a call needs the owner; `storage/approvals.py` records the request.
This module is what happens around it:

  * `request()`   the gateway hands over an action that may not run unattended or unasked. It
                  is stored, then put in front of him (Discord DM with Approve / Deny buttons,
                  injected here as a notifier so this layer never imports the bridge).
  * `approve()`   a tap, or a text reply "yes 12". The request is claimed (one conditional
                  UPDATE, so it can only be claimed once), then the **stored** tool and
                  arguments are run through the gateway with their own run and receipt, and
                  the outcome is recorded. Nothing is regenerated: the action that runs is the
                  one he was shown.
  * `deny()`      "no 12", or the Deny button.
  * `sweep()`     unanswered requests expire after 24 hours and never run late.
  * `startup()`   a request found `executing` at boot was cut off mid-run. Whether an email
                  went out cannot be known, so it is marked `uncertain` and he is told.
                  It is never retried on a guess (the A02 rule for uncertain writes).

Failure behaviour: a request that cannot be recorded is refused (the action does not run); a
request that is recorded but cannot be delivered to him stays pending and is mentioned on his
next message; a notifier that raises never breaks the tool call that asked.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import logging
import re
from typing import Awaitable, Callable

from backend.storage import approvals, operations, runs
from backend.tools import outcomes, policy

log = logging.getLogger(__name__)

Notifier = Callable[[dict], Awaitable["str | None"]]
ResolutionNotifier = Callable[[dict, str], Awaitable["bool | None"]]

_notifier: Notifier | None = None
_resolution_notifier: ResolutionNotifier | None = None
_undelivered: list[int] = []            # requests he has not been told about yet
_notes: list[str] = []                  # news (expired, cut off) he could not be told on Discord
_decided: list[str] = []                # what became of requests, for JARVIS's own next turn
_DECIDED_KEEP = 10
_listeners: list[Callable[[dict], None]] = []   # other clients (the phone) that show requests


def set_notifier(fn: Notifier | None) -> None:
    """async fn(row) -> message id | None: put a request in front of him."""
    global _notifier
    _notifier = fn


def add_listener(fn: Callable[[dict], None]) -> None:
    """fn(row) on every change a client may show: a new request, a claim, a decision, an
    expiry. Called synchronously; a listener that raises is logged and ignored. The phone app
    (backend/mobile) uses it, so a request decided on Discord updates there and the reverse."""
    if fn not in _listeners:
        _listeners.append(fn)


def _changed(row: dict | None) -> None:
    if not row:
        return
    for fn in list(_listeners):
        try:
            fn(row)
        except Exception as exc:
            log.debug("approvals: listener failed for #%s: %s", row.get("id"), exc)


def set_resolution_notifier(fn: ResolutionNotifier | None) -> None:
    """async fn(row, text) -> bool: tell him (and update the request's message) what happened."""
    global _resolution_notifier
    _resolution_notifier = fn


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

async def request(*, context: str, tool: str, args: dict, risk: str, why: str,
                  label: str = "") -> dict | None:
    """Store an action that needs his say-so and put it in front of him.

    Returns the stored row, or None when it could not be recorded -- in which case the
    caller must not run the action: something that needs approval and cannot be tracked
    is not something to do on a guess.
    """
    approval_id = approvals.create(
        context=context, tool=tool, action=str(args.get("action") or ""), arguments=args,
        preview=policy.preview(tool, args, label), risk=risk, why=why,
        run_id=operations.current_run_id.get())
    if approval_id is None:
        return None
    row = approvals.get(approval_id)
    delivered = False
    if _notifier is not None:
        try:
            message_id = await _notifier(row)
            if message_id:
                approvals.attach_message(approval_id, message_id)
                delivered = True
        except Exception as exc:
            log.warning("approvals: could not deliver request #%s: %s", approval_id, exc)
    if not delivered:
        _undelivered.append(approval_id)
    stored = approvals.get(approval_id) or row
    _changed(stored)
    return stored


def waiting_text(row: dict, context: str) -> str:
    """What the tool returns to the model in place of running the action. It says, in words
    a model will not misread, that nothing was done and what to tell him."""
    n = row["id"]
    reach = (_owner.say("I've asked «him» on Discord and in the phone app: «he» can tap Approve, or reply "
             "\"yes %d\" or \"no %d\".")
             % (n, n)) if n not in _undelivered else \
        _owner.say("I could not reach «him» on Discord just now; it will be put to «him» when «he» next writes.")
    if context == policy.LIVE:
        return (_owner.say(f"Not done: this needs «owner's» approval (request #{n}), so nothing was sent or "
                f"changed. {reach} Tell «him» it is waiting for «his» approval. Do not say it was "
                "done, and do not try it again."))
    return (_owner.say(f"Not done: this needs «owner's» approval (request #{n}), so nothing was sent or "
            f"changed. {reach} Carry on with the rest of your task, and say in your final "
            "answer that this is waiting for «his» approval."))


def request_text(row: dict) -> str:
    """The message he reads to decide."""
    return (f"Approval needed (#{row['id']})\n{row['preview']}\n\n"
            f"Why I'm asking: {row['why']}\n"
            f"Tap a button, or reply \"yes {row['id']}\" / \"no {row['id']}\". "
            "It expires in 24 hours and will not run after that.")


def pop_undelivered() -> list[dict]:
    """Requests he has not seen (Discord was unreachable): mentioned on his next message."""
    rows = []
    while _undelivered:
        row = approvals.get(_undelivered.pop(0))
        if row and row["state"] == approvals.PENDING:
            rows.append(row)
    return rows


def pop_notices() -> str:
    """The next-turn block, delivered once: requests he could not be told about on Discord,
    news (a request that expired, or was cut off mid-run) that never reached him, and -- for
    JARVIS's own awareness, so "did that email go out?" has an answer -- what became of the
    requests he settled. Empty when there is nothing."""
    parts = []
    waiting = pop_undelivered()
    if waiting:
        lines = "\n".join(f"- #{r['id']}: {r['preview'].splitlines()[0]}  ({r['why']})" for r in waiting)
        parts.append(
            _owner.say("[Approval requests waiting on «owner»] These could not be put to «him» on Discord. "
            "Tell «him» briefly that they are waiting; «he» can answer here with \"yes N\" or "
            "\"no N\" (N is the number), and nothing has been done yet:\n") + lines)
    unseen = list(_notes)
    _notes.clear()
    if unseen:
        parts.append(_owner.say("[Approval news «he» has not seen]\n") + "\n".join(f"- {n}" for n in unseen))
    settled = [d for d in _decided[-_DECIDED_KEEP:] if d not in unseen]
    _decided.clear()
    if settled:
        parts.append(_owner.say("[Approval requests settled since you last spoke -- «he» saw each result on "
                     "Discord, so use these as facts if «he» asks, and do not announce them]\n")
                     + "\n".join(f"- {d}" for d in settled))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

async def _tell(row: dict, text: str, *, remember: bool = False) -> None:
    """Show him what became of a request. With `remember`, news that never reached him is
    kept for his next message (an expiry he did not see is worth knowing; the result of a
    button he just tapped is not)."""
    shown = False
    _decided.append(text)
    _changed(row)
    if _resolution_notifier is not None:
        try:
            shown = bool(await _resolution_notifier(row, text))
        except Exception as exc:
            log.debug("approvals: resolution notice for #%s failed: %s", row.get("id"), exc)
    if remember and not shown:
        _notes.append(text)


async def approve(approval_id: int, how: str) -> str:
    """Carry out request `approval_id` exactly as stored. Returns the words to say back."""
    row = approvals.claim(approval_id, how)
    if row is None:
        return approvals.reason_not_pending(approval_id)
    _changed(row)                                         # executing: shown wherever it waits

    from backend.brain.engine import TOOL_DISPATCH       # lazy: the gateway's dispatch table
    from backend.tools import gateway

    run_id = runs.begin_run("approval", f"approval #{approval_id}: {row['preview']}"[:200])
    token = operations.current_run_id.set(run_id)
    try:
        result = await gateway.execute(row["tool"], row["arguments"], dispatch=TOOL_DISPATCH,
                                       caller=policy.APPROVED)
    except asyncio.CancelledError:
        approvals.finish(approval_id, approvals.UNCERTAIN, "cancelled while running")
        runs.finish_run(run_id, runs.INTERRUPTED)
        raise
    except Exception as exc:                              # execute() does not raise; belt and braces
        approvals.finish(approval_id, approvals.FAILED, str(exc))
        runs.finish_run(run_id, runs.FAILED, error=str(exc)[:200])
        text = f"Approved #{approval_id}, but it failed: {exc}"
        await _tell(approvals.get(approval_id) or row, text)
        return text
    finally:
        operations.current_run_id.reset(token)

    ok = result.outcome == outcomes.OK
    approvals.finish(approval_id, approvals.EXECUTED if ok else approvals.FAILED,
                     str(result.result))
    runs.finish_run(run_id, runs.COMPLETED if ok else runs.FAILED,
                    error=None if ok else str(result.result)[:200])
    text = (f"Approved #{approval_id}. {result.result}" if ok
            else f"Approved #{approval_id}, but it did not go through: {result.result}")
    await _tell(approvals.get(approval_id) or row, text)
    return text


async def deny(approval_id: int, how: str) -> str:
    row = approvals.deny(approval_id, how)
    if row is None:
        return approvals.reason_not_pending(approval_id)
    text = f"Denied #{approval_id}. Nothing was done."
    await _tell(row, text)
    return text


_REPLY_RE = re.compile(r"^\s*(yes|approve|approved|no|deny|denied)\s+#?(\d{1,9})\s*[.!]?\s*$",
                       re.IGNORECASE)


async def withdraw(approval_id: int, how: str) -> bool:
    """G2: a goal that no longer needs this action withdraws it: pending -> denied (the claim is
    unchanged), and his message says so. False when it was no longer pending -- a tap that
    landed first is executing or done, and the goal records it as such."""
    row = approvals.deny(approval_id, how)
    if row is None:
        return False
    await _tell(row, f"Withdrawn #{approval_id}: {how}. Nothing was done.")
    return True


def deny_orphaned_goal_requests(is_live) -> list[int]:
    """G2, at startup: a goal approval still pending whose goal effect is gone (a crash between
    recording the effect and the approval, or a goal ended meanwhile) is denied. `is_live`
    (approval id -> bool) is the goal runner's check."""
    denied = []
    for row in approvals.pending():
        if row.get("context") == "goal" and not is_live(row["id"]):
            if approvals.deny(row["id"], "withdrawn: no live goal behind it"):
                denied.append(row["id"])
    return denied


def parse_reply(text: str) -> tuple[bool, int] | None:
    """"yes 12" -> (True, 12); "no 12" -> (False, 12); anything else -> None.

    Only this exact shape, and only with an id: a bare "yes" is far more likely to be the
    answer to some other question than to an approval request.
    """
    m = _REPLY_RE.match(text or "")
    if not m:
        return None
    return m.group(1).lower() in ("yes", "approve", "approved"), int(m.group(2))


async def handle_reply(text: str) -> str | None:
    """A text reply to a request. Returns the answer to send back, or None when `text` is not
    a reply to any request (so it is treated as an ordinary message)."""
    parsed = parse_reply(text)
    if parsed is None:
        return None
    yes, approval_id = parsed
    if approvals.get(approval_id) is None:
        return None                                       # not one of ours: an ordinary message
    return await (approve(approval_id, "text") if yes else deny(approval_id, "text"))


# ---------------------------------------------------------------------------
# Time and restarts
# ---------------------------------------------------------------------------

async def sweep() -> int:
    """Expire what has waited 24 hours. Returns how many."""
    expired = approvals.expire_due()
    for row in expired:
        await _tell(row, f"Request #{row['id']} expired after 24 hours and did not run.",
                    remember=True)
    return len(expired)


def reconcile() -> list[dict]:
    """Boot-time: a request found `executing` was cut off mid-run. Whether it went through
    cannot be known, so it becomes `uncertain` and is never retried. Pure storage, so it runs
    before Discord is up; `announce_interrupted` tells him once it is."""
    return approvals.reconcile_interrupted()


async def announce_interrupted(rows: list[dict]) -> int:
    for row in rows:
        await _tell(row, (f"Request #{row['id']} was interrupted while it was running, so I don't "
                          "know whether it went through. I will not run it again; please check "
                          f"({row['preview'].splitlines()[0]})."), remember=True)
    return len(rows)


async def startup() -> int:
    return await announce_interrupted(reconcile())


async def run_loop() -> None:
    from backend.brain import pacing
    await pacing.run_periodic(name="approval expiry", check_interval_s=300.0, on_cycle=sweep)
