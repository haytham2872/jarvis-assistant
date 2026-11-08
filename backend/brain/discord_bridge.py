"""Discord bridge -- talk to JARVIS from your phone, the same way you'd DM any
contact. One bot account, one whitelisted user; you type or send a voice
message, he replies in text.

Design:
  - Gateway connection (a persistent socket), not polling -- discord.py's
    Client.start() runs for as long as the bot is connected, so it's launched
    as its own background task; start() itself just waits for the connection
    to come up (or fail) before returning, same "confirm before declaring
    success" pattern used elsewhere (mem0's warmup search, SearXNG health
    check).
  - Auth: DISCORD_ALLOWED_USER_ID whitelist. Anything from another user id is
    ignored and logged, never replied to. JARVIS holds Gmail send and the vault
    -- an unauthenticated bot would let anyone impersonate the owner to his own
    assistant. If DISCORD_ALLOWED_USER_ID isn't set yet, the bridge still
    connects: every sender is rejected, and the first one to DM "/claim <code>"
    with the one-time code from the startup log becomes the owner
    (discord_claim.py).
  - Voice messages are downloaded and transcribed with the SAME local
    faster-whisper engine already used for desk/browser audio (injected from
    main.py -- see set_transcribe_handler) -- no first-party transcription
    dependency, same accuracy as talking at the desk.
  - "/new" forces the *next* message to start a fresh conversation on this
    channel, bypassing the normal 2h gap check -- see
    engine.force_new_session for why a plain "mint a new session id" isn't
    enough on its own.
  - Full tool parity with the desk channel -- F1 (2026-09-08) removed the
    screen/mouse-control tools that used to be withheld here (see
    engine.tools_for_channel); there is nothing left to restrict.
  - Same silent-degradation contract as the rest of the brain: if
    DISCORD_BOT_TOKEN isn't set, start() logs once and returns -- the feature
    just doesn't exist for this run, nothing else is affected. If the
    `discord.py` package itself somehow isn't installed, the same graceful
    no-op applies (the import happens lazily inside start(), never at module
    load time, so an unconfigured or unavailable bridge can never break basic
    backend startup).

Setup note (Discord-specific): a bot can only DM you once you share a server
with it. The simplest path is a small private server with just you and the
bot -- invite it there via an OAuth URL generated in the Developer Portal
(Send Messages / Read Message History / View Channels), then either type in
a channel there or click the bot's profile and choose Message to open a real
DM. Also: the Developer Portal has a "Message Content Intent" toggle under
the bot's settings that MUST be turned on, or every incoming message.content
arrives empty regardless of what this code requests.

Dependency injection, mirroring background.py's set_broadcaster pattern: this
module is imported by main.py, but never imports it back. main.py wires the
actual pipeline entry point in at startup.
"""
from backend import owner as _owner

import dataclasses
import os
import logging
import tempfile
import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from backend.storage import delivery as _delivery
from backend.storage import discord_messages as _discord_messages
from backend.brain import commands as _commands
from backend.brain import discord_claim as _claim
from backend.brain import discord_context as _discord_context
from backend.tasks import spawn

log = logging.getLogger(__name__)

_BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")
_ALLOWED_ID = os.getenv("DISCORD_ALLOWED_USER_ID", "")

_MAX_MESSAGE_CHARS = 1900   # Discord's real cap is 2000; leave headroom
# How long start() waits for the first connection before returning (startup goes on;
# the connection keeps trying in the background). Not a give-up point: on 6 November
# 2026 a 15 s give-up closed a connection that was 3 s from ready, and nothing retried.
_READY_TIMEOUT      = 60.0
_RETRY_DELAYS_S     = (30.0, 60.0, 120.0, 300.0, 600.0)  # after a failed or lost connection

# Single source of truth for "/commands" (the list itself lives in backend/brain/commands.py) -- and for CLAUDE.md's "Discord Commands
# — quick reference" section, which should read the same as this list. Live
# incident (2026-09-06): JARVIS had no way to answer "what commands do you have?"
# in normal conversation, since each command's description lived under whichever
# module happened to implement it and was never collected in one place his
# self-model could actually reach. Update BOTH this list and that CLAUDE.md
# section together when adding a command -- they're meant to say the same thing,
# but nothing enforces that automatically, so treat a drift between them as a bug.
_COMMAND_HELP: list[tuple[str, str]] = _commands.COMMAND_HELP


def _commands_text() -> str:
    lines = ["Here's what you can send me directly:"]
    lines += [f"**{cmd}** — {desc}" for cmd, desc in _COMMAND_HELP]
    return "\n".join(lines)


def _claim_ingress(message) -> bool:
    """True when this Discord message is new and should be processed.

    Never raises -- a storage problem must not stop Jarvis answering.
    """
    try:
        from backend.storage import exchanges
        return exchanges.claim_ingress(str(message.id), "discord")
    except Exception as exc:
        log.debug("Discord: ingress dedup unavailable (%s) -- admitting", exc)
        return True


TurnHandler        = Callable[[str], Awaitable[str]]
NewSessionHandler   = Callable[[], str]
TranscribeHandler   = Callable[[str], str]   # sync -- called via run_in_executor

_turn_handler:        Optional[TurnHandler]      = None
_new_session_handler: Optional[NewSessionHandler] = None
_stop_handler: Optional[Callable[[], bool]] = None
# Said while an answer is being written, these stop it ("/stop" always replies).
STOP_WORDS = {"/stop", "stop", "stop now", "stop it", "stop everything", "stop everything now"}
_transcribe_handler:  Optional[TranscribeHandler] = None

_client = None        # discord.Client, set only once start() confirms a live connection
_client_task: Optional[asyncio.Task] = None
_supervisor: Optional[asyncio.Task] = None
_stopping = False
# What /status and the phone report: off (not configured), connecting, connected, down.
_link: dict = {"state": "off", "error": "", "retry_at": 0.0}


async def wait_connected(timeout: float = 6 * 3600.0) -> bool:
    """Wait until the bridge is connected (True), or not configured / stopped / timed out
    (False). For the one-time startup notices: a connection made 70 s after a restart must
    still carry them, now that a slow connection is left to finish rather than dropped."""
    end = time.monotonic() + timeout
    while _client is None:
        if _link["state"] == "off" or time.monotonic() >= end:
            return False
        await asyncio.sleep(1.0)
    return True


def link_status() -> str:
    """One line for [Runtime status]: '' when connected or not configured."""
    state = _link["state"]
    if state in ("off", "connected"):
        return ""
    if state == "connecting":
        return "Discord: connecting"
    wait = max(0, int(_link["retry_at"] - time.time()))
    why = f" ({_link['error']})" if _link["error"] else ""
    return f"Discord: DOWN{why}, retrying in {wait // 60} min {wait % 60} s"


