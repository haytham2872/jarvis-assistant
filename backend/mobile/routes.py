"""HTTP and WebSocket routes for the phone app. Mounted on the backend under /mobile/v1.

The backend binds to 127.0.0.1, `tailscale serve`
publishes only the /mobile path to the owner's tailnet over HTTPS (DEPLOY.md), and every
request carries MOBILE_TOKEN as a bearer token, the WebSocket included. Without
MOBILE_TOKEN the routes do not exist (404), so a server that was never set up exposes
nothing.

Routes: ping; the conversation (messages, snapshot, chats, events); voice in (warm,
recordings); spoken answers (speech plan and clips); approvals and phone-started jobs; the
activity view with pause and resume; /ask, one request in and the answer out, for an
iOS Shortcut on the Action Button; Today and Portfolio (slices 2 and 3); and the Inbox, where
JARVIS's own messages go, push registration and what the phone schedules itself (slice 4). The M0 probe routes (echo socket, probe recording and
speech) were removed on 15 October 2026 with the probe screen.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import math
import os
import platform
import tempfile
import time

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from backend.mobile import speech

log = logging.getLogger(__name__)
router = APIRouter(prefix="/mobile/v1")

SCHEMA_VERSION = 1
MAX_RECORDING_BYTES = 12 * 1024 * 1024     # the plan's bound; 2 min of 32 kbit/s AAC is <1 MB
MAX_RECORDING_S = 120.0
_SUFFIXES = {"audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
             "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/mpeg": ".mp3"}


class TooLong(Exception):
    """The recording decoded fine but runs past MAX_RECORDING_S."""


def _refusal(authorization: str) -> int | None:
    """None when the bearer token is right, else the HTTP status to refuse with."""
    token = os.getenv("MOBILE_TOKEN", "").strip()
    if not token:
        return 404
    if not authorization.startswith("Bearer ") or not hmac.compare_digest(
            authorization[7:].encode(), token.encode()):
        return 401
    return None


def _authorize(request: Request) -> None:
    status = _refusal(request.headers.get("authorization", ""))
    if status == 404:
        raise HTTPException(status_code=404)
    if status:
        raise HTTPException(status_code=401, detail="mobile token required")


def _conditional(request: Request, data: dict) -> Response:
    """Private view validation, reusable by /hub when that slice adds it.

    Construction clocks are not view changes. A weak validator compares the meaningful
    JSON while preserving those clocks in a full response. Domain timestamps stay in it.
    """
    stable = {key: value for key, value in data.items() if key not in {"as_of", "now"}}
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    etag = f'W/"{digest}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization, X-Push-Token"}
    matches = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
    if "*" in matches or etag.removeprefix("W/") in matches:
        return Response(status_code=304, headers=headers)
    return JSONResponse(data, headers=headers)


@router.get("/ping")
async def ping(request: Request) -> dict:
    _authorize(request)
    from backend.brain.version import VERSION
    return {"schema_version": SCHEMA_VERSION, "server_time": time.time(),
            "host": platform.system(), "version": VERSION}


async def _read_bounded(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"recording over {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail=f"recording over {limit} bytes")
    return bytes(body)


def _inspect(path: str, transcribe: bool) -> dict:
    """Container facts, decoded length and (optionally) the transcript. Blocking."""
    import av

    from backend.audio import transcriber

    with av.open(path) as container:
        stream = container.streams.audio[0]
        facts = {"container": container.format.name, "codec": stream.codec_context.name,
                 "sample_rate": stream.codec_context.sample_rate,
                 "channels": stream.codec_context.channels}
    t0 = time.monotonic()
    pcm = transcriber.decode(path)
    facts["decode_ms"] = round((time.monotonic() - t0) * 1000)
    facts["duration_s"] = round(pcm.size / transcriber.SAMPLE_RATE, 2)
    if facts["duration_s"] > MAX_RECORDING_S:
        raise TooLong(f"recording longer than {MAX_RECORDING_S:.0f}s")
    if transcribe:
        was_loaded = bool(transcriber.engine())
        t0 = time.monotonic()
        facts["transcript"] = transcriber.transcribe(pcm)
        facts["transcribe_ms"] = round((time.monotonic() - t0) * 1000)
        facts["stt_engine"] = transcriber.engine()
        facts["stt_was_loaded"] = was_loaded
    return facts


# ------------------------------------------------------------------ M1: the conversation

MAX_MESSAGE_BODY = 64_000


def _notice_id(value) -> int | None:
    """An Inbox message being answered; anything that is not a positive id is ignored."""
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@router.post("/messages", status_code=202)
async def post_message(request: Request) -> dict:
    """{request_id, text, input?, uploads?}: stored before it is acknowledged; a retry with the
    same request id returns the original's state instead of running it again. `uploads` (slice
    7): ids from POST /uploads; the files are read like Discord attachments and the message
    names them. "problems" lists any file that could not be read."""
    _authorize(request)
    from backend.mobile import conversation
    raw = await _read_bounded(request, MAX_MESSAGE_BODY)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    request_id, text = str(body.get("request_id", "")), str(body.get("text", ""))
    upload_ids = body.get("uploads") or []
    if not isinstance(upload_ids, list):
        raise HTTPException(status_code=400, detail="uploads must be a list of ids")
    problems: list[str] = []
    if upload_ids:
        if not conversation.valid_request_id(request_id):
            raise HTTPException(status_code=400, detail="request_id must be 8-64 letters, digits, '-' or '_'")
        known = conversation.status(request_id)
        if known is not None:                    # a retry: the files were read the first time
            return {**known, "problems": []}
        text, problems = await _attach(text, upload_ids)
    try:
        status = conversation.submit(request_id, text, str(body.get("input", "text")),
                                     reply_to=_notice_id(body.get("reply_to")))
    except conversation.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    if upload_ids:
        from backend.mobile import uploads
        uploads.drop(upload_ids)                 # only once the message is stored
    return {**status, "problems": problems}


@router.post("/stop")
async def stop_answer(request: Request) -> dict:
    """The Stop button: stops the answer JARVIS is writing now. The run ends as interrupted,
    which the phone already shows. {"stopped": false} when nothing was being written."""
    _authorize(request)
    from backend.mobile import conversation
    return {"stopped": conversation.stop()}


async def _attach(text: str, upload_ids: list) -> tuple[str, list[str]]:
    """His words plus the line naming what he sent. 410 when a file is gone."""
    from backend.mobile import conversation, uploads
    try:
        files = uploads.take(upload_ids)
    except uploads.Refused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return await _read_files(text, files, conversation.CHANNEL)


async def _read_files(text: str, files: list, channel: str) -> tuple[str, list[str]]:
    """Read the files (attachments.receive, as for Discord) and add the line naming them to his
    words. 422 when nothing at all is left to send."""
    from backend.brain import attachments
    received = await attachments.receive(files, channel)
    problems = [f"{r.name} ({r.error})" for r in received if r.error]
    line = attachments.marker(received)
    text = (text or "").strip()
    combined = f"{text}\n\n{line}".strip() if line else text
    if not combined:
        raise HTTPException(status_code=422, detail="Couldn't read " + "; ".join(problems or ["the file"]))
    return combined, problems


@router.post("/uploads", status_code=201)
async def post_upload(request: Request, name: str = Query("", max_length=200)) -> dict:
    """One file for a message he is about to send (slice 7): the raw bytes as the body, its
    type in Content-Type. Refused at once when it can't be read (415), is too big (413) or
    empty (400), so the phone says so before anything is sent."""
    _authorize(request)
    from backend.mobile import uploads
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    data = await _read_bounded(request, uploads.MAX_UPLOAD_BYTES)
    try:
        up = uploads.put(name, content_type, data)
    except uploads.Refused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return {"upload_id": up.upload_id, "name": up.name, "kind": up.kind, "bytes": up.size}


@router.get("/requests/{request_id}")
async def get_request(request: Request, request_id: str) -> dict:
    """Resolve an uncertain send: did the server get it, and what became of it?"""
    _authorize(request)
    from backend.mobile import conversation
    found = conversation.status(request_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such request")
    return found


@router.get("/snapshot")
async def get_snapshot(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    _authorize(request)
    from backend.mobile import conversation
    return conversation.snapshot(limit)


@router.get("/chats")
async def list_chats(request: Request, limit: int = Query(30, ge=1, le=100)) -> dict:
    _authorize(request)
    from backend.mobile import conversation
    return {"chats": conversation.chats(limit)}


@router.get("/chats/{session_id}")
async def get_chat(request: Request, session_id: str) -> dict:
    _authorize(request)
    from backend.mobile import conversation
    return {"session_id": session_id, "exchanges": conversation.chat_history(session_id)}


@router.post("/chats")
async def start_chat(request: Request) -> dict:
    """New chat: closes the phone's current session as /new does on Discord."""
    _authorize(request)
    from backend.mobile import conversation
    try:
        return {"message": await conversation.new_chat()}
    except conversation.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


