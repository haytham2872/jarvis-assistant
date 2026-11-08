"""The phone as a conversation channel (docs/archive/MOBILE-APP-PLAN.md, M1).

A message from the phone takes the same road as a Discord message: stored in the
conversation inbox before it is acknowledged, then either absorbed by the running phone
turn at its next safe point or answered as its own turn by main.py's admission loop, on
channel "mobile" (its own history and sessions, JARVIS's shared memory).

What the phone needs on top lives in storage/mobile_requests.py: its request id (a retry
finds the original instead of running twice) and the final answer. Live events go through
events.HUB. main.py is never imported here: it wires itself in through `configure`.
"""
from __future__ import annotations

import ast
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.brain import steering
from backend.mobile import live_speech
from backend.mobile.events import HUB
from backend.storage import exchanges, inbox, mobile_requests, operations

log = logging.getLogger(__name__)

CHANNEL = CONVERSATION = "mobile"
KEY_PREFIX = "mobile:"
MAX_TEXT = 8_000
INPUT_KINDS = ("text", "voice")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# What chat() sends that the phone shows live; everything else stays on the desk.
FORWARD = {"thinking", "response", "tool_call", "turn_meta", "plan", "step_start", "step_done",
           "plan_update", "error", "background_started", "background_done"}


# The words of a tool call worth showing on the phone, in this order. Never the full input:
# a vault_write carries a whole note, an email its body.
_DETAIL_KEYS = ("action", "query", "question", "task", "ticker", "url", "filename", "subject", "when")
MAX_DETAIL = 100
MAX_TOOLS = 25


def tool_detail(args: Any) -> str:
    """A short line saying what a tool call was about ("search · NVDA close today")."""
    if isinstance(args, str):
        try:
            args = ast.literal_eval(args)      # a receipt's args_preview is the dict's repr
        except (ValueError, SyntaxError):
            return args.strip("{} ")[:MAX_DETAIL]
    if not isinstance(args, dict):
        return ""
    parts = [str(args[k]).strip() for k in _DETAIL_KEYS if args.get(k) not in (None, "")]
    return " · ".join(p for p in parts if p)[:MAX_DETAIL]