def set_turn_handler(fn: TurnHandler) -> None:
    """Injected by main.py: runs one full pipeline turn, returns the reply text."""
    global _turn_handler
    _turn_handler = fn


def stop_reply(text: str) -> str | None:
    """The reply to a stop word, or None when the message is not one (or, for a plain "stop",
    when nothing is running: then it is an ordinary message). Handled before any turn: a turn
    would wait behind the very answer he wants stopped."""
    said = (text or "").strip().lower().rstrip(".!")
    if said not in STOP_WORDS or _stop_handler is None:
        return None
    if _stop_handler():
        return "Stopped."
    return "Nothing is running right now." if said == "/stop" else None


def set_stop_handler(fn: Callable[[], bool]) -> None:
    """Injected by main.py: stops the answer being written now; True when one was."""
    global _stop_handler
    _stop_handler = fn


def set_new_session_handler(fn: NewSessionHandler) -> None:
    """Injected by main.py: handles "/new" -- clears history, forces a fresh session."""
    global _new_session_handler
    _new_session_handler = fn


def set_transcribe_handler(fn: TranscribeHandler) -> None:
    """Injected by main.py: backend/audio/transcriber.py's transcribe_file (Parakeet, English
    and French), the same engine the desk microphone uses."""
    global _transcribe_handler
    _transcribe_handler = fn


PaceHandler = Callable[[str], str]   # sync -- "/pace [arg]" -> reply text
_pace_handler: Optional[PaceHandler] = None


def set_pace_handler(fn: PaceHandler) -> None:
    """Injected by main.py: handles "/pace [level]" (research.handle_pace_command)."""
    global _pace_handler
    _pace_handler = fn


StatusHandler = Callable[[], str]   # sync -- "/status" -> reply text
_status_handler: Optional[StatusHandler] = None


def set_status_handler(fn: StatusHandler) -> None:
    """Injected by main.py: handles "/status" (runtime_status.runtime_status_text)."""
    global _status_handler
    _status_handler = fn


CostHandler = Callable[[], str]   # sync -- "/cost" -> reply text
_cost_handler: Optional[CostHandler] = None


def set_cost_handler(fn: CostHandler) -> None:
    """Injected by main.py: handles "/cost" (usage.session_summary)."""
    global _cost_handler
    _cost_handler = fn


PitchHandler = Callable[[], str]   # sync -- "/about" -> reply text
_pitch_handler: Optional[PitchHandler] = None


def set_pitch_handler(fn: PitchHandler) -> None:
    """Injected by main.py: handles "/about" (pitch.get_pitch_text) -- the
    hand-written showcase message the owner forwards to friends."""
    global _pitch_handler
    _pitch_handler = fn


BriefHandler = Callable[[str], str]              # sync -- "/brief [HH:MM|on|off]" config -> reply text
BriefNowHandler = Callable[[Callable[[str], Awaitable[bool]]], Awaitable[str]]
_brief_handler: Optional[BriefHandler] = None
_brief_now_handler: Optional[BriefNowHandler] = None


def set_brief_handler(fn: BriefHandler) -> None:
    """Injected by main.py: handles "/brief" config (chronos.handle_brief_command)."""
    global _brief_handler
    _brief_handler = fn


def set_brief_now_handler(fn: BriefNowHandler) -> None:
    """Injected by main.py: composes and acknowledges "/brief now" after delivery."""
    global _brief_now_handler
    _brief_now_handler = fn


RulesHandler = Callable[[str], str]   # sync -- "/rules [drop N]" -> reply text
_rules_handler: Optional[RulesHandler] = None


def set_rules_handler(fn: RulesHandler) -> None:
    """Injected by main.py: handles "/rules" (instructions.handle_rules_command)."""
    global _rules_handler
    _rules_handler = fn


RecordHandler = Callable[[], str]   # sync -- "/record" -> reply text
_record_handler: Optional[RecordHandler] = None


def set_record_handler(fn: RecordHandler) -> None:
    """Injected by main.py: handles "/record" (forecasts.record_text)."""
    global _record_handler
    _record_handler = fn


PauseHandler = Callable[[str], str]      # sync -- "/pause [hours] [all] [reason]" -> reply text
ResumeHandler = Callable[[str], str]     # sync -- "/resume" -> reply text (lists what was skipped)
ActivityHandler = Callable[[], str]      # sync -- "/activity" -> the whole view
PlanHandler = Callable[[str], str]       # sync -- "/plan [week|stats|checkin ...|review ...]" -> reply text
_pause_handler: Optional[PauseHandler] = None
_resume_handler: Optional[ResumeHandler] = None
_activity_handler: Optional[ActivityHandler] = None
_plan_handler: Optional[PlanHandler] = None


def set_pause_handler(fn: PauseHandler) -> None:
    """Injected by main.py: handles "/pause" (pause.handle_pause_command)."""
    global _pause_handler
    _pause_handler = fn


def set_resume_handler(fn: ResumeHandler) -> None:
    """Injected by main.py: handles "/resume" (pause.handle_resume_command)."""
    global _resume_handler
    _resume_handler = fn


def set_activity_handler(fn: ActivityHandler) -> None:
    """Injected by main.py: handles "/activity" (activity.activity_text)."""
    global _activity_handler
    _activity_handler = fn


_goals_handler: Optional[Callable[[str], str]] = None


_goal_control_handler = None


def set_goal_control_handler(fn) -> None:
    """Injected by main.py: handles "/goal <id> pause|resume|cancel" (async)."""
    global _goal_control_handler
    _goal_control_handler = fn


def set_goals_handler(fn) -> None:
    """Injected by main.py: handles "/goals [id]" (goals.handle_command). Read-only."""
    global _goals_handler
    _goals_handler = fn


def set_plan_handler(fn: PlanHandler) -> None:
    """Injected by main.py: handles "/plan" (planday.handle_plan_command)."""
    global _plan_handler
    _plan_handler = fn


# P1: approval requests. `decider(approval_id, approve, how)` carries out a tap and returns the
# words to show; `reply_handler(text)` handles a typed "yes 12" and returns None when the text is
# not a reply to any request (so it is an ordinary message). Wired from main.py.
ApprovalDecider = Callable[[int, bool, str], Awaitable[str]]
ApprovalReplyHandler = Callable[[str], Awaitable[Optional[str]]]
_approval_decider: Optional[ApprovalDecider] = None
_approval_reply_handler: Optional[ApprovalReplyHandler] = None


def set_approval_handlers(decider: Optional[ApprovalDecider],
                          reply_handler: Optional[ApprovalReplyHandler]) -> None:
    global _approval_decider, _approval_reply_handler
    _approval_decider = decider
    _approval_reply_handler = reply_handler


_portfolio_handler: Optional[Callable[[str], str]] = None


def set_portfolio_handler(fn: Callable[[str], str]) -> None:
    global _portfolio_handler
    _portfolio_handler = fn