@router.websocket("/events")
async def events(ws: WebSocket, epoch: str = "", after: int = 0) -> None:
    """Live events after (epoch, after). A phone whose position is from another process or
    has left the buffer gets {"type": "resync"} and fetches /snapshot."""
    status = _refusal(ws.headers.get("authorization", ""))
    if status:
        await ws.close(code=4404 if status == 404 else 4401)
        return
    from backend.mobile.events import HUB
    await ws.accept()
    queue = HUB.subscribe()
    try:
        missed = HUB.since(epoch, after) if epoch else None
        if missed is None:
            last = HUB.seq
            await ws.send_text(json.dumps({"type": "resync", "epoch": HUB.epoch, "seq": last}))
        else:
            last = missed[-1]["seq"] if missed else after
            for evt in missed:
                await ws.send_text(json.dumps(evt))
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                if not HUB.is_subscribed(queue):
                    await ws.send_text(json.dumps({"type": "resync", "epoch": HUB.epoch, "seq": HUB.seq}))
                    return
                await ws.send_text(json.dumps({"type": "keepalive", "epoch": HUB.epoch, "seq": HUB.seq}))
                continue
            if evt["seq"] > last:          # never twice: the catch-up may already hold it
                await ws.send_text(json.dumps(evt))
                last = evt["seq"]
    except WebSocketDisconnect:
        pass
    finally:
        HUB.unsubscribe(queue)


