"""Mid-turn steering and pending turns -- Step 1 of the upgrade plan (2026-09-08), made
durable and conversation-aware by G3 (2026-10-06, docs/archive/GOALS-ROADMAP.md M3).

Once a turn starts the pipeline lock is held for the whole cycle. A message that arrives
meanwhile is written to the conversation inbox (`backend/storage/inbox.py`) BEFORE it is
acknowledged, and then goes one of two ways:

* **Steering**: it belongs to the conversation whose turn is running, so it carries that
  run's id and is drained at the tool loop's one safe point (after a tool-call iteration's
  results, before the next model call -- DeepSeek rejects anything else) and folded into
  the running turn via `format_interjection`.
* **A pending turn**: anything else -- a Discord message during a desk turn, a server
  channel during a DM turn, a correction to a turn that had no tool calls to drain at --
  becomes its own turn once JARVIS is idle, with its own reply (main._admit_pending).

Until G3 this was a deque per *channel* with maxlen=3, in memory: a fourth message was
silently evicted (D3), a restart after the "got it" lost the message, a server channel and
the DM shared one queue (N5), and a Discord message sent during a desk turn was told "I'll
factor that in" and then waited for some later Discord turn (D11).

The clarifying question (1b) waits on the same inbox: `latest_seq` is recorded before the
question goes out, and `await_reply` takes only messages newer than it (A07).

The in-memory asyncio.Event per conversation only wakes a waiter faster; the inbox row is
the truth. This module owns no Discord or audio I/O and never imports `main.py`.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import contextlib
import contextvars

from backend.storage import inbox as _inbox

# The conversation and run of the turn executing in this task's context. Set by main for
# each turn; engine.py's drain reads them, so a drain can only take messages aimed at the
# turn that is actually running. Unset (tests, legacy callers): the channel name is the
# conversation and messages aimed at no run are taken.
current_conversation: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "steering_conversation", default=None)
current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "steering_run", default=None)
# The inbox key of the message this turn answers (a Discord message id, or a local key):
# what makes a goal steer or answer from this message idempotent.
current_message_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "steering_message_key", default=None)

_events: dict[str, asyncio.Event] = {}

# The owner's DM with the bot: where a clarifying question is asked and answered.
DM = "discord:dm"


def _event(conversation: str) -> asyncio.Event:
    return _events.setdefault(conversation, asyncio.Event())


def _reset_memory() -> None:
    """Forget the in-memory wake signals (a process restart, or a test). The inbox stays."""
    _events.clear()


def _store_insert(**kw) -> int:
    return _inbox.insert(**kw)


@contextlib.contextmanager
def turn(conversation: str, run_id: str | None, message_key: str | None = None):
    """Mark the code inside as running turn `run_id` of `conversation`."""
    tokens = (current_conversation.set(conversation), current_run.set(run_id),
              current_message_key.set(message_key))
    try:
        yield
    finally:
        current_message_key.reset(tokens[2])
        current_run.reset(tokens[1])
        current_conversation.reset(tokens[0])


def _conversation(channel: str) -> str:
    return current_conversation.get() or channel


def push(channel: str, text: str, *, key: str | None = None, conversation: str | None = None,
         target_run: str | None = None, context: dict | None = None,
         reply_to: str | None = None) -> int:
    """Store a message durably and wake any waiter. Returns its sequence number (> 0), or 0
    when the text was blank or could not be stored -- and then it must not be acknowledged.
    A duplicate key returns the original's sequence: one message, one row."""
    text = (text or "").strip()
    if not text:
        return 0
    conv = conversation or channel
    seq = _store_insert(conversation_id=conv, text=text, message_key=key,
                        target_run_id=target_run, context=context, reply_to=reply_to)
    if seq:
        _event(conv).set()
    return seq


def drain(channel: str) -> list[str]:
    """Claim and return, oldest first, the messages aimed at the turn running in this
    context (its conversation and run). Always safe to call speculatively."""
    rows = _inbox.claim_for_run(_conversation(channel), current_run.get())
    ev = _events.get(_conversation(channel))
    if ev is not None and not _inbox.pending(_conversation(channel)):
        ev.clear()
    return [r["text"] for r in rows]


def pending(channel: str) -> bool:
    return bool(_inbox.pending(_conversation(channel)))


def pending_turns() -> list[dict]:
    """Messages waiting to become their own turn (oldest first, every conversation)."""
    return _inbox.pending()


def claim_next() -> dict | None:
    return _inbox.claim_next()


def finish(message_key: str, ok: bool) -> None:
    _inbox.finish(message_key, _inbox.PROCESSED if ok else _inbox.FAILED)


def finish_run(run_id: str | None, completed: bool) -> int:
    """The messages a run absorbed take its outcome (interrupted if it did not complete)."""
    if not run_id:
        return 0
    return _inbox.finish_run(run_id, _inbox.PROCESSED if completed else _inbox.INTERRUPTED)


def recover() -> list[dict]:
    return _inbox.recover()


def _all_rows() -> list[dict]:
    return _inbox.all_rows()


def format_interjection(messages: list[str]) -> str:
    """Wrap drained steering messages into a clearly-labeled injected user turn, so the
    model reads this as a live correction arriving mid-work -- not the turn's original
    request repeated back to it."""
    if len(messages) == 1:
        body = messages[0]
    else:
        body = "\n".join(f"- {m}" for m in messages)
    return (
        _owner.say("[«owner» just sent this while you were mid-task -- read it now and "
        "adjust course if it changes anything]\n") + body
    )


def join_as_followup(messages: list[str]) -> str:
    """Several messages as one piece of plain user text (a clarifying question's answer)."""
    return "\n".join(messages)


def latest_seq(conversation: str) -> int:
    """The newest message so far in `conversation` (named explicitly: a clarifying question
    asks in the DM whichever conversation the running turn is in). A caller about to ask
    records this first, so anything already waiting cannot be mistaken for the reply."""
    return _inbox.latest_seq(conversation)


async def await_reply(channel: str, timeout: float, after_seq: int = 0) -> str | None:
    """Wait up to `timeout` seconds for a message NEWER than `after_seq` in `channel`'s
    conversation, then take it (joined) -- or None on timeout.

    A07: older messages stay where they are -- they are steering for the work already in
    flight, not an answer to a question asked after they arrived. The clarifying question
    asks in the DM, so callers pass the DM conversation explicitly.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    conv = channel
    # A fresh wake signal for this wait: an Event belongs to the loop that first waits on it,
    # and the inbox row (not the signal) is what is read. push() sets whichever is current.
    ev = _events[conv] = asyncio.Event()
    while True:
        taken = _take_newer(conv, after_seq)
        if taken:
            return join_as_followup(taken)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            await asyncio.wait_for(ev.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        ev.clear()


def _take_newer(conversation: str, after_seq: int) -> list[str]:
    """Claim the messages newer than `after_seq` in `conversation`, whatever run they were
    aimed at: a reply to a question is the question's, not the running turn's steering."""
    return [r["text"] for r in _inbox.claim_newer(conversation, after_seq)]


def clear(channel: str, conversations: list[str] | None = None) -> int:
    """/new: messages still waiting in the abandoned conversation are cancelled (not
    silently: the count is returned so the caller can say so) and must not steer the fresh
    one. Returns how many."""
    convs = conversations or [channel]
    for c in convs:
        _events.pop(c, None)
    return _inbox.cancel_pending(convs)