_trace_handler: Optional[Callable[[], str]] = None


def set_trace_handler(fn: Callable[[], str]) -> None:
    """Injected by main.py: handles "/trace" (tool_record.trace_text for this channel)."""
    global _trace_handler
    _trace_handler = fn


SkillsHandler = Callable[[str], str]     # sync -- "/skills" -> reply text
# sync -- "/skill export <name>" -> (filename, text, caption), or an error to show
SkillExportHandler = Callable[[str], "tuple[str, str, str] | str"]
_skills_handler: Optional[SkillsHandler] = None
_skill_export_handler: Optional[SkillExportHandler] = None
_skill_undo_handler: Optional[SkillsHandler] = None


def set_skills_handlers(list_fn: Optional[SkillsHandler],
                        export_fn: Optional[SkillExportHandler],
                        undo_fn: Optional[SkillsHandler] = None) -> None:
    """Injected by main.py: "/skills" (skills.handle_skills_command), "/skill export"
    (skills.export_file) and "/skill undo" (skills.handle_undo_command). "/skill <name>"
    itself is an ordinary turn: chat() reads it."""
    global _skills_handler, _skill_export_handler, _skill_undo_handler
    _skills_handler = list_fn
    _skill_export_handler = export_fn
    _skill_undo_handler = undo_fn


def configure(
    *,
    turn_handler: Optional[TurnHandler] = None,
    new_session_handler: Optional[NewSessionHandler] = None,
    transcribe_handler: Optional[TranscribeHandler] = None,
    pace_handler: Optional[PaceHandler] = None,
    status_handler: Optional[StatusHandler] = None,
    cost_handler: Optional[CostHandler] = None,
    brief_handler: Optional[BriefHandler] = None,
    brief_now_handler: Optional[BriefNowHandler] = None,
    pitch_handler: Optional[PitchHandler] = None,
    rules_handler: Optional[RulesHandler] = None,
    record_handler: Optional[RecordHandler] = None,
    pause_handler: Optional[PauseHandler] = None,
    resume_handler: Optional[ResumeHandler] = None,
    activity_handler: Optional[ActivityHandler] = None,
    plan_handler: Optional[PlanHandler] = None,
    goals_handler: Optional[Callable[[str], str]] = None,
    goal_control_handler=None,
    portfolio_handler: Optional[Callable[[str], str]] = None,
    trace_handler: Optional[Callable[[], str]] = None,
    skills_handler: Optional[SkillsHandler] = None,
    skill_export_handler: Optional[SkillExportHandler] = None,
    skill_undo_handler: Optional[SkillsHandler] = None,
    approval_decider: Optional[ApprovalDecider] = None,
    approval_reply_handler: Optional[ApprovalReplyHandler] = None,
    stop_handler: Optional[Callable[[], bool]] = None,
) -> None:
    """Bulk-wire every Discord DI hook in one call instead of nine.

    Each kwarg maps 1:1 to an existing set_*_handler() above -- this is
    purely a batching convenience over main.py's startup wiring (one of the
    contributors to lifespan() being the code graph's single biggest hub);
    the individual setters, their docstrings, and their own direct callers
    (tests included) are untouched and still work exactly as before. An
    omitted kwarg is left alone, not cleared -- configure(pace_handler=fn)
    does not reset the other eight handlers to None.
    """
    if turn_handler is not None:
        set_turn_handler(turn_handler)
    if new_session_handler is not None:
        set_new_session_handler(new_session_handler)
    if stop_handler is not None:
        set_stop_handler(stop_handler)
    if transcribe_handler is not None:
        set_transcribe_handler(transcribe_handler)
    if pace_handler is not None:
        set_pace_handler(pace_handler)
    if status_handler is not None:
        set_status_handler(status_handler)
    if cost_handler is not None:
        set_cost_handler(cost_handler)
    if brief_handler is not None:
        set_brief_handler(brief_handler)
    if brief_now_handler is not None:
        set_brief_now_handler(brief_now_handler)
    if pitch_handler is not None:
        set_pitch_handler(pitch_handler)
    if rules_handler is not None:
        set_rules_handler(rules_handler)
    if record_handler is not None:
        set_record_handler(record_handler)
    if pause_handler is not None:
        set_pause_handler(pause_handler)
    if resume_handler is not None:
        set_resume_handler(resume_handler)
    if activity_handler is not None:
        set_activity_handler(activity_handler)
    if plan_handler is not None:
        set_plan_handler(plan_handler)
    if goals_handler is not None:
        set_goals_handler(goals_handler)
    if goal_control_handler is not None:
        set_goal_control_handler(goal_control_handler)
    if portfolio_handler is not None:
        set_portfolio_handler(portfolio_handler)
    if trace_handler is not None:
        set_trace_handler(trace_handler)
    if skills_handler is not None or skill_export_handler is not None or skill_undo_handler is not None:
        set_skills_handlers(skills_handler or _skills_handler,
                            skill_export_handler or _skill_export_handler,
                            skill_undo_handler or _skill_undo_handler)
    if approval_decider is not None or approval_reply_handler is not None:
        set_approval_handlers(approval_decider or _approval_decider,
                              approval_reply_handler or _approval_reply_handler)


# ---------------------------------------------------------------------------
# Reply chunking
# ---------------------------------------------------------------------------

def _chunk(text: str) -> list[str]:
    """Split a long reply into Discord-sized chunks, breaking on line boundaries
    where possible so a sentence isn't cut mid-word."""
    if len(text) <= _MAX_MESSAGE_CHARS:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _MAX_MESSAGE_CHARS:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, _MAX_MESSAGE_CHARS)
        if cut <= 0:
            cut = _MAX_MESSAGE_CHARS
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


# Legacy unkeyed notifications use bounded retries. Keyed replies use durable
# per-part receipts below; an ambiguous send there is never retried blindly.
_CHUNK_RETRIES = 2
_CHUNK_RETRY_DELAY = 1.0


async def _send_chunks(sender, text: str) -> int:
    """Send `text` in parts via `sender`. Returns how many parts actually landed.

    Never raises: the count tells the caller whether delivery was complete, partial or
    nothing at all, instead of collapsing all three into one exception.
    """
    parts = _chunk(text)
    for index, part in enumerate(parts):
        for attempt in range(_CHUNK_RETRIES + 1):
            try:
                await sender(part)
                break
            except Exception as e:
                if attempt >= _CHUNK_RETRIES:
                    log.warning(
                        "Discord: giving up on part %d/%d after %d attempts (%s) -- "
                        "%d part(s) already delivered",
                        index + 1, len(parts), attempt + 1, e, index,
                    )
                    return index
                await asyncio.sleep(_CHUNK_RETRY_DELAY)
    return len(parts)