# ------------------------------------------------------------------ M2: voice in

_warming = False


@router.post("/warm")
async def warm(request: Request) -> dict:
    """The phone started recording: load Parakeet now, so the upload finds it ready (on the
    server a cold load is ~11 s, measured 14 October). Returns at once."""
    _authorize(request)
    from backend.audio import transcriber
    global _warming
    loaded = bool(transcriber.engine())
    if not loaded and not _warming:
        _warming = True

        def run() -> None:
            global _warming
            try:
                transcriber.warm()
            finally:
                _warming = False
        asyncio.get_running_loop().run_in_executor(None, run)
    elif loaded:
        asyncio.get_running_loop().run_in_executor(None, transcriber.warm)   # restart the idle clock
    return {"loaded": loaded}


async def _transcribe_body(request: Request) -> tuple[str, dict]:
    """The request body as a recording: decoded, bounded, transcribed. (transcript, stt facts).
    Empty 400, too large or too long 413, undecodable 415, nothing heard 422."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    body = await _read_bounded(request, MAX_RECORDING_BYTES)
    if not body:
        raise HTTPException(status_code=400, detail="empty recording")
    fd, path = tempfile.mkstemp(suffix=_SUFFIXES.get(content_type, ".bin"), prefix="jarvis-voice-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        try:
            facts = await asyncio.get_running_loop().run_in_executor(None, _inspect, path, True)
        except TooLong as exc:      # not ValueError: PyAV's InvalidDataError is one
            raise HTTPException(status_code=413, detail=str(exc)) from None
        except Exception as exc:
            log.info("mobile: undecodable voice message (%s, %d bytes): %s", content_type, len(body), exc)
            raise HTTPException(status_code=415, detail=f"could not decode: {exc}") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    transcript = (facts.get("transcript") or "").strip()
    stt = {k: facts.get(k) for k in ("duration_s", "transcribe_ms", "stt_engine", "stt_was_loaded")}
    log.info("mobile: voice message %s", {**stt, "bytes": len(body), "chars": len(transcript)})
    if not transcript:
        raise HTTPException(status_code=422, detail="I heard nothing in that recording")
    return transcript, stt


@router.post("/recordings", status_code=202)
async def post_recording(request: Request, request_id: str = Query(...),
                         spoken: bool = Query(False), reply_to: int | None = Query(None)) -> dict:
    """A voice message: the raw recording as the body. Transcribed, then submitted like a typed
    message under the same request id, so a retry after a lost acknowledgement returns the
    original (nothing transcribed or run twice). Nothing heard: 422, and no turn."""
    _authorize(request)
    from backend.mobile import conversation
    if not conversation.valid_request_id(request_id):
        raise HTTPException(status_code=400, detail="request_id must be 8-64 letters, digits, '-' or '_'")
    known = conversation.status(request_id)
    if known is not None:
        return {"transcript": known.get("text"), "status": known, "stt": None}
    transcript, stt = await _transcribe_body(request)
    try:
        status = conversation.submit(request_id, transcript, "voice", spoken=spoken,
                                     reply_to=_notice_id(reply_to))
    except conversation.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return {"transcript": transcript, "status": status, "stt": stt}


# ------------------------------------------------------------------ M3: spoken answers

@router.get("/speech/plan")
async def speech_plan(request: Request, source: str = Query(...)) -> dict:
    """How the final answer `source` names is spoken: its clips, each with its text and
    language (the text is also what the phone's own voice reads if the server's fails)."""
    _authorize(request)
    from backend.mobile import conversation, spoken
    try:
        answer = conversation.spoken_answer(source)
    except conversation.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return await spoken.plan(source, answer)


@router.get("/speech/clip")
async def speech_clip(request: Request, source: str = Query(...), revision: str = Query(...),
                      seq: int = Query(..., ge=0)) -> Response:
    """One clip of a plan as MP3. A revision that no longer matches the answer is refused
    (409); a used-up monthly allowance (503) or a failed voice (502) tells the phone to read
    the clip with its own voice."""
    _authorize(request)
    from backend.mobile import conversation, spoken
    try:
        answer = conversation.spoken_answer(source)
    except conversation.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    if revision != spoken.revision(answer):
        raise HTTPException(status_code=409, detail="the answer changed; ask for its plan again")
    clips = (await spoken.plan(source, answer))["clips"]
    if seq >= len(clips):
        raise HTTPException(status_code=404, detail="no such clip")
    t0 = time.monotonic()
    try:
        audio, engine = await spoken.clip_audio(revision, clips[seq])
    except spoken.OverAllowance as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except speech.SpeechError as exc:
        log.warning("mobile speech: clip %s/%d failed: %s", source, seq, exc)
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"X-Engine": engine, "X-Synth-Ms": str(int((time.monotonic() - t0) * 1000)),
                             "Cache-Control": "private, max-age=1800"})


# ------------------------------------------------------------------ M4: approvals and jobs

@router.get("/approvals")
async def get_approvals(request: Request) -> dict:
    """Approval requests waiting on the owner, and the ones settled in the last day."""
    _authorize(request)
    from backend.mobile import conversation
    return conversation.approvals_view()


@router.post("/approvals/{approval_id}")
async def decide_approval(request: Request, approval_id: int) -> dict:
    """{"decision": "approve" | "deny"}: the same decision as a Discord tap, at most once."""
    _authorize(request)
    from backend.mobile import conversation
    try:
        body = await request.json()
    except Exception:
        body = None
    decision = body.get("decision") if isinstance(body, dict) else None
    if decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail='decision must be "approve" or "deny"')
    return await conversation.decide(approval_id, decision == "approve")


@router.get("/jobs")
async def get_jobs(request: Request) -> dict:
    """Background work started from the phone in the last three days."""
    _authorize(request)
    from backend.mobile import conversation
    return {"jobs": conversation.phone_jobs()}


# ------------------------------------------------------------------ slice 1: activity, pause, /ask

@router.get("/activity")
async def get_activity(request: Request) -> Response:
    """What JARVIS is running, what waits on the owner, what is scheduled and what he did in the
    last day, as the sections of /activity (backend/brain/activity.py), plus the pause line."""
    _authorize(request)
    data = await asyncio.get_running_loop().run_in_executor(None, _activity_with_trace)
    return _conditional(request, data)


def _activity_with_trace() -> dict:
    """/activity, plus what JARVIS's tools did in the phone conversation (/trace on Discord):
    the receipts, so he can check any "the ledger refused it" himself. The Activity tab draws
    any section the server sends, so the phone needs no update for it."""
    from backend.brain import activity, tool_record
    view = activity.activity_view()
    since, found, cut = tool_record.trace("mobile")
    title = "What my tools did in our chat"
    if since is not None:
        title += " since " + time.strftime("%H:%M", time.localtime(since))
    section = {"title": title + (" (latest turns)" if cut else ""), "items": found,
               "empty": "no tool calls in our latest chat"}
    return {**view, "sections": [*view["sections"], section]}


def _json_object(body) -> dict:
    return body if isinstance(body, dict) else {}


@router.post("/pause")
async def pause_work(request: Request) -> dict:
    """{"hours": number | null, "all": bool}: the same as /pause on Discord. `all` also holds
    reminders and the morning brief. Answers with the confirmation and the view."""
    _authorize(request)
    from backend.brain import activity, pause
    try:
        body = _json_object(await request.json())
    except Exception:
        body = {}
    hours = body.get("hours")
    if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float))):
        raise HTTPException(status_code=400, detail="hours must be a number or null")
    arg = " ".join(([f"{float(hours):g}"] if hours is not None else []) + (["all"] if body.get("all") else []))
    text = pause.handle_pause_command(arg)
    return {"text": text, "activity": activity.activity_view()}


@router.post("/resume")
async def resume_work(request: Request) -> dict:
    """The same as /resume on Discord: ends the pause and lists what it skipped."""
    _authorize(request)
    from backend.brain import activity, pause
    text = pause.handle_resume_command("")
    return {"text": text, "activity": activity.activity_view()}


ASK_WAIT_S = 45.0          # an iOS Shortcut's request gives up at about 60 s
_ASK_POLL_S = 0.4
_ASK_DONE = {"answered", "failed", "interrupted", "cancelled", "absorbed", "unknown"}


@router.post("/ask")
async def ask(request: Request, wait: float = Query(ASK_WAIT_S, ge=0, le=55),
              q: str = Query("", max_length=4000), name: str = Query("", max_length=200)) -> dict:
    """For an iOS Shortcut (the Action Button): a message in, the answer out, in one request.

    The body is JSON {"text": "..."} (dictated), a raw recording (any audio type, transcribed
    by Parakeet like a voice message), or (slice 7, the share sheet) a photo or document named
    in `name`, with his words, if any, in `q`. It joins the phone chat under a fresh request id, so the
    exchange is in the app afterwards, and JARVIS is told the answer will be heard. Waits up to
    `wait` seconds. Always answers 200 with "say", the words for the Shortcut to speak, so the
    Shortcut needs no branches; "state" says what happened."""
    _authorize(request)
    import uuid

    from backend.mobile import conversation, spoken
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    kind = "text"
    if content_type == "application/json":
        try:
            body = _json_object(await request.json())
        except Exception:
            body = {}
        heard = str(body.get("text") or "").strip()
        if not heard:
            return {"state": "empty", "heard": "", "answer": "", "say": "I didn't get any words."}
    elif _is_shared_file(content_type, name):
        from backend.mobile import uploads
        data = await _read_bounded(request, uploads.MAX_UPLOAD_BYTES)
        fname = uploads.file_name("" if name == "shared" else name, content_type, data)
        shared_text = _shared_text(content_type, fname, data)
        try:
            if shared_text is not None:          # a link or a few lines: his message, not a document
                heard, _problems = f"{q or 'Look at this.'}\n\n{shared_text}".strip(), []
            else:
                uploads.check(fname, content_type, len(data))
                heard, _problems = await _read_files(q or "What is this?", [(fname, content_type, data)],
                                                     conversation.CHANNEL)
        except uploads.Refused as exc:
            return {"state": "unreadable", "heard": q, "answer": "", "say": f"I couldn't read that: {exc.detail}."}
        except HTTPException as exc:
            return {"state": "unreadable", "heard": q, "answer": "", "say": f"{exc.detail}."}
        if _problems:
            return {"state": "unreadable", "heard": q, "answer": "",
                    "say": "I couldn't read that: " + "; ".join(_problems) + "."}
    else:
        try:
            heard, _ = await _transcribe_body(request)
        except HTTPException as exc:
            if exc.status_code == 422:
                return {"state": "empty", "heard": "", "answer": "", "say": "I didn't hear anything."}
            raise
        kind = "voice"
    rid = f"ask-{uuid.uuid4().hex[:20]}"
    try:
        conversation.submit(rid, heard, kind, spoken=True)
    except conversation.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    deadline = time.monotonic() + wait
    status = conversation.status(rid) or {}
    while status.get("state") not in _ASK_DONE and time.monotonic() < deadline:
        await asyncio.sleep(_ASK_POLL_S)
        status = conversation.status(rid) or {}
    state = status.get("state", "unknown")
    answer = status.get("answer") or ""
    if state == "answered" and answer:
        say, _ = spoken.read_text(answer)
    elif state == "absorbed":
        say = "Added to what I'm already working on. The answer will be in the app."
    elif state in ("queued", "running"):
        say = "Still working on it. The answer will be in the app."
    else:
        say = "I couldn't answer that just now. It's in the app."
    log.info("mobile: /ask %s (%s, %d chars heard)", state, kind, len(heard))
    return {"state": state, "heard": heard, "answer": answer, "say": say, "request_id": rid}


SHARED_TEXT_MAX = 4000


def _shared_text(content_type: str, fname: str, data: bytes) -> str | None:
    """Text shared from the share sheet (a link from Safari, a few lines from Notes) is what he
    wants looked at, so it joins his message; a longer text file stays a document."""
    if not (content_type.startswith("text/") or fname.lower().endswith((".txt", ".url"))):
        return None
    if len(data) > SHARED_TEXT_MAX:
        return None
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return text or None


def _is_shared_file(content_type: str, name: str) -> bool:
    """A file from the share sheet rather than the Action Button's recording: named, or of a
    type that is not audio. An unnamed body of an unknown type is still read as a recording."""
    if content_type.startswith(("audio/", "video/")):
        return False
    if not name and content_type in ("", "application/octet-stream"):
        return False
    return True


# ------------------------------------------------------------------ slice 2: Today

@router.get("/today")
async def get_today(request: Request) -> Response:
    """the owner's day: planned blocks and their outcomes, yesterday's still open, the calendar,
    today's reminders and timed checks, and the week (backend/mobile/today.py)."""
    _authorize(request)
    from backend.mobile import today
    data = await asyncio.get_running_loop().run_in_executor(None, today.view)
    return _conditional(request, data)


