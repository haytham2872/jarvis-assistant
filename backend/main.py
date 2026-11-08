import os
import time
import base64
import tempfile
import json
import asyncio
import argparse
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

# All models are pre-downloaded by setup.ps1 — skip HuggingFace version-check
# network requests on every startup (~10s saved). Override with HF_HUB_OFFLINE=0 in .env.
import os as _os
_os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from backend.brain.engine import chat, flush_session, force_new_session, set_plan_notifier, set_clarify_notifier
from backend.brain import history_bridge, history_refill
from backend.brain import steering
from backend.brain.memory import warm_mem
from backend.brain.mood import warm_mood
from backend.brain.router import log_startup_mode, provider as _provider
from backend.providers import gateway
from backend.storage import runs as _runs
from backend.storage import operations as _ops
from backend.storage import db as _db
from backend.brain.self_model import warm_self_model
from backend.brain import self_model
from backend.brain.embeddings import warm_embedder
from backend.brain.intent import warm_intent
from backend.brain import semindex
from backend.brain import bootstrap
from backend.brain import turn_input as _turn_input
from backend.mobile import conversation as mobile_conversation
from backend.mobile import live_speech
from backend.mobile import routes as mobile_routes
from backend.brain import background
from backend.brain import threads as _threads
from backend.brain import discord_bridge
from backend.brain import notices
from backend.brain import research
from backend.brain import backup_loop
from backend.brain import canary
from backend.brain import runtime_status
from backend.brain import usage
from backend.brain import watches
from backend.brain import instructions
from backend.brain import forecasts
from backend.brain import people_research
from backend.brain import session_close
from backend.brain import turn_signals
from backend.brain import rerank
from backend.brain import chronos
from backend.brain import goal_runner
from backend.brain import goals as goal_tool
from backend.brain import rumination
from backend.brain import evolution
from backend.brain import pitch
from backend.brain import pause
from backend.brain import activity
from backend.brain import planday
from backend.brain import planloop
from backend.brain import portfolio
from backend.brain import tool_record
from backend.brain import skills
from backend.brain import skill_nightly
from backend.tools import approval_flow
from backend.tools import gateway as _tool_gateway
from backend.brain import outward_check
from backend.brain.sleep import record_sleep
from backend.tasks import spawn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request at INFO (one line per Qdrant call, per TTS sentence,
# per HF HEAD request) and drowns the terminal — warnings only.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("jarvis")

# The only browser-rendered origins that should ever be allowed to talk to this
# backend: the Vite dev server and the packaged Electron renderer (file://
# pages send Origin: null, which shows up as the literal string "null").
# Non-browser callers (teardown.ps1, cli.py, curl) never send an Origin header
# at all -- those are allowed through since there's nothing to spoof there.
# A malicious website's JS is a real threat here: browsers let cross-origin
# WebSocket connections and simple (non-preflighted) POST/GET requests through
# regardless of CORS -- CORS only blocks the page from reading the response.
# Checking Origin server-side is what actually stops that page from ever
# reaching /shutdown, /inspector, or the WebSocket in the first place.
_ALLOWED_ORIGINS = frozenset([
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "null",       # some Chromium/Electron versions serialize a file:// page's
                  # opaque origin as the literal string "null" per the Fetch spec
    "file://",    # others send the literal scheme instead -- confirmed via the
                  # real -Prod Electron build rejecting every connection with
                  # only "null" allowlisted. Neither string is ever what a real
                  # remote attacker's page sends (that's always its own origin),
                  # so allowing both widens compatibility, not the attack surface.
])


def _origin_allowed(origin: str | None) -> bool:
    return origin is None or origin in _ALLOWED_ORIGINS


connected_clients: set[WebSocket] = set()
# One history list per channel ("local" = voice/UI/terminal, "discord" = the
# phone bridge) -- a text from your phone shouldn't get glued into the middle
# of whatever you were saying at your desk, or vice versa. Long-term memory
# (mood, mem0, vault, skills) stays global/shared; only this short-term
# "what did we just say" list is split per channel.
conversation_histories: dict[str, list[dict]] = {}
_no_wake = False
_no_tts = os.getenv("NO_TTS", "0") == "1"

# A01 (2026-09-21): audio is built on first use, not at import.
#
# `stt = SpeechToText()` at module scope loaded the faster-whisper model on EVERY
# startup -- including the headless Linux/GCP deployment, which has no microphone and
# only needs transcription for the occasional Discord voice note. Importing
# backend.audio.{stt,tts} at module scope also pulled in `sounddevice`, i.e. PortAudio,
# on a box that may not have it at all. The roadmap's A01 acceptance is explicit: "Text
# startup does not initialize microphone/TTS/Whisper; voice still transcribes when
# needed."
#
# These are module-level singletons exactly as before -- only their construction moved.
_stt = None
_tts = None


def get_stt():
    """The desk microphone's SpeechToText, built on first use (its model loads on the first
    transcription, see backend/audio/transcriber.py)."""
    global _stt
    if _stt is None:
        from backend.audio.stt import SpeechToText
        log.info("Loading speech-to-text (first use) …")
        _stt = SpeechToText()
    return _stt


_tts_unavailable = False


def get_tts():
    """The shared TextToSpeech, or None when NO_TTS=1 or the machine cannot play audio.
    Built on first use.

    A headless Linux server has no PortAudio and no sound card; a typed turn there (the
    terminal client) used to fail with "PortAudio library not found" (found by the
    fresh-install test). It is logged once and the turn is answered in text."""
    global _tts, _tts_unavailable
    if _no_tts or _tts_unavailable:
        return None
    if _tts is None:
        log.info("Loading text-to-speech (first use) …")
        try:
            from backend.audio.tts import TextToSpeech
            _tts = TextToSpeech()
        except Exception as exc:          # OSError from PortAudio, ImportError, no device
            _tts_unavailable = True
            log.warning("Speech is unavailable on this machine (%s) -- answering in text "
                        "only. Set NO_TTS=1 to skip this check.", exc)
            return None
    return _tts


def transcribe_file(path: str) -> str:
    """The Discord bridge's transcribe handler: straight to the transcriber, so a voice note
    on the server never loads the desk microphone code (PortAudio). The model itself loads on
    the first note and is released after 10 idle minutes."""
    from backend.audio import transcriber
    return transcriber.transcribe_file(path)
_pipeline_lock = asyncio.Lock()
_current_pipeline_task: asyncio.Task | None = None
# The answer being written now, and whether the owner asked to stop it.
_current_chat_task: asyncio.Task | None = None
_stop_requested = False


def stop_turn() -> bool:
    """Stop the answer being written now: the phone's Stop button, /stop on Discord, the
    desk's interrupt. Only the turn's chat task is cancelled. The task running the turn can
    be the loop that answers every phone message, and cancelling it would leave the phone
    unanswered until a restart (20 October: a reasoning loop ran 6 minutes and the owner had
    to restart the server twice). Goals and background jobs are untouched."""
    global _stop_requested
    if _tts is not None:
        _tts.interrupt()
    task = _current_chat_task
    if task is None or task.done():
        return False
    _stop_requested = True
    task.cancel()
    log.info("Turn stopped by the owner")
    return True

# Wake-word availability, cached so a client that connects after startup (the
# broadcast during lifespan fires before anyone is connected to receive it)
# still learns the real status instead of the UI silently defaulting to
# "assume active".
_wake_status: dict = {"type": "wake_status", "active": False, "reason": "starting up"}
# One task per paced background loop, keyed by name -- see bootstrap.py.
# "research" stays first in the startup dict below so it's also cancelled
# first at shutdown (it may be mid-tick trying to message over the very
# Discord connection about to be torn down).
_loop_tasks: dict[str, asyncio.Task] = {}


async def _drain_sentence_queue(q: asyncio.Queue) -> None:
    """Consume and discard sentences from the queue (NO_TTS mode)."""
    while True:
        item = await q.get()
        if item is None:
            break


async def broadcast(msg: dict):
    # Snapshot to a list before the loop: connected_clients can gain or lose
    # members (a client connecting/disconnecting) between awaits, and mutating
    # a set while iterating it directly raises RuntimeError mid-broadcast.
    dead = set()
    text = json.dumps(msg)
    for ws in list(connected_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


async def send_state(state: str):
    await broadcast({"type": "state", "value": state})


_MAX_HISTORY_TURNS = 40   # CLAUDE.md: "Conversation history capped at 40 turns"
_MAX_HISTORY_ENTRIES = _MAX_HISTORY_TURNS * 2   # one user + one assistant entry per turn

# Bridges the 40-turn cliff above -- see backend/brain/history_bridge.py for
# why this exists and the (pure, unit-tested) logic behind it. main.py just
# owns the per-channel state dict and wires it into chat()'s history_bridge param.
history_bridges: dict[str, list[str]] = {}


def _get_history(channel: str) -> list[dict]:
    return conversation_histories.setdefault(channel, [])


def _get_history_bridge(channel: str) -> list[str]:
    return history_bridges.setdefault(channel, [])


# A03: when each history entry happened, kept PARALLEL to conversation_histories rather
# than as a field on the entries themselves -- those dicts are sent verbatim to the
# provider as chat messages, and an unknown key there is at best ignored and at worst
# rejected. Same length and order as the history list, so a slice of one indexes the
# other.
history_times: dict[str, list[float]] = {}


def _get_history_times(channel: str) -> list[float]:
    return history_times.setdefault(channel, [])


def history_bridge_text(channel: str) -> str:
    return history_bridge.format_text(_get_history_bridge(channel))


def _refill_history(channel: str) -> int:
    """At startup, put back the conversation a restart interrupted (history_refill.py).
    Returns how many turns were restored."""
    rows = history_refill.restore(channel, _MAX_HISTORY_TURNS)
    if not rows:
        return 0
    hist, times = [], []
    for r in rows:
        ts = float(r["timestamp"])
        hist += [{"role": "user", "content": r["user_text"]},
                 {"role": "assistant", "content": r["assistant_text"]}]
        times += [ts, ts]
    conversation_histories[channel] = hist
    history_times[channel] = times
    log.info("History: restored %d turn(s) on %s from before the restart", len(rows), channel)
    return len(rows)


def _history_since(channel: str) -> float | None:
    """When the oldest turn in the channel's live history started, or None when it is empty.
    Recall leaves out stored exchanges from then on: the model already reads them."""
    times = history_times.get(channel)
    return times[0] if times else None


def _append_history(channel: str, user_text: str, response: str,
                    started: float | None = None) -> None:
    """`started`: when the turn began. Its stored exchange is written when the reply is
    done, so a start time keeps that exchange at or after its own history entry, which
    _history_since relies on (at the desk, speech can end a minute after the reply)."""
    hist = _get_history(channel)
    times = _get_history_times(channel)
    now = time.time() if started is None else started
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": response})
    times.extend((now, now))
    # Defensive: if the two ever desynchronise (an entry appended elsewhere), rebuild
    # rather than slice them apart. A wrong date on a bridge line is worse than none.
    if len(times) != len(hist):
        times[:] = [now] * len(hist)
    if len(hist) > _MAX_HISTORY_ENTRIES:
        cut = len(hist) - _MAX_HISTORY_ENTRIES
        dropped, dropped_times = hist[:cut], times[:cut]
        hist[:] = hist[-_MAX_HISTORY_ENTRIES:]
        times[:] = times[-_MAX_HISTORY_ENTRIES:]
        history_bridge.record_dropped(
            _get_history_bridge(channel), dropped, dropped_times)


async def _execute_turn(text: str, channel: str = "local", want_tts: bool = True) -> str | None:
    """Run one chat → TTS turn and wake the admission of anything that arrived meanwhile.

    G0 (D1) joined follow-ups into this turn's reply; G3 (2026-10-06) gives every message
    its own turn and its own reply instead. A message that arrived while this turn ran and
    was not absorbed at a tool-loop seam (a turn with no tool calls has none) is still
    pending in the inbox; `_admit_pending` answers it as soon as the lock is free.
    """
    response = await _execute_turn_once(text, channel, want_tts)
    _admission.set()
    return response


# The conversation and run being executed right now (read by the entry points of OTHER
# messages, which run in their own tasks, to decide whether a message can steer it).
_running: dict = {}
# Set when something may be waiting in the inbox; the admission loop waits on it.
_admission = asyncio.Event()


def _turn_identity(channel: str) -> tuple[str, str | None, dict]:
    """(conversation id, message key, delivery context) of the message being answered.

    N5: the DM, each server channel and the desk are separate conversations. Discord's
    are read from the bridge's context for the message in hand."""
    if channel == mobile_conversation.CHANNEL:
        return mobile_conversation.CONVERSATION, steering.current_message_key.get(), {}
    if channel != "discord":
        return "local", None, {}
    from backend.brain import discord_context
    ctx = discord_context.current.get()
    chan = discord_bridge.current_channel.get()
    conv = discord_bridge.conversation_of(chan)
    key = ctx.message_id if ctx is not None and ctx.message_id else None
    context = {"message_id": key, "channel_id": getattr(chan, "id", None),
               "target_id": getattr(ctx, "target_id", "") if ctx else "",
               "voice": bool(getattr(ctx, "voice", False)) if ctx else False}
    return conv, key, context


async def _admit_pending(limit: int = 20) -> int:
    """Answer waiting messages, oldest first, each as its own turn with its own reply.
    Returns how many were taken. A message that fails is recorded failed and the owner is
    told; nothing waits silently."""
    done = 0
    for _ in range(limit):
        async with _pipeline_lock:
            row = steering.claim_next()
            if row is None:
                return done
            ok = await _answer_admitted(row)
        steering.finish(row["message_key"], ok)
        done += 1
    return done


def _inbox_notice(notice_id) -> dict | None:
    """The Inbox message a phone message answers, read from the store (never from the phone)."""
    if not notice_id:
        return None
    try:
        from backend.storage import notices as _notice_store
        return _notice_store.get(int(notice_id))
    except Exception:
        return None


async def _answer_admitted(row: dict) -> bool:
    text, conv = row["text"], row["conversation_id"]
    if conv == "local":
        with steering.turn("local", None, row["message_key"]):
            return bool(await _execute_turn_once(text, "local", True))
    if conv == mobile_conversation.CONVERSATION:
        # The phone reads its answer from events and mobile_requests, not from a reply here.
        # A voice message is a transcript, and JARVIS is told so (turn_input -> _VOICE_NOTE).
        from backend.brain import turn_input
        ctx = row.get("context") or {}
        token = turn_input.voice.set(ctx.get("input") == "voice")
        spoken = turn_input.spoken.set(bool(ctx.get("spoken")))
        # Slice 4: answering a message from the Inbox; the turn is shown that message.
        reply = turn_input.reply_to.set(_inbox_notice(ctx.get("reply_to")))
        try:
            with steering.turn(conv, None, row["message_key"]):
                return bool(await _execute_turn_once(text, mobile_conversation.CHANNEL, False))
        finally:
            turn_input.reply_to.reset(reply)
            turn_input.spoken.reset(spoken)
            turn_input.voice.reset(token)

    async def run_turn(t: str) -> str | None:
        response = await _execute_turn_once(t, "discord", False)
        return await _shape_discord(response, t) if response else None
    try:
        return bool(await discord_bridge.answer_admitted(row["context"], text, run_turn))
    except Exception as e:
        log.warning("Admitted message %s failed: %s", row["message_key"], e)
        return False


async def _admission_loop() -> None:
    """Answer whatever waits in the inbox whenever JARVIS is free: messages that arrived
    during a turn and were not absorbed, a Discord message sent during a desk turn (D11),
    and, after a restart, anything acknowledged but never started."""
    while True:
        try:
            try:
                await asyncio.wait_for(_admission.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            _admission.clear()
            if steering.pending_turns():
                await _admit_pending()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Admission loop: %s", e)


async def _execute_turn_once(text: str, channel: str = "local", want_tts: bool = True) -> str | None:
    """Run one chat → TTS turn for already-transcribed text.

    The single pipeline body shared by all entry points (browser audio, typed
    text, wake word, terminal, Discord). Caller must hold _pipeline_lock.
    Broadcasts the user transcript, streams the response through TTS, appends
    to the channel's own history (40 turn cap), and handles interruption and
    errors. Returns the response, or None when the turn produced nothing
    (empty input, error, interrupt).

    want_tts=False (Discord) skips audio synthesis entirely regardless of the
    global NO_TTS setting -- a text you send from your phone shouldn't make
    the PC start talking to an empty room.
    """
    global _current_pipeline_task, _current_chat_task, _stop_requested
    _current_pipeline_task = asyncio.current_task()
    if not text.strip():
        await send_state("idle")
        return None

    # Cheap visual cue in the existing transcript view so a Discord exchange
    # doesn't look indistinguishable from a desk one if the UI happens to be
    # open at the same time -- no frontend changes needed for this.
    label = "" if channel == "local" else f"[{channel.capitalize()}] "
    _started = time.time()

    await broadcast({"type": "transcript", "role": "user", "text": f"{label}{text}"})
    await send_state("thinking")

    sentence_queue: asyncio.Queue = asyncio.Queue()

    async def amplitude_cb(amp: float):
        await broadcast({"type": "amplitude", "value": amp})

    async def run_chat():
        try:
            return await chat(
                text, _get_history(channel), _send,
                sentence_queue=sentence_queue, channel=channel,
                history_bridge=history_bridge_text(channel),
                history_since=_history_since(channel),
            )
        finally:
            await sentence_queue.put(None)

    # A02: durable run record. Opened here and closed in the finally below, so a turn
    # killed mid-flight (crash, taskkill outrunning graceful shutdown, host reboot)
    # leaves a `running` row that startup reconciliation marks `interrupted`. Nothing is
    # ever auto-retried from it -- an uncertain side effect must stay uncertain.
    _run_id = _runs.begin_run(channel, text)
    # G3: this turn's conversation, run and message, for the steering drain (only messages
    # aimed at this run of this conversation join it) and for goal steers/answers keyed by
    # the message. Set before the chat task is created, like the run id below.
    _conversation, _message_key, _ = _turn_identity(channel)
    _message_key = steering.current_message_key.get() or _message_key
    _turn_ctx = steering.turn(_conversation, _run_id, _message_key)
    _turn_ctx.__enter__()
    _running.update(conversation=_conversation, run_id=_run_id)
    # Tool receipts are written deep inside engine.py, where the run id is not in scope.
    # A ContextVar rather than a module global: tools run concurrently under
    # asyncio.gather, and each task inherits the context of the turn that spawned it.
    # Set BEFORE the chat task is created: a task copies its context at creation. Until
    # 4 October 2026 it was set after, so every live turn's receipts had no run id (the
    # server showed 167 turns with no tool calls; test_turn_run_id.py).
    _ops.current_run_id.set(_run_id)
    # The phone: chat()'s events also go to its stream, and its request learns the run.
    _send, _request_id = broadcast, None
    if channel == mobile_conversation.CHANNEL:
        _request_id = mobile_conversation.request_id_of(_message_key)
        _send = mobile_conversation.sink(broadcast, _run_id, _request_id)
        mobile_conversation.started(_run_id, _request_id)
    # A heard phone turn speaks while it is written (mobile/live_speech.py).
    _live = None
    if _request_id and _turn_input.spoken.get():
        _live = live_speech.start(_request_id, text)
    _answer = None
    chat_task = asyncio.create_task(run_chat())
    _current_chat_task, _stop_requested = chat_task, False
    _run_state = _runs.FAILED      # overwritten on every deliberate exit path below
    _run_error = None
    try:
        tts = get_tts() if want_tts else None
        if _live is not None:
            await _live.run(sentence_queue)
        elif tts is None:
            await _drain_sentence_queue(sentence_queue)
        else:
            await tts.speak_from_queue(
                sentence_queue,
                on_state_cb=lambda: send_state("speaking"),
                on_amplitude=amplitude_cb,
            )
        response = await chat_task
        _append_history(channel, text, response, started=_started)
        await broadcast({"type": "transcript", "role": "assistant", "text": f"{label}{response}"})
        _run_state = _runs.COMPLETED
        _answer = response
        return response
    except asyncio.CancelledError:
        if _stop_requested and chat_task.cancelled():
            # Stopped by the owner: the answer is dropped and the turn ends here, without
            # cancelling the task this turn runs in (see stop_turn).
            _run_state = _runs.INTERRUPTED
            return None
        # Interrupt: salvage the response if the chat already finished.
        if not chat_task.done():
            chat_task.cancel()
        elif not chat_task.cancelled():
            try:
                response = chat_task.result()
                if response:
                    _append_history(channel, text, response, started=_started)
                    await broadcast({"type": "transcript", "role": "assistant", "text": f"{label}{response}"})
            except Exception:
                pass
        _run_state = _runs.INTERRUPTED
        raise
    except Exception as e:
        chat_task.cancel()
        log.error("Pipeline error: %s", e)
        await broadcast({"type": "error", "message": str(e)})
        _run_state, _run_error = _runs.FAILED, str(e)[:500]
        return None
    finally:
        _current_pipeline_task = None
        _current_chat_task = None
        # Messages this run absorbed take its outcome: answered, or cut off (reported,
        # never rerun on a guess).
        steering.finish_run(_run_id, _run_state == _runs.COMPLETED)
        _running.clear()
        _turn_ctx.__exit__(None, None, None)
        _runs.finish_run(_run_id, _run_state, error=_run_error)
        if channel == mobile_conversation.CHANNEL:
            from backend.brain import skills as _skills_line
            footer = _skills_line.pop_footer(channel) if _answer else ""
            if _live is not None:
                # Every speech_clip and speech_end go out before run_finished.
                _live.finish(_answer, _run_state == _runs.COMPLETED)
            mobile_conversation.finished(_run_id, _request_id, _run_state == _runs.COMPLETED,
                                         (_answer + footer) if _answer else None, _run_error)
        _log_expensive_turn(_run_id, text)
        await send_state("idle")


async def _announce_cut_off_turns(started, runs: list[dict],
                                  messages: list[dict] | None = None) -> None:
    """H4: after a restart, tell the owner which Discord requests were cut off mid-turn.
    Deliberately not retried -- the turn's side effects may already have landed.
    G3: also the messages he sent during such a turn, which it was absorbing."""
    try:
        await started
        await discord_bridge.wait_connected()   # a slow connection must not drop these
        for m in messages or []:
            preview = " ".join((m.get("text") or "").split())[:120]
            if preview:
                await discord_bridge.send_to_owner(
                    f'You also sent "{preview}" while that was running; it was cut off with '
                    "it. Send it again if you still want it.")
        for r in runs:
            preview = " ".join((r.get("user_preview") or "").split())[:120]
            if preview:
                await discord_bridge.send_to_owner(
                    f'I was restarted while working on "{preview}" and didn\'t finish it. '
                    "Nothing more is coming for that one; send it again if you still want it.")
    except Exception as e:
        log.warning("Could not announce interrupted Discord turns: %s", e)


async def _announce_interrupted_approvals(started, rows: list[dict]) -> None:
    """P1: an approved request found mid-run at boot is `uncertain` (already recorded);
    tell the owner once Discord is up. Never retried -- an email may already have gone."""
    try:
        await started
        await discord_bridge.wait_connected()   # a slow connection must not drop these
        await approval_flow.announce_interrupted(rows)
    except Exception as e:
        log.warning("Could not announce interrupted approvals: %s", e)


async def _decide_approval(approval_id: int, approve: bool, how: str) -> str:
    """A tap on an approval request's button (Discord)."""
    return await (approval_flow.approve(approval_id, how) if approve
                  else approval_flow.deny(approval_id, how))


def _log_expensive_turn(run_id, text: str) -> None:
    """H1: a turn over llm_calls.EXPENSIVE_TURN_USD is logged here and named in the next
    morning brief. Replaces the old stance of no per-turn ceiling at all."""
    try:
        from backend.storage import llm_calls as _llm_calls
        cost = _llm_calls.run_cost(run_id)
        if cost > _llm_calls.EXPENSIVE_TURN_USD:
            log.warning("Expensive turn: $%.3f for %r", cost, (text or "")[:80])
    except Exception as e:
        log.debug("expensive-turn check skipped: %s", e)


async def run_pipeline_from_audio(audio_b64: str):
    """Transcribe browser-recorded audio then run the model → TTS."""
    global _current_pipeline_task
    async with _pipeline_lock:
        _current_pipeline_task = asyncio.current_task()  # interruptible during STT too
        await send_state("thinking")

        audio_bytes = base64.b64decode(audio_b64)
        tmp = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
        try:
            tmp.write(audio_bytes)
            tmp.close()
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, transcribe_file, tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        log.info("Transcribed: %s", text)
        await _execute_turn(text)


async def run_pipeline_from_text(text: str) -> None:
    """Run the model → TTS pipeline from a typed text message (no STT)."""
    async with _pipeline_lock:
        log.info("Text input: %s", text)
        await _execute_turn(text)


async def run_pipeline_from_discord(text: str) -> str:
    """Run one turn for a Discord-originated message and return the reply text.

    Unlike the WS entry points, this normally *waits* for the lock instead of
    dropping the message when busy -- a text you sent from your phone is
    precious; a duplicate wake-word trigger during an in-flight turn is not.
    The bot sends a "typing…" indicator while this is in flight, so the wait
    is visible rather than looking like the message vanished. No TTS
    (want_tts=False).

    Step 1a (2026-09-08) exception: if the pipeline is ALREADY busy, this no
    longer waits to run as a second full turn behind whatever's in flight --
    it steers the running turn instead (whichever channel that turn is on;
    see `_chat_compat`'s mid-loop drain) and returns a quick
    ack immediately. The turn that's actually running still replies through
    its own original caller's `_turn_handler` await, exactly as before --
    this early return only affects the message THAT triggered it.
    """
    if _pipeline_lock.locked() or steering.pending_turns():
        ack = _queue_discord(text)
        if ack is not None:
            return ack
        # Could not be stored: no ack (it would be a promise nothing backs). The message
        # waits for the lock and is answered as its own turn, as before Step 1a.
    async with _pipeline_lock:
        log.info("Discord input: %s", text)
        response = await _execute_turn(text, channel="discord", want_tts=False)
    return await _shape_discord(response, text) or "…"


def _queue_discord(text: str) -> str | None:
    """G3: store a Discord message that cannot run now, then say what will happen to it.
    None when it could not be stored. It joins the running turn only when that turn is in
    the same conversation; otherwise it is answered on its own when JARVIS is free."""
    conv, key, context = _turn_identity("discord")
    same = _pipeline_lock.locked() and _running.get("conversation") == conv
    target = _running.get("run_id") if same else None
    seq = steering.push("discord", text, key=key, conversation=conv, target_run=target,
                        context=context, reply_to=context.get("target_id") or None)
    if not seq:
        return None
    _admission.set()
    log.info("Discord input queued (%s, %s)", conv, "steers the running turn" if target else "own turn")
    spawn(broadcast({"type": "steering_queued", "channel": "discord", "text": text}))
    if target:
        return "Got it — I'll factor that in."
    return "Got it — I'll answer this as soon as I've finished what I'm doing."


async def _shape_discord(response: str | None, text: str) -> str | None:
    """The reply as it goes to Discord: long answers to shared/ (H4) and the skills line."""
    if not response:
        return response
    # H4: past long_reply.LONG_REPLY_CHARS the full answer goes to shared/ and the
    # chat gets a summary. Outside the lock: the next message need not wait on it.
    from backend.brain import long_reply
    response = await long_reply.shape_for_discord(response, text)
    # A skill saved or changed in that turn: the code-written line naming it and the undo.
    from backend.brain import skills as _skills_line
    if response:
        response += _skills_line.pop_footer("discord")
    return response


def discord_new_session() -> str:
    """Handles the Discord "/new" command: clear this channel's short-term
    history and force the next message to start a fresh session, bypassing
    the normal 2h gap check (see engine.force_new_session)."""
    conversation_histories["discord"] = []
    history_bridges["discord"] = []
    history_times["discord"] = []      # stays parallel to the history it stamps
    history_refill.mark_new("discord")  # a restart must not bring this conversation back
    force_new_session("discord")
    # A correction queued for the conversation being abandoned must not
    # silently steer the fresh one that starts next -- nor be dropped silently.
    waiting = sorted({r["conversation_id"] for r in steering.pending_turns()
                      if r["conversation_id"].startswith("discord")})
    dropped = steering.clear("discord", waiting) if waiting else 0
    from backend.brain import attachments
    attachments.clear("discord")     # nothing from the old conversation stays in view
    if dropped:
        return (f"New conversation — go ahead. ({dropped} earlier message(s) I hadn't "
                "answered yet were dropped with the old one.)")
    return "New conversation — go ahead."


def mobile_new_session() -> str:
    """The phone's "New chat": the same boundary as Discord's /new, on channel "mobile".
    Messages still waiting for the old conversation are cancelled, and he is told how many."""
    ch = mobile_conversation.CHANNEL
    conversation_histories[ch] = []
    history_bridges[ch] = []
    history_times[ch] = []
    history_refill.mark_new(ch)
    force_new_session(ch)
    waiting = sorted({r["conversation_id"] for r in steering.pending_turns()
                      if r["conversation_id"] == mobile_conversation.CONVERSATION})
    dropped = steering.clear(ch, waiting) if waiting else 0
    from backend.brain import attachments
    attachments.clear(ch)
    if dropped:
        return f"New chat. ({dropped} earlier message(s) I hadn't answered yet were dropped with the old one.)"
    return "New chat."


mobile_conversation.configure(admit=lambda: _admission.set(), running=lambda: dict(_running),
                              new_chat=mobile_new_session, stop=stop_turn)


async def run_pipeline():
    """Full listen → transcribe → model → speak pipeline (wake-word / keyboard path)."""
    # Authoritative drop-if-busy guard. .locked() and the acquire below run with
    # no await between them, and acquire() returns synchronously when unlocked —
    # so this is atomic on the single-threaded loop and cannot double-trigger.
    global _current_pipeline_task
    if _pipeline_lock.locked():
        return
    async with _pipeline_lock:
        _current_pipeline_task = asyncio.current_task()  # interruptible during listening
        await send_state("listening")
        text = await get_stt().listen_and_transcribe()
        log.info("Heard: %s", text)
        await _execute_turn(text)


async def on_wake():
    # run_pipeline drops itself if the lock is already held (atomic guard inside),
    # so a second wake trigger during processing is safely ignored.
    spawn(run_pipeline())


async def text_input_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, input, "\nYou> ")
        except EOFError:
            break
        if not text.strip():
            continue
        answer = await approval_flow.handle_reply(text)
        if answer is not None:                       # P1: an answer to an approval request
            print(f"\nJARVIS> {answer}")
            continue
        if _pipeline_lock.locked():
            print("(busy — previous request still running)")
            continue
        try:
            async with _pipeline_lock:
                response = await _execute_turn(text)
            if response:
                print(f"\nJARVIS> {response}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let one bad turn kill the input loop for the whole process.
            log.error("Terminal turn failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_startup_mode()
    # A01: fail before accepting any work if the provider has no credential.
    # Previously the first real message discovered this, after startup had already
    # reported healthy.
    gateway.require_configuration(_provider())
    # A02: anything still marked `running` belongs to a process that no longer exists.
    # Mark it interrupted BEFORE admitting new work, so the record is accurate and a
    # human can see what was cut off. Deliberately never auto-retried.
    #
    # A06: read the open runs FIRST, then reconcile, then surface the autonomous ones.
    # Marking alone left a background task killed by a restart recorded in SQLite and
    # never mentioned again -- the owner asked for work that silently never finished.
    _interrupted = _runs.open_runs(background._AUTONOMOUS_CHANNELS)
    # H4: a Discord turn cut off by a restart. Its message was already claimed, so the
    # gateway will not redeliver it; without a notice the owner just never hears back.
    _cut_off_discord = [r for r in _runs.open_runs(["discord"])
                        if time.time() - float(r.get("started_at") or 0) < 6 * 3600]
    _runs.reconcile_interrupted()
    # G3: a message still claimed belonged to a turn the dead process was running. It is
    # marked interrupted and named in the cut-off notice; pending ones are answered once
    # the admission loop starts.
    _cut_off_messages = [r for r in steering.recover()
                         if r["conversation_id"].startswith("discord")]
    # P1: an approved request cut off mid-run is `uncertain` from here on, never retried.
    _interrupted_approvals = approval_flow.reconcile()
    background.recover_durable_jobs()
    if _interrupted:
        background.recover_interrupted(_interrupted)
    # G0 / D7: a night task the previous process died running goes into the next brief
    # with its full text. Before the chronos loop starts, so a fresh run is never mistaken
    # for one. Not retried.
    try:
        chronos.recover_night_task()
    except Exception as e:
        log.warning("Night-task recovery skipped: %s", e)
    # G1a: take the goal coordinator's lock and mark sessions the previous process was
    # running as interrupted (their charges held), before the goal loop starts.
    try:
        _goal_recovered = goal_runner.startup()
        if _goal_recovered:
            log.info("Goals: %d interrupted session(s) will be redone", len(_goal_recovered))
    except Exception as e:
        log.warning("Goal recovery skipped: %s", e)
    # The conversation a restart interrupted comes back before any message is admitted.
    for _channel in ("discord", "local", mobile_conversation.CHANNEL):
        _refill_history(_channel)
    # Warm Mem0 in background — sentence-transformers + spaCy + BM25 take ~15s cold
    warm_mem()
    # Load mood state from Qdrant in background — applies time-gap decay
    warm_mood()
    # Build self-model digest in background — sliced from CLAUDE.md (hash-keyed cache)
    warm_self_model()
    # Load the shared semantic embedder (gte-small) in background
    warm_embedder()
    # Passive-recall reranker (cross-encoder, local cache only) -- loaded in the
    # background so the first turn doesn't pay ~14s. Absent model = cosine-only recall.
    rerank.warm()
    # Build the intent-gate route matrix in background -- shares the embedder
    # load lock with warm_embedder() above, so this safely waits its turn
    # rather than racing it. Seeds-only on first build; upgrades itself once
    # the self-model digest (warmed just above) becomes ready.
    warm_intent()
    # Jev's pooled connection, so the first turn's classification is not the cold one.
    spawn(turn_signals.warm())
    # Index any not-yet-indexed vault files into the semantic collection
    spawn(semindex.backfill())
    # A02 (deferred read-path work): copy any exchange the canonical SQLite store is
    # missing out of the Qdrant thread history. A02 mirrored every NEW exchange, but an
    # existing install has none of its past there -- measured 0 rows against 345
    # threads -- so nothing historical could be served from the index until this runs.
    # Insert-if-absent and idempotent; a live-recorded row is never replaced by a
    # poorer backfilled one.
    spawn(_threads.backfill_canonical())
    # Step 2 (2026-09-08) -- seed the jarvis_semantic point-count cache
    # runtime_status.py reads (see semindex.warm_point_count()'s own comment
    # for why this can't just be a live query from that fully-synchronous,
    # no-network function).
    spawn(semindex.warm_point_count())
    # Background tasks notify the UI through the server's broadcast (injected
    # here — background.py must not import this module)
    background.set_broadcaster(broadcast)
    background.start_queued_jobs()

    # Discord bridge: same dependency-injection pattern as the broadcaster
    # above -- discord_bridge.py must never import this module. No-ops
    # silently (logs once, does nothing else) if DISCORD_BOT_TOKEN isn't set.
    discord_bridge.configure(
        turn_handler        = run_pipeline_from_discord,
        new_session_handler = discord_new_session,
        stop_handler        = stop_turn,
        transcribe_handler  = transcribe_file,
        pace_handler        = research.handle_pace_command,
        status_handler      = runtime_status.runtime_status_text,
        cost_handler        = usage.cost_report,
        brief_handler       = chronos.handle_brief_command,
        brief_now_handler   = chronos.brief_now,
        pitch_handler       = pitch.get_pitch_text,
        rules_handler       = instructions.handle_rules_command,
        record_handler      = forecasts.record_text,
        pause_handler       = pause.handle_pause_command,
        resume_handler      = pause.handle_resume_command,
        activity_handler    = activity.activity_text,
        plan_handler        = planday.handle_plan_command,
        goals_handler       = goal_tool.handle_command,
        goal_control_handler = goal_tool.handle_control,
        portfolio_handler   = portfolio.handle_command,
        trace_handler       = lambda: tool_record.trace_text("discord"),
        skills_handler      = skills.handle_skills_command,
        skill_export_handler = skills.export_file,
        skill_undo_handler  = skills.handle_undo_command,
        approval_decider    = _decide_approval,
        approval_reply_handler = approval_flow.handle_reply,
    )
    # The phone's /new and /trace act on the phone's own conversation, not Discord's.
    from backend.brain import commands as _commands
    _commands.set_channel_handler("/new", "mobile", mobile_new_session)
    _commands.set_channel_handler("/trace", "mobile", lambda: tool_record.trace_text("mobile"))
    # P1: approval requests go out with Approve / Deny buttons, and what became of them is
    # written back onto the same message.
    approval_flow.set_notifier(discord_bridge.send_approval_request)
    approval_flow.set_resolution_notifier(discord_bridge.update_approval_message)
    # G1a: a goal's messages go through keyed delivery, so a restart never sends one twice.
    goal_runner.set_notifier(notices.deliver_keyed("goal", discord_bridge.send_keyed_to_owner))
    goal_runner.set_card_functions(discord_bridge.post_card, discord_bridge.edit_card)
    # ...and an email he DID ask for gets a second look before it goes: the recipient and text
    # against what he asked (measured: scripts/measure_outward_check.py). If the check cannot
    # run, the email is put to him instead of sent.
    _tool_gateway.set_outward_reviewer(outward_check.review)
    _discord_start = asyncio.create_task(discord_bridge.start())
    if _cut_off_discord or _cut_off_messages:
        spawn(_announce_cut_off_turns(_discord_start, _cut_off_discord,
                                                    _cut_off_messages))
    if _interrupted_approvals:
        spawn(_announce_interrupted_approvals(_discord_start, _interrupted_approvals))

    # Plan-and-Execute: when a Discord turn produces a structured plan, DM
    # the step list once up front (no per-step chatter) -- same notifier
    # function research.py's notify path already uses.
    set_plan_notifier(discord_bridge.send_to_owner)

    # Step 1b (2026-09-08): the clarifying-question tool's DM, same
    # send_to_owner function as every other notifier above -- own named hook
    # rather than reusing set_plan_notifier, matching how research/backup_loop/
    # canary/chronos each get their own hook onto the same underlying function.
    set_clarify_notifier(discord_bridge.send_to_owner)
    # H4: vault_send delivers a note's exact text through the bridge.
    from backend.tools import verbatim as _verbatim
    _verbatim.set_sender(discord_bridge.send_verbatim)
    # A skill exported with the tool goes out as a file, the same way.
    skills.set_file_sender(discord_bridge.send_file)
    # H4: progress lines for long Discord turns read the running turn's state.
    from backend.brain.engine import progress_state as _progress_state
    discord_bridge.set_progress_handler(lambda since: _progress_state("discord", since))

    # Five paced background loops share two DI values (the Discord notifier,
    # the pipeline busy-check) wired here in bulk instead of by hand per
    # module -- see bootstrap.py for why. rumination deliberately gets no
    # notifier: delivery is next-turn only, never a Discord DM (see
    # backend/brain/rumination.py).
    # Phone slice 4: every message JARVIS sends on his own goes through notices.deliver, which
    # keeps it for the app's Inbox and sends it to Discord, the phone or both (his setting).
    # The kind is the module's; chronos and the planner label reminders, the brief, check-ins
    # and reviews themselves (notices.labelled).
    for _module, _kind in (
            (backup_loop, "system"), (canary, "system"),
            (chronos, "check"),        # reminders and the brief are labelled inside chronos
            (watches, "watch"), (forecasts, "forecast"), (people_research, "profile"),
            (portfolio, "portfolio"),
            (background, "job"),       # H4: a handed-off turn DMs its result
            (planloop, "plan"),        # P4: the evening check-in, clash notices and the weekly review
            (pause, "pause")):         # P2: a timed pause ending on its own is announced
        _module.set_notifier(notices.deliver(_kind, discord_bridge.send_to_owner))
    # Research is a substantive conversational message: retain its Discord
    # identity so a short follow-up can refer to it even without a reply click.
    research.set_notifier(notices.deliver("research", discord_bridge.send_research_to_owner))
    bootstrap.wire_busy_check(
        [research, chronos, rumination, watches, forecasts, session_close, skill_nightly],
        _pipeline_lock.locked,
    )

    # Autonomous research ("dreaming", paced by /pace), periodic backup
    # safety net, the memory canary self-check, chronos (reminders/night
    # tasks/morning brief), rumination (l'esprit de l'escalier), and the
    # self-model's live CLAUDE.md re-check (see self_model.py's "Live
    # reload" section — without this, a backend left running through a dev
    # session serves a self-model frozen at whatever CLAUDE.md looked like
    # at launch) — one task per loop, "research" first so it's also
    # cancelled first at shutdown (see the _loop_tasks comment above).
    global _loop_tasks
    _loop_tasks = bootstrap.start_loops({
        "research":   research.run_loop,
        "backup":     backup_loop.run_loop,
        "canary":     canary.run_loop,
        "chronos":    chronos.run_loop,
        "rumination": rumination.run_loop,
        "self_model": self_model.run_loop,
        "evolution":  evolution.run_loop,
        "reconcile":  semindex.run_reconcile_loop,
        "watches":    watches.run_loop,
        "forecasts":  forecasts.run_loop,
        # P1: unanswered approval requests expire after 24 hours and never run late.
        "approvals":  approval_flow.run_loop,
        # P2: a timed /pause ends on its own and says so (the gate itself opens on time).
        "pause":      pause.run_loop,
        # P4: the evening check-in, the 30-minute look for clashes, the Sunday review.
        "planloop":   planloop.run_loop,
        # Consolidates a session ~2h after its last message rather than at the next one.
        "session_close": session_close.run_loop,
        # Daily closes, splits and thesis conditions. Its own loop: a slow price feed
        # used to hold up chronos (and so reminders) for up to a minute.
        "portfolio":  portfolio.run_loop,
        # Skills' overnight pass: repeats become skills, duplicates merge, unused retire.
        "skill_nightly": skill_nightly.run_loop,
        # G1a: goals, one bounded session at a time (does nothing unless JARVIS_GOALS=1).
        "goals":      goal_runner.run_loop,
        # G3: messages that waited in the inbox, each answered as its own turn when idle.
        "admission":  _admission_loop,
    })
    _admission.set()

    global _wake_status
    loop = asyncio.get_event_loop()
    if _no_wake:
        _wake_status = {"type": "wake_status", "active": False, "reason": "text input mode (-NoWake)"}
        spawn(text_input_loop())
    else:
        try:
            # Imported lazily -- openwakeword (and its tflite-runtime dependency) is a
            # desk/Windows-only feature with no available wheel for Python 3.12 on Linux
            # (see requirements.txt). A headless --no-wake deployment must never need it
            # installed at all; a top-level import here would break that regardless of the
            # --no-wake flag, since imports happen before argparse ever runs.
            from backend.audio.wake import WakeWordDetector
            detector = WakeWordDetector(on_wake=on_wake)
            detector.start(loop)
            log.info("Wake word detector active.")
            _wake_status = {"type": "wake_status", "active": True}
        except Exception as e:
            log.warning(
                "Wake word detector failed to start (%s). "
                "Running in button-only mode — use the mic button in the UI.",
                e,
            )
            _wake_status = {"type": "wake_status", "active": False, "reason": str(e)}
        await broadcast(_wake_status)
    await send_state("idle")
    log.info("JARVIS ready — listening for WebSocket connections on port 8765.")
    yield
    log.info("JARVIS going to sleep — flushing session …")
    # 0. stop the background loops, research first — it may be mid-tick
    # trying to message over the very Discord connection about to be torn
    # down (see the _loop_tasks comment above for why order matters here).
    bootstrap.cancel_all(_loop_tasks)
    try:
        await goal_runner.shutdown()   # a running goal session is interrupted; its charge is held
    except Exception as e:
        log.warning("Goal runner stop failed: %s", e)
    try:
        await discord_bridge.stop()    # 1. disconnect before flushing so no new turn starts mid-shutdown
    except Exception as e:
        log.warning("Discord bridge stop failed: %s", e)
    try:
        await flush_session()          # 2. finish last thread write(s) + generate checkpoint/letter(s)
    except Exception as e:
        log.warning("Session flush failed: %s", e)
    try:
        await record_sleep()           # 3. write sleep marker (timestamp + mood)
    except Exception as e:
        log.warning("Sleep marker write failed: %s", e)
    try:
        await gateway.close_all()      # 4. release pooled provider connections
    except Exception as e:
        log.warning("Provider client shutdown failed: %s", e)
    try:
        # 5. drop queued indexing rather than hold the process open for it; the next
        # startup's hash-aware reconcile re-indexes anything that did not land.
        from backend.brain import embeddings as _embeddings
        _embeddings.shutdown_derived()
        from backend.storage import qdrant as _qdrant_store
        _qdrant_store.close_all()      # release the pooled Qdrant connections
    except Exception as e:
        log.warning("Derived-work executor shutdown failed: %s", e)
    try:
        _db.close_all()                # 6. release SQLite handles (all threads)
    except Exception as e:
        log.warning("Runtime database shutdown failed: %s", e)
    log.info("JARVIS shutting down.")


app = FastAPI(title="JARVIS Backend", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
# Phone app: /mobile/v1 (Tailscale serve + MOBILE_TOKEN; 404 without it). docs/archive/MOBILE-APP-PLAN.md
app.include_router(mobile_routes.router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin")
    if not _origin_allowed(origin):
        log.warning("WebSocket connection rejected — untrusted origin: %s", origin)
        await ws.close(code=1008)
        return
    await ws.accept()
    connected_clients.add(ws)
    log.info("Frontend connected. Total: %d", len(connected_clients))
    try:
        await ws.send_text(json.dumps({"type": "state", "value": "idle"}))
        await ws.send_text(json.dumps(_wake_status))
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif msg.get("type") == "audio_data":
                # Small yield so a just-released lock (post-interrupt) is seen correctly
                await asyncio.sleep(0)
                if not _pipeline_lock.locked():
                    audio_b64 = msg.get("data", "")
                    if audio_b64:
                        spawn(run_pipeline_from_audio(audio_b64))
                else:
                    log.warning("audio_data received but pipeline still locked — dropping")
            elif msg.get("type") == "text_input":
                text = msg.get("text", "").strip()
                # P1: "yes 12" / "no 12" answers an approval request, here as on Discord.
                answer = await approval_flow.handle_reply(text) if text else None
                if answer is not None:
                    await broadcast({"type": "transcript", "role": "assistant", "text": answer})
                elif text and not _pipeline_lock.locked():
                    spawn(run_pipeline_from_text(text))
                elif text:
                    # Step 1a (2026-09-08) -- used to be dropped outright with
                    # zero feedback. Typed input steers the running turn instead
                    # (voice/audio_data above deliberately does not -- see the
                    # upgrade plan: live-mic steering during TTS playback
                    # collides with the existing cooperative interrupt path).
                    same = _running.get("conversation") == "local"
                    if steering.push("local", text, conversation="local",
                                     target_run=_running.get("run_id") if same else None):
                        _admission.set()
                        await broadcast({"type": "steering_queued", "channel": "local", "text": text})
                        log.info("text_input received while busy — queued")
                    else:
                        spawn(run_pipeline_from_text(text))
            elif msg.get("type") == "start_listening":
                if not _pipeline_lock.locked():
                    spawn(run_pipeline())
            elif msg.get("type") == "interrupt":
                log.info("Interrupt received — cancelling pipeline")
                # Only interrupt a TTS engine that was actually built -- get_tts()
                # here would construct one just to stop it.
                # An answer being written is stopped on its own (stop_turn): cancelling the
                # task it runs in could be the phone's admission loop. Before an answer starts
                # (listening, transcribing), the desk's own pipeline task is cancelled.
                if not stop_turn() and _current_pipeline_task and not _current_pipeline_task.done():
                    _current_pipeline_task.cancel()
                await send_state("idle")
    except WebSocketDisconnect:
        log.info("Frontend disconnected.")
    finally:
        connected_clients.discard(ws)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/inspector")
async def inspector(request: Request):
    """Read-only snapshot of JARVIS's memory surfaces for the UI overlay (Alt+M).

    Returns the recent letters, the intent queue, unconsolidated mem0 facts,
    and the skills list. Every section degrades to empty on failure — the
    endpoint never errors. Origin-gated: this is private long-term memory, and
    a simple GET like this can be triggered by any page's JS regardless of
    CORS (CORS only stops the page from reading the response, not from firing
    the request), so the check has to happen server-side.
    """
    if not _origin_allowed(request.headers.get("origin")):
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    import pathlib
    vault = pathlib.Path(__file__).parent.parent / "jarvis-vault"
    out = {"letters": [], "queue": "", "facts": [], "skills": []}

    try:
        letters_dir = vault / "letters"
        if letters_dir.exists():
            files = sorted(
                (f for f in letters_dir.iterdir() if f.is_file()),
                key=lambda f: f.stat().st_mtime, reverse=True,
            )[:8]
            for f in files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    text = ""
                out["letters"].append({
                    "name": f.name,
                    "mtime": f.stat().st_mtime,
                    "preview": text[:400],
                })
    except Exception:
        pass

    try:
        queue_file = vault / "queue.md"
        if queue_file.exists():
            out["queue"] = queue_file.read_text(encoding="utf-8", errors="replace")[:4000]
    except Exception:
        pass

    try:
        from backend.brain.memory import _scroll_facts_sync
        loop = asyncio.get_event_loop()
        facts = await loop.run_in_executor(None, _scroll_facts_sync)
        out["facts"] = [t for _, t in facts][-30:]
    except Exception:
        pass

    try:
        skills_dir = vault / "skills"
        if skills_dir.exists():
            out["skills"] = sorted(f.stem for f in skills_dir.glob("*.md"))
    except Exception:
        pass

    return out


@app.post("/shutdown")
async def graceful_shutdown(request: Request):
    """Called by teardown.ps1 before the force-kill.

    Blocks until the active session is checkpointed and the sleep marker is
    written, then returns.  teardown.ps1 waits for this response before
    issuing taskkill, so the memory is always saved on a clean Ctrl+C.
    Origin-gated so a page open in the browser can't CSRF a shutdown — a plain
    POST like teardown.ps1's is a "simple request" that a browser will fire
    (and this handler will execute) even though CORS blocks the page from
    reading the response, so the Origin check has to happen here, server-side.
    """
    if not _origin_allowed(request.headers.get("origin")):
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    log.info("Shutdown endpoint called — flushing session …")
    try:
        await flush_session()
    except Exception as e:
        log.warning("Shutdown: session flush failed: %s", e)
    try:
        await record_sleep()
    except Exception as e:
        log.warning("Shutdown: sleep marker failed: %s", e)
    log.info("Shutdown: memory flushed — ready to be terminated.")
    return {"status": "flushed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-wake", action="store_true")
    # Loopback-only by default -- JARVIS holds Gmail send and vault access, so
    # binding 0.0.0.0 would let anyone on the LAN drive it over an
    # unauthenticated WebSocket. Override via JARVIS_HOST in .env if LAN access
    # is genuinely wanted (then add real auth in front of it).
    parser.add_argument("--host", default=os.getenv("JARVIS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    _no_wake = args.no_wake

    import uvicorn
    # Pass app object directly — avoids re-importing the module (which would create
    # a second TextToSpeech instance and double Kokoro warm-up)
    uvicorn.run(app, host=args.host, port=args.port, reload=False)