async def _send_durable(sender, text: str, *, intent_id: str, route: str,
                        parts: list[str] | None = None) -> bool:
    """Send only parts that have never been attempted, recording each receipt.

    `parts` defaults to `_chunk(text)`; `send_verbatim` passes exact parts instead."""
    record = _delivery.prepare(intent_id, route, text,
                               parts if parts is not None else _chunk(text))
    if record is None:
        log.warning("Discord: could not persist reply %s; leaving it unsent", intent_id)
        return False
    if record["state"] == "sent":
        return True
    if record["state"] in ("uncertain", "sending"):
        return False
    for part in record["parts"]:
        if part["state"] == "sent":
            continue
        if part["state"] != "pending" or not _delivery.claim_part(intent_id, part["ordinal"]):
            return False
        try:
            message = await sender(part["body"])
        except Exception as exc:
            _delivery.mark_uncertain(intent_id, part["ordinal"], str(exc))
            log.warning("Discord: reply %s part %d uncertain (%s)",
                        intent_id, part["ordinal"] + 1, exc)
            return False
        if not _delivery.mark_sent(intent_id, part["ordinal"], str(getattr(message, "id", ""))):
            _delivery.mark_uncertain(intent_id, part["ordinal"], "sent receipt could not be stored")
            return False
        _record_outgoing(message, text, "reply")
    latest = _delivery.get(intent_id)
    return bool(latest and latest["state"] == "sent")


async def _send_reply(channel, text: str, *, delivery_key: str | None = None) -> bool:
    if delivery_key is not None and getattr(channel, "id", None) is not None:
        return await _send_durable(channel.send, text, intent_id=delivery_key,
                                   route=f"channel:{channel.id}")
    return await _send_chunks(channel.send, text) == len(_chunk(text))


async def _resume_pending() -> None:
    """Continue prepared replies after reconnect; leave ambiguous parts for review."""
    if _client is None:
        return
    for item in _delivery.resumable():
        route = item["channel"]
        if route == "owner":                        # G1a keyed goal messages
            await send_keyed_to_owner(item["body"], item["intent_id"])
            continue
        if not route.startswith("channel:"):
            continue
        try:
            channel_id = int(route.split(":", 1)[1])
            channel = _client.get_channel(channel_id)
            if channel is None:
                channel = await _client.fetch_channel(channel_id)
            await _send_durable(channel.send, item["body"],
                                intent_id=item["intent_id"], route=route)
        except Exception as exc:
            log.warning("Discord: could not resume reply %s (%s)", item["intent_id"], exc)


def conversation_of(channel) -> str:
    """G3 / N5: the conversation a Discord channel is. The DM (no guild) is one; each server
    channel is its own, so a message there never steers a DM turn or vice versa."""
    if channel is None or getattr(channel, "guild", None) is None:
        return "discord:dm"
    return f"discord:{channel.id}"


async def _channel_by_id(channel_id):
    """The channel a waiting message came from: the DM when it is unknown or gone."""
    if _client is None:
        return None
    chan = None
    if channel_id:
        chan = _client.get_channel(int(channel_id))
        if chan is None:
            try:
                chan = await _client.fetch_channel(int(channel_id))
            except Exception:
                chan = None
    if chan is None and _ALLOWED_ID:
        user = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
        chan = user.dm_channel or await user.create_dm()
    return chan


async def answer_admitted(context: dict, text: str, run_turn) -> str | None:
    """G3: answer a message that waited in the inbox, as its own turn, with its own reply.

    The turn runs with that message's context (its id, the message it replied to, its
    channel) and the answer goes to its own durable delivery key. The "got it" sent when it
    arrived used `reply:<id>`; the answer uses `answer:<id>`, so a restart resumes either
    without the two ever being confused. A turn that produced nothing gets a plain line
    saying so instead of silence."""
    message_id = str(context.get("message_id") or "")
    chan = await _channel_by_id(context.get("channel_id"))
    ctx = _discord_context.TurnContext(message_id, context.get("target_id") or "",
                                       voice=bool(context.get("voice")))
    token = _discord_context.current.set(ctx)
    channel_token = current_channel.set(chan)
    try:
        if chan is not None:
            async with _typing(chan):
                response = await run_turn(text)
        else:
            response = await run_turn(text)
    finally:
        _discord_context.current.reset(token)
        current_channel.reset(channel_token)
    quoted = " ".join(text.split())[:120]
    body = response or (f'I couldn\'t answer "{quoted}" -- send it again if you still need it.')
    if chan is not None:
        await _send_reply(chan, body, delivery_key=f"answer:{message_id or hash(text)}")
    return response


async def send_keyed_to_owner(text: str, key: str) -> bool:
    """G1a: a DM to the owner under a durable delivery key (goal messages). The intent is
    recorded before sending and each part's receipt after it, so a retry or a restart
    resumes the same message instead of sending it twice, and an uncertain part is never
    blindly resent. True only when every part is confirmed sent."""
    if _client is None or not _ALLOWED_ID or not text.strip():
        return False
    try:
        user = _client.get_user(int(_ALLOWED_ID))
        if user is None:
            user = await _client.fetch_user(int(_ALLOWED_ID))
        return await _send_durable(user.send, text, intent_id=key, route="owner")
    except Exception as e:
        log.warning("Discord: keyed message %s failed: %s", key, e)
        return False


async def send_to_owner(text: str) -> bool:
    """DM the whitelisted user unprompted (research notifications, future
    heartbeat). True only if the message actually went out.

    Requires the bridge to be connected AND the bot to share a server with
    the owner (Discord's own DM rule -- already satisfied by the setup flow).
    Never raises: an unreachable owner degrades to False, and the caller's
    content survives in the vault regardless.
    """
    return await _send_owner(text, kind="notice")


async def send_research_to_owner(text: str) -> bool:
    """Research DMs can anchor a later 'explain this' without a reply click."""
    return await _send_owner(text, kind="research")


# H4: the Discord channel the current turn came from, so a note the owner asks for lands
# in the same conversation (a DM, or a channel in his private server). None on the desk.
current_channel: ContextVar = ContextVar("discord_current_channel", default=None)

# H4: the notification contract for long turns. Nothing is posted for a turn that ends
# within a minute (most do). After that, one plain line at a time -- what the turn is
# doing and how long it has been going -- at most every two minutes, and never within two
# minutes of the plan notice the turn itself sent. Stops the moment the turn ends.
_PROGRESS_FIRST_S = 60.0
_PROGRESS_EVERY_S = 120.0
ProgressHandler = Callable[[float], tuple[str, Optional[float]]]
_progress_handler: Optional[ProgressHandler] = None


def set_progress_handler(fn: Optional[ProgressHandler]) -> None:
    global _progress_handler
    _progress_handler = fn


# Typing tasks still running (asyncio keeps only weak references to tasks).
_typing_tasks: set = set()
# How long the reply may wait for a typing request still in flight; see _typing.
_TYPING_SETTLE_S = 1.0