@router.post("/today/blocks/{uid}")
async def tap_block(request: Request, uid: str) -> dict:
    """{"outcome": "done" | "partly" | "skipped" | "moved"}: what became of one block, the same
    record a check-in reply makes. A skip is offered a new slot (an approval request, or moved at
    once where he pre-approved it). 409 with the reason when the block cannot take it."""
    _authorize(request)
    from backend.brain import reshuffle, weekplan
    from backend.mobile import today
    try:
        body = _json_object(await request.json())
    except Exception:
        body = {}
    outcome = str(body.get("outcome") or "")
    loop = asyncio.get_running_loop()
    # Its own list: planday's shared one belongs to a chat turn's `week_plan report`, which drains
    # it, runs, then puts what it holds; a tap in between must neither take nor wipe that.
    proposals: list[dict] = []
    try:
        text = await loop.run_in_executor(None, today.tap, uid, outcome, None, proposals)
    except weekplan._Fail as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # The card already shows the outcome (its badge) and an offered slot (Approve / Deny); the note
    # is the rest: a move made at once where he pre-approved it, no free slot, a calendar that
    # could not be read, a request that could not be stored.
    note = text.split("\n")[1:]
    asked = []
    for p in proposals:                              # a skip's new slot, put to him for a tap
        row = await reshuffle.put_move(p)
        if row:
            asked.append(row["id"])
            text = f"{text}\n{p['label']}?"
        else:
            line = f"I couldn't put the new slot to you as a request ({p['label']})."
            text, note = f"{text}\n{line}", [*note, line]
    return {"text": text, "note": "\n".join(note), "asked": asked,
            "today": await loop.run_in_executor(None, today.view)}