class Rejected(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


_admit: Callable[[], None] = lambda: None
_running: Callable[[], dict] = lambda: {}
_new_chat: Callable[[], Awaitable[str] | str] | None = None
_stop: Callable[[], bool] = lambda: False


def configure(*, admit: Callable[[], None], running: Callable[[], dict],
              new_chat: Callable[[], Awaitable[str] | str], stop: Callable[[], bool] = lambda: False) -> None:
    global _admit, _running, _new_chat, _stop
    _admit, _running, _new_chat, _stop = admit, running, new_chat, stop
    _listen()


def stop() -> bool:
    """The Stop button: stops the answer being written now. True when one was."""
    return bool(_stop())


def valid_request_id(request_id: str) -> bool:
    return isinstance(request_id, str) and bool(_REQUEST_ID.match(request_id))


def message_key(request_id: str) -> str:
    return KEY_PREFIX + request_id


def request_id_of(key: str | None) -> str | None:
    return key[len(KEY_PREFIX):] if key and key.startswith(KEY_PREFIX) else None


# ------------------------------------------------------------------ accepting a message

def submit(request_id: str, text: str, input_kind: str = "text", spoken: bool = False,
           reply_to: int | None = None) -> dict:
    """Store the message, then wake the admission loop. Raises Rejected.

    `reply_to` (slice 4): the Inbox message he is answering; the turn is shown it."""
    text = (text or "").strip()
    if not valid_request_id(request_id):
        raise Rejected(400, "request_id must be 8-64 letters, digits, '-' or '_'")
    if not text:
        raise Rejected(400, "the message is empty")
    if len(text) > MAX_TEXT:
        raise Rejected(413, f"the message is over {MAX_TEXT} characters")
    if input_kind not in INPUT_KINDS:
        raise Rejected(400, f"input must be one of {INPUT_KINDS}")

    seen = mobile_requests.record(request_id, text, input_kind)
    if seen == mobile_requests.CONFLICT:
        raise Rejected(409, "this request id was already used for a different message")
    if seen == mobile_requests.UNSTORED:
        raise Rejected(503, "the message could not be stored; send it again")
    if seen == mobile_requests.SAME and inbox.get(message_key(request_id)):
        return status(request_id) or {}           # a retry: the original's outcome, nothing reruns

    running = _running() or {}
    target = running.get("run_id") if running.get("conversation") == CONVERSATION else None
    seq = steering.push(CHANNEL, text, key=message_key(request_id), conversation=CONVERSATION,
                        target_run=target, context={"request_id": request_id, "input": input_kind,
                                 "spoken": bool(spoken),
                                 **({"reply_to": int(reply_to)} if reply_to else {})})
    if not seq:
        mobile_requests.forget(request_id)        # not acknowledged: a retry starts afresh
        raise Rejected(503, "the message could not be stored; send it again")
    HUB.publish({"type": "accepted", "request_id": request_id, "steers_run": target})
    _admit()
    return status(request_id) or {}


_STATES = {inbox.PENDING: "queued", inbox.CLAIMED: "running", inbox.PROCESSED: "answered",
           inbox.FAILED: "failed", inbox.INTERRUPTED: "interrupted", inbox.CANCELLED: "cancelled"}


def status(request_id: str) -> dict | None:
    """Where a request is now, from the durable records (never from memory alone)."""
    req = mobile_requests.get(request_id)
    if req is None:
        return None
    row = inbox.get(message_key(request_id))
    steered = bool(row and row.get("target_run_id"))
    if req.get("outcome"):
        state = req["outcome"]
    elif row is None:
        state = "unknown"
    elif steered and row["state"] in (inbox.CLAIMED, inbox.PROCESSED):
        state = "absorbed"     # joined a running turn; that turn's answer covers it
    else:
        state = _STATES.get(row["state"], row["state"])
    return {"request_id": request_id, "state": state, "input": req["input_kind"],
            "text": row["text"] if row else None,
            "run_id": req.get("run_id") or (row.get("target_run_id") if steered else None),
            "tools": _tools(req.get("run_id")) if not steered else [],
            "answer": req.get("answer"), "received_at": req["received_at"],
            "finished_at": req.get("finished_at"), "steered": steered}


# ------------------------------------------------------------------ a phone turn (main.py)

def sink(base_send: Callable[[dict], Awaitable[Any]], run_id: str | None,
         request_id: str | None) -> Callable[[dict], Awaitable[None]]:
    """chat()'s `send` for a phone turn: the desk UI as before, plus the phone's stream."""
    async def send(msg: dict) -> None:
        await base_send(msg)
        if msg.get("type") == "tool_call":
            live = live_speech.get(request_id)
            if live:
                live.on_event(msg)           # a heard turn: one spoken line while the tool runs
            msg ={"type": "tool_call", "tool": msg.get("tool"), "detail": tool_detail(msg.get("input"))}
        if msg.get("type") in FORWARD:
            HUB.publish({**msg, "run_id": run_id, "request_id": request_id})
    return send


def started(run_id: str | None, request_id: str | None) -> None:
    if request_id and run_id:
        mobile_requests.bind_run(request_id, run_id)
    HUB.publish({"type": "run_started", "run_id": run_id, "request_id": request_id})


def finished(run_id: str | None, request_id: str | None, completed: bool,
             answer: str | None, error: str | None = None) -> None:
    """The final answer is stored before it is announced: a phone that missed the event
    finds it in the snapshot."""
    outcome = mobile_requests.ANSWERED if completed and answer else (
        mobile_requests.FAILED if error or completed else mobile_requests.INTERRUPTED)
    if request_id:
        mobile_requests.finish(request_id, outcome, answer if completed else None)
    HUB.publish({"type": "run_finished", "run_id": run_id, "request_id": request_id,
                 "outcome": outcome, "answer": answer if completed else None, "error": error,
                 "mood": mood()})


def mood() -> dict | None:
    """JARVIS's mood as the orb tints by it (slice 6): the tone and how strong it is, nothing
    else. None when the mood engine has no state; a failure never breaks a reply."""
    try:
        from backend.brain import mood as mood_engine
        m = mood_engine.get_mood()
        if m is None or not m.tone:
            return None
        return {"tone": str(m.tone), "intensity": round(max(0.0, min(1.0, float(m.intensity))), 3)}
    except Exception:
        log.debug("mobile: mood unavailable", exc_info=True)
        return None


# ------------------------------------------------------------------ reading back

def snapshot(limit: int = 50) -> dict:
    """The phone conversation JARVIS is in now, open requests, and the event position to
    resume from. "Now" is what his live history holds after a restart (history_refill: by
    time, never past the last New chat), so a new chat really starts empty and an ended one
    moves to the chat list."""
    from backend.brain import history_refill
    rows = history_refill.restore(CHANNEL, limit)
    running = _running() or {}
    return {
        "schema_version": 1, "epoch": HUB.epoch, "seq": HUB.seq,
        "exchanges": [_exchange(r) for r in rows],
        "requests": [s for s in (status(r["request_id"]) for r in mobile_requests.recent(30)) if s],
        "running": ({"run_id": running.get("run_id")} if running.get("conversation") == CONVERSATION
                    else None),
        # M4: what waits on him and what he started in the background (both survive a
        # closed app: they are read from their tables, not from events he may have missed).
        "approvals": approvals_view()["pending"],
        "jobs": phone_jobs(),
        "mood": mood(),
    }


def chats(limit: int = 30) -> list[dict]:
    return exchanges.sessions_for_channel(CHANNEL, limit)


def chat_history(session_id: str, limit: int = 500) -> list[dict]:
    return [_exchange(r) for r in exchanges.exchanges_for_session(session_id, limit)
            if r.get("channel") == CHANNEL]


# ------------------------------------------------------------------ M4: approvals and jobs

JOBS_SHOWN_S = 3 * 24 * 3600.0      # phone-started jobs listed for three days


def public_approval(row: dict) -> dict:
    """What the phone shows of an approval request (never the raw arguments: the preview is
    what he is asked to approve, as on Discord)."""
    return {"id": row.get("id"), "state": row.get("state"), "preview": row.get("preview", ""),
            "why": row.get("why", ""), "context": row.get("context", ""),
            "created_at": row.get("created_at"), "expires_at": row.get("expires_at"),
            "decided_at": row.get("decided_at"), "decided_how": row.get("decided_how"),
            "outcome": (row.get("outcome") or "")[:400]}


def approvals_view(now: float | None = None) -> dict:
    """Requests waiting on him, and the ones settled in the last day."""
    from backend.storage import approvals
    now = time.time() if now is None else now
    recent = [r for r in approvals.recent(20)
              if r.get("state") != approvals.PENDING and (r.get("decided_at") or r.get("created_at") or 0) > now - 86400]
    return {"pending": [public_approval(r) for r in approvals.pending(now=now)],
            "recent": [public_approval(r) for r in recent[:10]]}


_JOB_STATES = {"queued": "queued", "running": "running", "done": "done", "failed": "failed",
               "cancelled": "cancelled", "interrupted": "failed"}


def phone_jobs(now: float | None = None) -> list[dict]:
    """Background work started from the phone in the last three days, newest first, from the
    durable job table: a job that finished while the app was closed is here on reopen."""
    from backend.storage import jobs
    now = time.time() if now is None else now
    out = []
    for row in jobs.recent(now - JOBS_SHOWN_S):
        p = row["payload"]
        if p.get("origin") != CHANNEL:
            continue
        status = _JOB_STATES.get(row["state"], row["state"])
        summary = p.get("summary", "") or ""
        if row["state"] == "interrupted" and not summary:
            summary = "Interrupted by a restart; it was not retried."
        out.append({"id": p.get("display_id"), "status": status, "kind": p.get("kind", "agent"),
                    "description": (p.get("description") or "")[:300],
                    "started": p.get("started") or row["created_at"], "finished": p.get("finished"),
                    "summary": summary[:600], "result": (p.get("result") or "") if status == "done" else "",
                    "note_file": p.get("note_file", ""), "origin": CHANNEL,
                    "conclusion": (p.get("conclusion") or "") if status == "done" else ""})
    return out


def _on_approval(row: dict) -> None:
    HUB.publish({"type": "approval", "approval": public_approval(row)})


def _on_job(job: dict) -> None:
    if job.get("origin") == CHANNEL:
        HUB.publish({"type": "job", "job": job})


def _listen() -> None:
    """Approval requests (any origin: he decides them wherever he is) and phone-started jobs
    reach the phone's event stream."""
    from backend.brain import background
    from backend.tools import approval_flow
    approval_flow.add_listener(_on_approval)
    background.add_job_listener(_on_job)


async def decide(approval_id: int, approve: bool) -> dict:
    """Approve or deny from the phone, through the same functions as a Discord tap: the
    claim is one conditional UPDATE, so a double tap (or a tap racing Discord) runs the
    action at most once, and the Discord message is updated with the result."""
    from backend.storage import approvals
    from backend.tools import approval_flow
    text = await (approval_flow.approve(approval_id, "phone") if approve
                  else approval_flow.deny(approval_id, "phone"))
    row = approvals.get(approval_id)
    return {"text": text, "approval": public_approval(row) if row else None}


def spoken_answer(source: str) -> str:
    """The final answer `source` names, read from the server's records: 'req:<request id>'
    (an answered phone request), 'ex:<exchange id>' (a phone exchange in history) or
    'notice:<id>' (a message in the Inbox). The
    phone never supplies the text it hears. Raises Rejected."""
    kind, _, ident = (source or "").partition(":")
    if kind == "req" and valid_request_id(ident):
        row = mobile_requests.get(ident)
        if row is None:
            raise Rejected(404, "no such request")
        if not row.get("answer"):                 # stored only once the turn is answered
            raise Rejected(409, "this request has no final answer yet")
        return row["answer"]
    if kind == "ex" and ident and len(ident) <= 64:
        row = exchanges.get_exchange(ident)
        if row is None or row.get("channel") != CHANNEL or not row.get("assistant_text"):
            raise Rejected(404, "no such exchange")
        return row["assistant_text"]
    if kind == "notice" and ident.isdigit() and len(ident) <= 12:
        # Slice 5: a message JARVIS sent on his own (the brief, read aloud), from the Inbox's store.
        from backend.storage import notices
        row = notices.get(int(ident))
        if row is None or not row.get("text"):
            raise Rejected(404, "no such message")
        return row["text"]
    raise Rejected(400, "source must be req:<request id>, ex:<exchange id> or notice:<message id>")


async def new_chat() -> str:
    if _new_chat is None:
        raise Rejected(503, "new chat is not available yet")
    result = _new_chat()
    if hasattr(result, "__await__"):
        result = await result      # type: ignore[misc]
    HUB.publish({"type": "chat_started"})
    return str(result)


def _tools(run_id: str | None) -> list[dict]:
    """The tools a run used, from its receipts (durable: they survive a reopened app)."""
    if not run_id:
        return []
    try:
        ops = operations.operations_for_run(run_id)[:MAX_TOOLS]
    except Exception:
        return []
    return [{"tool": o["tool"], "outcome": o["outcome"], "detail": tool_detail(o.get("args_preview") or "")}
            for o in ops]


def _exchange(r: dict) -> dict:
    return {"id": r["id"], "session_id": r["session_id"], "at": r["timestamp"],
            "user": r["user_text"], "assistant": r["assistant_text"]}