@asynccontextmanager
async def _typing(channel):
    """Show "typing..." while the body runs, without waiting for Discord first.

    discord.py's `async with channel.typing()` awaits the typing request before the body
    starts: most of the 0.31s median on the server between a message arriving and its turn
    starting. A failed request also raised out of it and failed the whole turn, and a hung
    one held it. The indicator is entered in its own task instead; its failure is logged and
    ignored. When the body ends, the indicator is stopped before the reply goes out, waiting
    at most _TYPING_SETTLE_S for a request still in flight: a typing request (or discord.py's
    own 5-second re-send) landing after the reply would show "typing..." again for ten
    seconds under an answer that is already there."""
    stop = asyncio.Event()

    async def hold():
        try:
            async with channel.typing():
                await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("Discord: typing indicator unavailable (%s)", e)

    task = asyncio.create_task(hold())
    _typing_tasks.add(task)
    task.add_done_callback(_typing_tasks.discard)
    try:
        yield
    finally:
        stop.set()
        done, _ = await asyncio.wait({task}, timeout=_TYPING_SETTLE_S)
        if not done:
            task.cancel()


async def _watch_progress(channel, message_id, since: float) -> None:
    """Post progress for a turn that is still running. Cancelled when the turn ends."""
    if _progress_handler is None:
        return
    last_sent = None
    last_line = None
    n = 0
    await asyncio.sleep(_PROGRESS_FIRST_S)
    while True:
        try:
            line, announced = _progress_handler(since)
        except Exception as e:
            log.debug("Discord: progress unavailable (%s)", e)
            line, announced = None, None
        recent = [t for t in (last_sent, announced) if t]
        wait = max((t + _PROGRESS_EVERY_S - time.time() for t in recent), default=0.0)
        if wait > 0:
            await asyncio.sleep(wait)
            continue
        if line and line != last_line:
            # Receipted like replies, on a route the reconnect resume skips: a stale
            # "still on it" line after a restart would only confuse.
            await _send_durable(channel.send, line, intent_id=f"progress:{message_id}:{n}",
                                route=f"progress:{getattr(channel, 'id', '')}")
            n, last_line, last_sent = n + 1, line, time.time()
        await asyncio.sleep(_PROGRESS_EVERY_S)


async def _run_turn_with_progress(message, text: str) -> str:
    """Run the turn handler while the watchdog reports on it."""
    watcher = asyncio.create_task(_watch_progress(message.channel, message.id, time.time()))
    try:
        return await _turn_handler(text)
    finally:
        watcher.cancel()


# A note that fits in this many messages is sent inline; a longer one as a file.
_VERBATIM_INLINE_PARTS = 4


def split_exact(text: str, limit: int = _MAX_MESSAGE_CHARS) -> list[str]:
    """Split at line boundaries without losing a character: "".join(parts) == text.

    `_chunk` strips the newline at each cut, which is fine for a reply and wrong for a
    note sent verbatim. A line longer than `limit` is cut at `limit`.
    """
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut + 1 if cut > 0 else limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    # Discord rejects a whitespace-only message; fold one into the part before it.
    merged: list[str] = []
    for part in parts:
        if merged and not part.strip() and len(merged[-1]) + len(part) <= 2000:
            merged[-1] += part
        else:
            merged.append(part)
    return merged


async def send_verbatim(label: str, text: str) -> tuple[bool, str]:
    """Send a vault note's exact text: inline in up to four messages, else as a file.

    To the current turn's channel when there is one, else to the owner's DMs. Every part
    goes through the durable delivery receipts, so a send that may have landed is marked
    uncertain rather than repeated.
    """
    import io
    import uuid
    if _client is None or not _ALLOWED_ID:
        return False, "Discord is not connected"
    target = current_channel.get()
    where = "in this conversation"
    if target is None:
        target = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
        where = "to your Discord DMs"
    route_id = getattr(target, "id", "owner")
    intent_id = f"verbatim:{uuid.uuid4()}"
    parts = split_exact(text)
    if len(parts) <= _VERBATIM_INLINE_PARTS:
        ok = await _send_durable(target.send, text, intent_id=intent_id,
                                 route=f"verbatim:{route_id}", parts=parts)
        return ok, f"{where}, as {len(parts)} message{'s' if len(parts) != 1 else ''}"
    import discord
    name = label.rsplit("/", 1)[-1]

    async def send_file(_part):
        return await target.send(f"{label}, exactly as saved:",
                                 file=discord.File(io.BytesIO(text.encode("utf-8")), filename=name))
    ok = await _send_durable(send_file, text, intent_id=intent_id,
                             route=f"verbatim:{route_id}", parts=[text])
    return ok, f"{where}, as the attached file {name}"


async def send_file(filename: str, text: str, caption: str) -> tuple[bool, str]:
    """Send `text` as an attached file, to this turn's channel or else the owner's DMs.

    Used to export a skill. Goes through the durable delivery receipts like every other
    send, so one that may have landed is marked uncertain rather than repeated."""
    import io
    import uuid
    if _client is None or not _ALLOWED_ID:
        return False, "Discord is not connected"
    target = current_channel.get()
    where = "in this conversation"
    if target is None:
        target = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
        where = "to your Discord DMs"
    import discord

    async def send(_part):
        return await target.send(caption, file=discord.File(io.BytesIO(text.encode("utf-8")),
                                                            filename=filename))
    ok = await _send_durable(send, text, intent_id=f"file:{uuid.uuid4()}",
                             route=f"file:{getattr(target, 'id', 'owner')}", parts=[text])
    return ok, f"{where}, as the attached file {filename}"


def _record_outgoing(message, body: str, kind: str) -> None:
    mid = getattr(message, "id", None)
    if mid is None or not _ALLOWED_ID:
        return
    channel_id = getattr(getattr(message, "channel", None), "id", "")
    _discord_messages.record(str(mid), _ALLOWED_ID, str(channel_id), kind, body)


async def _send_owner(text: str, *, kind: str) -> bool:
    if _client is None or not _ALLOWED_ID or not text.strip():
        return False
    try:
        user = _client.get_user(int(_ALLOWED_ID))
        if user is None:
            user = await _client.fetch_user(int(_ALLOWED_ID))
        sent_messages = []
        async def tracked_send(part):
            message = await user.send(part)
            sent_messages.append(message)
            return message
        parts = _chunk(text)
        expected = len(parts)
        delivered = await _send_chunks(tracked_send, text)
        for index, message in enumerate(sent_messages):
            # The Discord ID belongs to this exact part. Include prior parts as
            # context when a user replies to a later chunk.
            _record_outgoing(message, "\n".join(parts[:index + 1]), kind)
        if delivered < expected:
            # Explicit uncertain delivery: some of it reached the owner. The caller will
            # treat this as unsent and may resend, which is the right trade for a
            # notification -- a duplicate is recoverable, a silent loss is not -- but
            # it must be visible in the log rather than looking like nothing happened.
            log.warning("Discord: partial delivery -- %d/%d parts sent; caller will "
                        "treat this as unsent", delivered, expected)
            return False
        return True
    except Exception as e:
        log.warning("Discord: send_to_owner failed: %s", e)
        return False