# ------------------------------------------------------------------ slice 3: Portfolio

@router.get("/portfolio")
async def get_portfolio(request: Request) -> Response:
    """The ledger, read-only: positions with their exact figures, the theses and their
    conditions, a split waiting for his yes, the total and its history
    (backend/mobile/ledger.py). Deciding a split is the approval route."""
    _authorize(request)
    from backend.mobile import ledger
    data = await asyncio.get_running_loop().run_in_executor(None, ledger.view)
    return _conditional(request, data)


# ---------------------------------------------------------- next plan slice 3: the living portfolio
# /portfolio/history is declared before /portfolio/{symbol}, or "history" would read as a symbol.

async def _chart(call, *args):
    from backend.mobile import charts
    try:
        return await call(*args)
    except charts.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except charts.Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:                      # a bad range or level
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except charts.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/portfolio/history")
async def get_portfolio_history(request: Request, range: str = Query("1Y")) -> Response:   # noqa: A002
    """The total in the base currency over time, with what it cost (backend/mobile/charts.py)."""
    _authorize(request)
    from backend.mobile import charts
    return _conditional(request, await _chart(charts.total, range))


@router.get("/portfolio/{symbol}")
async def get_holding(request: Request, symbol: str, range: str = Query("1Y")) -> Response:   # noqa: A002
    """One stock's price with his trades, average cost, thesis levels and forecasts on it."""
    _authorize(request)
    from backend.mobile import charts
    return _conditional(request, await _chart(charts.holding, symbol, range))


@router.post("/portfolio/{symbol}/watch")
async def propose_watch(request: Request, symbol: str) -> dict:
    """A level he dragged to and confirmed: watch for a close below or above it."""
    _authorize(request)
    from backend.mobile import charts
    body = await _goal_body(request)
    return await _chart(charts.propose_watch, symbol, body.get("direction"), body.get("level"))


@router.post("/portfolio/{symbol}/condition")
async def propose_condition(request: Request, symbol: str) -> dict:
    """A level he dragged to and confirmed: a price condition on one of his open theses."""
    _authorize(request)
    from backend.mobile import charts
    body = await _goal_body(request)
    thesis = body.get("thesis_id")
    if isinstance(thesis, bool) or not isinstance(thesis, int):
        raise HTTPException(status_code=400, detail="Choose the thesis.")
    return await _chart(charts.propose_condition, symbol, thesis, body.get("direction"),
                        body.get("kind"), body.get("level"))


# ---------------------------------------------------------- next plan, slice 4: the week

async def _week_call(call, *args):
    from backend.mobile import week
    try:
        return await asyncio.get_running_loop().run_in_executor(None, call, *args)
    except week.Missing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except week.Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="The week could not be read or saved just now. Refresh before trying again.") from None


@router.get("/week")
async def get_week(request: Request, start: str = "", days: str = "3") -> Response:
    _authorize(request)
    from backend.mobile import week
    try:
        count = int(days)
    except ValueError:
        raise HTTPException(status_code=400, detail="Show between one and seven days.") from None
    return _conditional(request, await _week_call(week.view, start, count))


@router.post("/week/blocks/{uid}/move")
async def move_week_block(request: Request, uid: str) -> dict:
    _authorize(request)
    from backend.mobile import week
    return await _week_call(week.action, "move", await _goal_body(request), uid)


@router.post("/week/blocks")
async def add_week_block(request: Request) -> dict:
    _authorize(request)
    from backend.mobile import week
    return await _week_call(week.action, "add", await _goal_body(request))


@router.post("/week/undo")
async def undo_week_move(request: Request) -> dict:
    _authorize(request)
    from backend.mobile import week
    return await _week_call(week.action, "undo", await _goal_body(request))


@router.post("/week/apply")
async def apply_week_proposal(request: Request) -> dict:
    _authorize(request)
    from backend.mobile import week
    return await _week_call(week.action, "apply", await _goal_body(request))


# ---------------------------------------------------------- next plan, slice 5: the track record

def _hub_view(since=None) -> dict:
    """Optional parts stay independent, even when a part's module is absent."""
    import importlib
    out = {"notes": []}
    for module, label in (("goals_view", "Goal counts"), ("record", "The forecast record"),
                          ("overnight", "Overnight activity"), ("mind", "The mind map")):
        try:
            reader = importlib.import_module(f"backend.mobile.{module}").hub
            part = reader(since) if module == "overnight" else reader()
            out.update({k: v for k, v in part.items() if k != "notes"})
            out["notes"].extend(part.get("notes", []))
        except Exception as exc:
            log.warning("mobile hub: %s unavailable (%s)", module, exc)
            out["notes"].append(f"{label} could not be read just now.")
    return out


@router.get("/record")
async def get_record(request: Request) -> Response:
    _authorize(request)
    from backend.mobile import record
    view = await asyncio.get_running_loop().run_in_executor(None, record.view)
    return _conditional(request, view)


# ---------------------------------------------------------- next plan, slice 6: while you slept

@router.get("/overnight")
async def get_overnight(request: Request, since: str | None = None) -> Response:
    _authorize(request)
    from backend.mobile import overnight
    data = await asyncio.get_running_loop().run_in_executor(None, overnight.view, since)
    return _conditional(request, data)


@router.get("/vault/note")
async def get_vault_note(request: Request, folder: str = "", name: str = "") -> Response:
    _authorize(request)
    from backend.mobile import notes
    from backend.storage import note_opens
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, notes.read, folder, name)
    except notes.InvalidNote as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "That note no longer exists.") from exc
    except Exception as exc:
        log.warning("mobile notes: read failed (%s)", exc)
        raise HTTPException(503, "That note could not be read just now.") from exc
    await asyncio.get_running_loop().run_in_executor(None, note_opens.record, folder, name)
    return _conditional(request, data)