async def _context_for_message(message, text: str) -> _discord_context.TurnContext:
    message_id = str(getattr(message, "id", ""))
    reference = getattr(message, "reference", None)
    target_id = str(getattr(reference, "message_id", "") or "")
    if target_id:
        stored = _discord_messages.get(target_id, _ALLOWED_ID)
        if stored:
            return _discord_context.TurnContext(
                message_id, target_id, stored["body"], "Jarvis", stored["kind"])
        target = getattr(reference, "resolved", None)
        if not getattr(target, "content", None):
            try:
                target = await message.channel.fetch_message(int(target_id))
            except Exception:
                target = None
        author_id = str(getattr(getattr(target, "author", None), "id", ""))
        bot_id = str(getattr(getattr(_client, "user", None), "id", ""))
        if target is not None and author_id and author_id in {_ALLOWED_ID, bot_id}:
            author = _owner.say("«Owner»") if author_id == _ALLOWED_ID else "Jarvis"
            return _discord_context.TurnContext(
                message_id, target_id, str(getattr(target, "content", "")), author)
        return _discord_context.TurnContext(message_id, target_id)
    if _discord_context.looks_like_followup(text):
        channel_id = str(getattr(message.channel, "id", "") or "")
        recent = _discord_messages.latest_substantive(
            _ALLOWED_ID, channel_id=channel_id)
        if recent and recent["kind"] == "research":
            return _discord_context.TurnContext(
                message_id, recent["message_id"], recent["body"],
                "Jarvis", "research", implicit=True)
    return _discord_context.TurnContext(message_id)


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


_HEARD_CHARS = 300


def heard_line(text: str) -> str:
    """What JARVIS heard in a voice note, sent back before he answers, so a mishearing shows."""
    text = " ".join(text.split())
    if len(text) > _HEARD_CHARS:
        text = text[:_HEARD_CHARS].rsplit(" ", 1)[0] + " …"
    return f'Heard: "{text}"'


def _file_attachments(message) -> list:
    """Images and documents on a message (anything that is not a voice note)."""
    from backend.brain import attachments
    return [a for a in message.attachments
            if attachments.classify(a.filename, a.content_type or "") != "audio"]


def _voice_attachment(message):
    """Return the first audio attachment on a message, or None."""
    for att in message.attachments:
        ct = (att.content_type or "").lower()
        if ct.startswith("audio/") or att.filename.lower().endswith((".ogg", ".mp3", ".wav", ".m4a")):
            return att
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _build_client():
    import discord

    intents = discord.Intents.default()
    # Privileged intent -- must ALSO be enabled under the bot's settings in
    # the Discord Developer Portal, or message.content always arrives "".
    intents.message_content = True

    class _JarvisClient(discord.Client):
        async def on_ready(self) -> None:
            log.info("Discord bridge active as %s", self.user)
            if _client is self:
                spawn(_resume_pending())

        async def on_message(self, message) -> None:
            # Never react to the bot's own messages -- guards against any
            # accidental self-reply loop if it's ever in a shared channel.
            if message.author.id == self.user.id:
                return
            if not _ALLOWED_ID and _claim.is_claim(message.content or ""):
                await _accept_claim(message)
                return
            if str(message.author.id) != _ALLOWED_ID:
                log.warning(
                    "Discord: message from unauthorized user id=%s username=%s -- ignored",
                    message.author.id, message.author,
                )
                # Deliberately no reply -- don't confirm to a stranger that this bot exists/is attended.
                return

            # A02 (2026-09-22): ingress deduplication. The Discord gateway can redeliver
            # a message after a reconnect, and without this the same request runs twice
            # -- two turns, two sets of tool side effects, two replies. Claimed by
            # message id via a PRIMARY KEY conflict, so two concurrent deliveries cannot
            # both win a check-then-act race. Fails OPEN: if storage is unavailable the
            # message is admitted, because being unable to deduplicate is not a reason
            # to silently drop something the owner actually sent.
            #
            # Placed before command handling so a redelivered "/new" cannot rotate the
            # session twice either.
            if not _claim_ingress(message):
                log.info("Discord: duplicate delivery of message %s -- ignored", message.id)
                return

            text = (message.content or "").strip()

            reply = stop_reply(text)
            if reply is not None:
                await message.channel.send(reply)
                return

            if text.lower().startswith("/skill export"):
                if _skill_export_handler is None:
                    return
                name = text[len("/skill export"):].strip()
                try:
                    out = _skill_export_handler(name)
                except Exception as e:
                    log.error("Discord: /skill export failed: %s", e)
                    out = "Couldn't export that skill -- check the backend log."
                if isinstance(out, str):
                    await _send_reply(message.channel, out.removeprefix("Error: "))
                    return
                filename, body, caption = out
                token = current_channel.set(message.channel)
                try:
                    sent, how = await send_file(filename, body, caption)
                finally:
                    current_channel.reset(token)
                if not sent:
                    await message.channel.send(f"Couldn't send the file ({how}).")
                return

            if " ".join(text.lower().split()) == "/brief now":
                async with _typing(message.channel):
                    reply = await _commands.dispatch(
                        text, channel="discord",
                        deliver=lambda part: _send_reply(message.channel, part))
            else:
                reply = await _commands.dispatch(text, channel="discord")
            if reply is not None:
                if reply:
                    await _send_reply(message.channel, reply)
                return

            # P1: "yes 12" / "no 12" answers an approval request. Only that exact shape, and
            # only for a request that exists; anything else falls through as a normal message.
            if text and _approval_reply_handler is not None:
                try:
                    answer = await _approval_reply_handler(text)
                except Exception as e:
                    log.error("Discord: approval reply failed: %s", e)
                    answer = None
                if answer is not None:
                    await _send_reply(message.channel, answer)
                    return

            voice = _voice_attachment(message)
            if voice is not None:
                await self._handle_voice(message, voice)
                return

            files = _file_attachments(message)
            if files:
                await self._handle_files(message, text, files)
                return

            if not text:
                return
            await self._handle_text(message, text)

        async def _handle_files(self, message, text: str, files) -> None:
            """Images and documents: kept and read (attachments.py), then one ordinary turn
            whose text is his words plus a line naming what he sent."""
            from backend.brain import attachments
            try:
                async with _typing(message.channel):
                    items = []
                    for att in files:
                        too_big = (getattr(att, "size", 0) or 0) > attachments.MAX_DOCUMENT_BYTES
                        items.append((att.filename, att.content_type or "",
                                      None if too_big else await att.read()))
                    received = await attachments.receive(items, "discord")
            except Exception as e:
                log.error("Discord: could not take attachments: %s", e)
                await message.channel.send("Couldn't read what you sent -- check the backend log.")
                return
            problems = [f"{r.name} ({r.error})" for r in received if r.error]
            if problems:
                await message.channel.send("Couldn't read " + "; ".join(problems) + ".")
            line = attachments.marker(received)
            if not line and not text:
                return
            await self._handle_text(message, f"{text}\n\n{line}".strip() if line else text)

        async def _handle_text(self, message, text: str) -> None:
            if _turn_handler is None:
                await message.channel.send("Not ready yet -- try again in a moment.")
                return
            try:
                context = await _context_for_message(message, text)
                token = _discord_context.current.set(context)
                channel_token = current_channel.set(message.channel)
                async with _typing(message.channel):
                    try:
                        response = await _run_turn_with_progress(message, text)
                    finally:
                        _discord_context.current.reset(token)
                        current_channel.reset(channel_token)
                await _send_reply(message.channel, response,
                                  delivery_key=f"reply:{message.id}")
            except Exception as e:
                log.error("Discord: turn failed: %s", e)
                await message.channel.send("Something went wrong on my end and I couldn't "
                                           "finish that. Ask again, or check the backend log.")

        async def _handle_voice(self, message, attachment) -> None:
            if _turn_handler is None or _transcribe_handler is None:
                await message.channel.send("Not ready yet -- try again in a moment.")
                return
            tmp_path = None
            try:
                async with _typing(message.channel):
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                        tmp_path = tmp.name
                    await attachment.save(tmp_path)

                    loop = asyncio.get_event_loop()
                    text = await loop.run_in_executor(None, _transcribe_handler, tmp_path)

                    if not text.strip():
                        await message.channel.send("Couldn't make out anything in that.")
                        return
                    await message.channel.send(heard_line(text))

                    context = dataclasses.replace(await _context_for_message(message, text),
                                                  voice=True)
                    token = _discord_context.current.set(context)
                    channel_token = current_channel.set(message.channel)
                    try:
                        response = await _run_turn_with_progress(message, text)
                    finally:
                        _discord_context.current.reset(token)
                        current_channel.reset(channel_token)
                await _send_reply(message.channel, response,
                                  delivery_key=f"reply:{message.id}")
            except Exception as e:
                log.error("Discord: voice turn failed: %s", e)
                await message.channel.send("Couldn't process that voice message -- check the backend log.")
            finally:
                if tmp_path:
                    _cleanup(tmp_path)

    client = _JarvisClient(intents=intents)
    # P1: registering the button TEMPLATE (not each message's view) is what lets a tap on a
    # request sent before a restart still reach its handler: Discord delivers the custom_id and
    # the library matches it against this pattern.
    try:
        client.add_dynamic_items(_approval_button())
    except Exception as e:                      # never let this stop the bridge starting
        log.warning("Discord: approval buttons unavailable (%s); replying \"yes N\" still works", e)
    return client


# ---------------------------------------------------------------------------
# P1: approval requests -- Approve / Deny that keep working after a restart
# ---------------------------------------------------------------------------

_approval_button_cls = None
_ANNOTATION = "\n\n\u2192 "


def _approval_button():
    """The persistent button class, built on first use because `discord` is imported lazily.

    A `DynamicItem` is matched by a pattern on the button's custom_id ("approval:12:approve"),
    not by a stored view, so a request sent yesterday still answers today.
    """
    global _approval_button_cls
    if _approval_button_cls is not None:
        return _approval_button_cls
    import discord

    class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"approval:(?P<id>[0-9]+):(?P<verb>approve|deny)"):
        def __init__(self, approval_id: int, verb: str):
            approve = verb == "approve"
            super().__init__(discord.ui.Button(
                label="Approve" if approve else "Deny",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"approval:{approval_id}:{verb}"))
            self.approval_id = approval_id
            self.verb = verb

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /):
            return cls(int(match["id"]), match["verb"])

        async def interaction_check(self, interaction) -> bool:
            if str(interaction.user.id) == _ALLOWED_ID:
                return True
            try:
                await interaction.response.send_message("This isn't yours to answer.", ephemeral=True)
            except Exception:
                pass
            return False

        async def callback(self, interaction):
            # Sending an email can take a few seconds; Discord wants an answer in three.
            await interaction.response.defer()
            if _approval_decider is None:
                text = "Not ready yet -- tap again in a moment."
            else:
                try:
                    text = await _approval_decider(self.approval_id, self.verb == "approve", "button")
                except Exception as e:
                    log.error("Discord: approval %s failed: %s", self.approval_id, e)
                    text = "Something went wrong handling that -- check the backend log."
            await _annotate(interaction.message, text)

    _approval_button_cls = ApprovalButton
    return ApprovalButton


async def _annotate(message, text: str) -> bool:
    """Show the outcome under the request and take the buttons away. True if it was shown."""
    try:
        base = (message.content or "").split(_ANNOTATION)[0]
        await message.edit(content=(base + _ANNOTATION + text)[:_MAX_MESSAGE_CHARS], view=None)
        return True
    except Exception as e:
        log.debug("Discord: could not update approval message: %s", e)
        return False


async def post_card(text: str) -> tuple[str, str]:
    """G1b: post a goal's card to the owner. ("sent", message id), ("failed", "") when Discord
    definitely did not take it (no connection, a rejected request), or ("uncertain", "") when
    it may have: a timeout or a dropped connection. The caller never posts again after an
    uncertain one -- a second card would be worse than a missing one."""
    if _client is None or not _ALLOWED_ID:
        return "failed", ""
    try:
        import discord
        user = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
        body = text[:_MAX_MESSAGE_CHARS]
        message = await user.send(body)
        _record_outgoing(message, body, "goal_card")
        return "sent", str(message.id)
    except Exception as e:
        try:
            import discord
            if isinstance(e, discord.HTTPException) and 400 <= int(getattr(e, "status", 0)) < 500:
                log.warning("Discord: goal card rejected (%s)", e)
                return "failed", ""
        except Exception:
            pass
        log.warning("Discord: goal card post uncertain (%s)", e)
        return "uncertain", ""


async def edit_card(message_id: str, text: str) -> str:
    """G1b: edit a goal's card in place. "ok", "gone" (Discord says the message no longer
    exists: a replacement may be posted) or "uncertain" (anything else: retried later)."""
    if _client is None or not _ALLOWED_ID:
        return "uncertain"
    try:
        import discord
        user = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
        dm = user.dm_channel or await user.create_dm()
        try:
            message = await dm.fetch_message(int(message_id))
        except discord.NotFound:
            return "gone"
        await message.edit(content=text[:_MAX_MESSAGE_CHARS])
        return "ok"
    except Exception as e:
        log.debug("Discord: goal card %s not edited (%s)", message_id, e)
        return "uncertain"