# ---------------------------------------------------------- next plan, slice 7: the mind map

@router.get("/mind")
async def get_mind(request: Request) -> Response:
    _authorize(request)
    from backend.mobile import mind
    data = await asyncio.get_running_loop().run_in_executor(None, mind.view)
    return _conditional(request, data)


# ------------------------------------------------------------------ slice 4: Inbox and notifications

_PUSH_TOKEN_MAX = 200


def _push_header(request: Request) -> str:
    """The phone's Expo push token, sent in a header rather than the address."""
    token = (request.headers.get("x-push-token") or "").strip()
    return token if 0 < len(token) <= _PUSH_TOKEN_MAX else ""


def _notice_settings(request: Request) -> dict:
    from backend.storage import notices as store
    token = _push_header(request)
    return {"route": store.route(), "push": store.token_state(token) if token else "unknown"}


@router.get("/inbox")
async def get_inbox(request: Request, before: int | None = Query(None),
                    limit: int = Query(50, ge=1, le=200)) -> Response:
    """Every message JARVIS sent the owner on his own (brain/notices.py), newest first."""
    _authorize(request)
    from backend.storage import notices as store
    return _conditional(request, {"notices": store.listing(before, limit), "unread": store.unread_count(),
                                  **_notice_settings(request)})


@router.post("/inbox/read")
async def read_inbox(request: Request) -> dict:
    """{"up_to": id}: he has seen everything up to that message."""
    _authorize(request)
    from backend.storage import notices as store
    try:
        body = _json_object(await request.json())
    except Exception:
        body = {}
    up_to = _notice_id(body.get("up_to"))
    if up_to is None:
        raise HTTPException(status_code=400, detail="up_to must be a message id")
    store.mark_read(up_to)
    return {"unread": store.unread_count()}


@router.get("/notices/settings")
async def get_notice_settings(request: Request) -> dict:
    _authorize(request)
    return _notice_settings(request)


@router.put("/notices/settings")
async def put_notice_settings(request: Request) -> dict:
    """{"route": "discord" | "both" | "phone"}: where JARVIS's own messages go."""
    _authorize(request)
    from backend.storage import notices as store
    try:
        body = _json_object(await request.json())
    except Exception:
        body = {}
    if body.get("route") not in store.ROUTES:
        raise HTTPException(status_code=400, detail=f"route must be one of {list(store.ROUTES)}")
    if not store.set_route(body["route"]):
        raise HTTPException(status_code=503, detail="the setting could not be saved")
    return _notice_settings(request)


@router.post("/push")
async def register_push(request: Request) -> dict:
    """The phone's Expo push token (X-Push-Token). The server sends a test notification; pushes
    are used only once the phone reports seeing it (POST /push/confirm)."""
    _authorize(request)
    from backend.brain import notices
    from backend.storage import notices as store
    token = _push_header(request)
    if not token:
        raise HTTPException(status_code=400, detail="X-Push-Token is required")
    if not store.add_token(token):
        raise HTTPException(status_code=503, detail="the token could not be saved")
    sent = await notices.send_test(token) if store.token_state(token) != "confirmed" else 1
    return {**_notice_settings(request), "test_sent": bool(sent)}


@router.post("/push/confirm")
async def confirm_push(request: Request) -> dict:
    """The phone showed the test notification: from now on JARVIS's messages are pushed."""
    _authorize(request)
    from backend.storage import notices as store
    token = _push_header(request)
    if not token or not store.confirm_token(token):
        raise HTTPException(status_code=404, detail="that token is not registered")
    return _notice_settings(request)


@router.get("/upcoming")
async def get_upcoming(request: Request) -> dict:
    """What the phone should notify about by itself over the next week (mobile/upcoming.py)."""
    _authorize(request)
    from backend.mobile import upcoming
    items = await asyncio.get_running_loop().run_in_executor(None, upcoming.items)
    return {"items": items, **_notice_settings(request)}


# ---------------------------------------------------------- next plan slice 2: goals

async def _goal_read(read, *args):
    from backend.mobile import goals_view
    try:
        return await asyncio.get_running_loop().run_in_executor(None, read, *args)
    except goals_view.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="The goal could not be read just now.") from None


async def _goal_body(request: Request) -> dict:
    try:
        body = json.loads(await _read_bounded(request, MAX_MESSAGE_BODY))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON object.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return body


async def _goal_invoke(action: str, goal_id: str, quote: str, **kwargs) -> dict:
    from backend.brain import goals
    from backend.mobile import goals_view
    from backend.tools import policy
    caller = policy.current_caller.set(policy.APPROVED)
    words = policy.call_quote.set(quote)
    try:
        text = await goals.goal(action=action, goal_id=goal_id, **kwargs)
    finally:
        policy.call_quote.reset(words)
        policy.current_caller.reset(caller)
    if text.startswith("Error:"):
        raise HTTPException(status_code=409, detail=text[6:].strip())
    view = await _goal_read(goals_view.detail, goal_id)
    return {"text": goals_view.confirmation(action, text, view), "goal": view}


async def _goal_question(wait_id: str) -> dict:
    from backend.mobile import goals_view
    from backend.storage import goals as store
    row = await _goal_read(store.get_wait, wait_id)
    if row is None or row["kind"] != "question":
        raise HTTPException(status_code=404, detail="That goal question could not be found.")
    await _goal_read(goals_view.require_goal, row["goal_id"])
    return row


@router.get("/hub")
async def get_hub(request: Request, since: str | None = None) -> Response:
    _authorize(request)
    view = await asyncio.get_running_loop().run_in_executor(None, _hub_view, since)
    return _conditional(request, view)


@router.get("/goals")
async def get_goals(request: Request) -> Response:
    _authorize(request)
    from backend.mobile import goals_view
    return _conditional(request, await _goal_read(goals_view.listing))


@router.post("/goals/answer")
async def answer_goal(request: Request) -> dict:
    _authorize(request)
    body = await _goal_body(request)
    wait_id, text = body.get("wait_id"), body.get("text")
    if not isinstance(wait_id, str) or not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="An answer needs the question and your words.")
    if len(text) > 1500:
        raise HTTPException(status_code=413, detail="An answer is at most 1,500 characters.")
    wait = await _goal_question(wait_id)
    return await _goal_invoke("answer", wait["goal_id"], text, wait_id=wait_id, text=text)


@router.post("/goals/answer/voice")
async def transcribe_goal_answer(request: Request, wait_id: str | None = Query(None)) -> dict:
    """Transcribe only. The owner edits what was heard, then sends it through /goals/answer."""
    _authorize(request)
    from backend.brain import goals
    from backend.storage import goals as store
    if not wait_id:
        raise HTTPException(status_code=400, detail="Choose a goal question first.")
    wait = await _goal_question(wait_id)
    if goals._unavailable():
        raise HTTPException(status_code=409, detail="Goals are off on this server.")
    if wait["state"] != store.OPEN:
        raise HTTPException(status_code=409, detail="That question is already settled.")
    transcript, _ = await _transcribe_body(request)
    return {"wait_id": wait_id, "transcript": transcript}


@router.get("/goals/{goal_id}")
async def get_goal(request: Request, goal_id: str) -> Response:
    _authorize(request)
    from backend.mobile import goals_view
    return _conditional(request, await _goal_read(goals_view.detail, goal_id))


@router.get("/goals/{goal_id}/files/{name}")
async def get_goal_file(request: Request, goal_id: str, name: str) -> dict:
    _authorize(request)
    from backend.mobile import goals_view
    return await _goal_read(goals_view.file, goal_id, name)


@router.get("/goals/{goal_id}/evidence/{evidence_id}")
async def get_goal_evidence(request: Request, goal_id: str, evidence_id: str) -> dict:
    _authorize(request)
    from backend.mobile import goals_view
    return await _goal_read(goals_view.evidence, goal_id, evidence_id)


@router.post("/goals/{goal_id}/control")
async def control_goal(request: Request, goal_id: str) -> dict:
    _authorize(request)
    from backend.mobile import goals_view
    body = await _goal_body(request)
    action = body.get("action")
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(status_code=400, detail="Choose pause, resume or cancel.")
    await _goal_read(goals_view.require_goal, goal_id)
    return await _goal_invoke(action, goal_id, f"phone: {action} goal {goal_id}")


@router.post("/goals/{goal_id}/budget")
async def budget_goal(request: Request, goal_id: str) -> dict:
    _authorize(request)
    from backend.mobile import goals_view
    body = await _goal_body(request)
    usd = body.get("usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0 or usd > 20 or not math.isfinite(usd):
        raise HTTPException(status_code=400, detail="The total budget must be above $0 and at most $20.")
    await _goal_read(goals_view.require_goal, goal_id)
    return await _goal_invoke("budget", goal_id, f"phone: raise goal {goal_id} total budget to ${usd}", budget_usd=usd)


@router.post("/goals/proposals/{goal_id}/start")
async def start_goal(request: Request, goal_id: str) -> dict:
    _authorize(request)
    from backend.mobile import goals_view
    await _goal_read(goals_view.require_goal, goal_id)
    return await _goal_invoke("start", goal_id, f"phone: start proposed goal {goal_id}", proposal_id=goal_id)


@router.post("/goals/proposals/{goal_id}/decline")
async def decline_goal(request: Request, goal_id: str) -> dict:
    _authorize(request)
    from backend.brain import goals
    from backend.mobile import goals_view
    from backend.storage import goals as store
    row = await _goal_read(goals_view.require_goal, goal_id)
    if goals._unavailable():
        raise HTTPException(status_code=409, detail="Goals are off on this server.")
    if row["state"] == store.CANCELLED:
        return {"text": "That offer was already declined."}
    if row["state"] != store.PROPOSED:
        raise HTTPException(status_code=409, detail="That offer is no longer waiting for a decision.")
    from functools import partial
    changed = await _goal_read(partial(store.transition, goal_id, store.CANCELLED,
                                      expect=(store.PROPOSED,), actor="owner", reason="phone: Not now"))
    if not changed:
        raise HTTPException(status_code=409, detail="That offer changed. Refresh the list.")
    return {"text": "Offer declined. Nothing was started."}