async def send_approval_request(row: dict) -> Optional[str]:
    """DM the request with Approve / Deny buttons. Returns the message id, or None when it
    could not be delivered (the caller keeps the request pending and mentions it next turn)."""
    if _client is None or not _ALLOWED_ID:
        return None
    try:
        import discord
        from backend.tools import approval_flow
        button = _approval_button()
        view = discord.ui.View(timeout=None)
        view.add_item(button(row["id"], "approve"))
        view.add_item(button(row["id"], "deny"))
        user = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
        body = approval_flow.request_text(row)[:_MAX_MESSAGE_CHARS]
        message = await user.send(body, view=view)
        _record_outgoing(message, body, "approval")
        return str(message.id)
    except Exception as e:
        log.warning("Discord: could not deliver approval request #%s: %s", row.get("id"), e)
        return None


async def update_approval_message(row: dict, text: str) -> bool:
    """Say what became of a request, on the request itself when it can still be found.
    True when he was told; False when Discord could not be reached."""
    if _client is None or not _ALLOWED_ID:
        return False
    message_id = row.get("message_id")
    if message_id:
        try:
            user = _client.get_user(int(_ALLOWED_ID)) or await _client.fetch_user(int(_ALLOWED_ID))
            dm = user.dm_channel or await user.create_dm()
            if await _annotate(await dm.fetch_message(int(message_id)), text):
                return True
        except Exception as e:
            log.debug("Discord: approval message %s not found (%s) -- sending a new one", message_id, e)
    return await send_to_owner(text)


async def _accept_claim(message) -> None:
    """A "/claim" while there is no owner. Only the right code, in a DM, makes the
    sender the owner; anything else gets no reply, like any stranger's message."""
    global _ALLOWED_ID
    author = str(message.author.id)
    if not _claim.try_claim(author, message.content or "", in_dm=message.guild is None):
        return
    _ALLOWED_ID = author
    saved = _claim.save_owner(author)
    log.warning("Discord: user id=%s (%s) claimed the bot%s", author, message.author,
                "" if saved else " -- .env could not be written, so only until a restart")
    if saved:
        reply = ("You're my owner now. I saved your Discord id to .env, so this holds after "
                 "a restart. Try /status, or just talk to me.")
    else:
        reply = (f"You're my owner for now, but I could not write .env. Add "
                 f"DISCORD_ALLOWED_USER_ID={author} to it, or the next restart forgets you.")
    try:
        await message.channel.send(reply)
    except Exception as exc:                   # the claim stands either way
        log.warning("Discord: could not confirm the claim: %s", exc)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start() -> None:
    """Connect to Discord's gateway in the background, and keep it connected.

    No-op when DISCORD_BOT_TOKEN isn't set. Otherwise a supervisor task owns the
    connection for the life of the process: a connection that fails or is lost is
    retried after _RETRY_DELAYS_S, never given up on. start() waits up to
    _READY_TIMEOUT for the first connection so the startup notices that follow it
    can be sent, then returns either way -- a slow connection is left to finish.

    Until 6 November 2026 a connection not ready within 15 s was closed and never
    retried: the server ran a day without Discord while the phone app worked.
    """
    global _supervisor, _stopping
    if not _BOT_TOKEN:
        log.info("Discord bridge: not configured (DISCORD_BOT_TOKEN unset) -- skipping.")
        return
    if not _ALLOWED_ID:
        # No owner yet: on_message rejects everyone except a DM claiming the bot with
        # this process's one-time code (discord_claim.py).
        log.warning(
            "Discord bridge: no owner yet (DISCORD_ALLOWED_USER_ID is empty). To make "
            "yourself the owner, send the bot this DM: /claim %s  (works once; a "
            "restart makes a new code)", _claim.open_claim()
        )
    if _supervisor is not None and not _supervisor.done():
        return
    _stopping = False
    _link.update(state="connecting", error="", retry_at=0.0)
    _supervisor = spawn(_supervise(), name="discord-supervisor")
    deadline = time.monotonic() + _READY_TIMEOUT
    while _client is None and time.monotonic() < deadline and not _supervisor.done():
        await asyncio.sleep(0.2)
    if _client is None:
        log.warning("Discord bridge: not connected after %.0f s -- still trying in the background.",
                    _READY_TIMEOUT)


async def _supervise() -> None:
    """Connect, and reconnect whenever the connection ends, until stop()."""
    failures = 0
    while not _stopping:
        became_ready, error = await _connect_once()
        if _stopping:
            return
        failures = 0 if became_ready else failures + 1
        delay = _RETRY_DELAYS_S[min(max(failures - 1, 0), len(_RETRY_DELAYS_S) - 1)]
        _link.update(state="down", error=error, retry_at=time.time() + delay)
        log.warning("Discord bridge: connection %s (%s) -- retrying in %.0f s.",
                    "lost" if became_ready else "failed", error or "no error given", delay)
        await asyncio.sleep(delay)
        _link.update(state="connecting")


async def _connect_once() -> tuple[bool, str]:
    """Run one client until its connection ends. Returns (became_ready, error).

    discord.py's own client.start() already resumes ordinary drops; it ends only on
    something it cannot recover from (a bad token, the network gone at login), and
    that is what this returns for. The client is never closed while it is still
    connecting."""
    global _client, _client_task
    became_ready = False
    error = ""
    client = None
    try:
        client = _build_client()
        _client_task = asyncio.create_task(client.start(_BOT_TOKEN))
        while not _client_task.done():
            if not became_ready and client.is_ready():
                became_ready = True
                _client = client
                _link.update(state="connected", error="", retry_at=0.0)
                log.info("Discord bridge: connected.")
                reconciled = _delivery.reconcile_inflight()
                if reconciled:
                    log.warning("Discord: %d in-flight reply part(s) have uncertain delivery", reconciled)
                spawn(_resume_pending())
            # Connected: just wait for the connection to end. Still connecting:
            # look again for readiness every 0.2 s.
            await asyncio.wait({_client_task}, timeout=None if became_ready else 0.2)
        if _client_task.cancelled():
            error = "cancelled"
        else:
            exc = _client_task.exception()
            error = f"{type(exc).__name__}: {exc}".strip(": ") if exc else "connection closed"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        if _client is client:
            _client = None
        _client_task = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    return became_ready, error


async def stop() -> None:
    """Disconnect gracefully and stop reconnecting. Safe if start() never ran or failed."""
    global _client, _client_task, _supervisor, _stopping
    _stopping = True
    client = _client
    try:
        if client is not None:
            await client.close()
    except Exception as e:
        log.debug("Discord bridge stop: %s", e)
    if _supervisor is not None and not _supervisor.done():
        _supervisor.cancel()
        try:
            await _supervisor
        except (asyncio.CancelledError, Exception):
            pass
    _supervisor = None
    _client = None
    _client_task = None
    _link.update(state="off", error="", retry_at=0.0)
