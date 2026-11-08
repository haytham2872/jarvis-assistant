"""Live voice: the phone streams the microphone, the server cuts it at pauses, transcribes each
piece as soon as it closes, and (conversation mode) decides when the turn is over.

One LiveSession per WebSocket. The model functions are injected (defaults: Silero VAD, the
transcriber, turn_detect) so the session is testable without a model. All time below is audio
time (samples received), not the wall clock, so a slow server cuts exactly where a fast one does.

Flow: feed() appends PCM and runs the VAD over every whole 512-sample frame in the executor;
a pause of CUT_SILENCE_MS closes a segment (with PRE_ROLL_MS before it and POST_PAD_MS after),
a segment of continuous speech past MAX_SEGMENT_S is cut at its quietest frame in the last 2 s.
Closed segments go to ONE worker that transcribes them in order and reports the text so far.
Conversation mode: END_CHECK_MS into a pause, once, the turn detector is asked whether the
sentence is complete; the answer sets how long the pause must last before the turn commits.
Note mode never commits on silence, only on the phone's "end".
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 512
CONTEXT = 64
START_P = 0.5
END_P = 0.35
PRE_ROLL_MS = 300
POST_PAD_MS = 100
CUT_SILENCE_MS = 250
END_CHECK_MS = 250
MAX_SEGMENT_S = 8
CUT_WINDOW_S = 2
NO_SPEECH_S = 8
MAX_TOTAL_S = 120
VOICED_MS_TO_COUNT = 200
DEFAULT_COMMIT_MS = 1200.0


def _ms(ms: float) -> int:
    return int(ms * SAMPLE_RATE / 1000)


@dataclass
class Result:
    outcome: str            # turn | empty | no_speech | cancel | disconnect | error
    text: str = ""
    detail: str = ""


# ------------------------------------------------------------------ default model functions

_silero = None


def default_vad_factory() -> Callable[[np.ndarray], float]:
    """A fresh Silero streaming state: call it with 512 samples, get the speech probability."""
    global _silero
    if _silero is None:
        import onnx_asr
        import onnxruntime as ort
        from backend.audio.transcriber import no_spin
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1      # one 512-sample frame needs no pool
        so.inter_op_num_threads = 1
        _silero = onnx_asr.load_vad("silero", sess_options=no_spin(so))._model
    model = _silero
    state = np.zeros((2, 1, 128), np.float32)
    context = np.zeros(CONTEXT, np.float32)
    sr = np.array([SAMPLE_RATE], dtype=np.int64)

    def step(frame: np.ndarray) -> float:
        nonlocal state, context
        inp = np.concatenate([context, frame]).astype(np.float32)[None, :]
        out, state = model.run(["output", "stateN"], {"input": inp, "state": state, "sr": sr})
        context = frame[-CONTEXT:].astype(np.float32)
        return float(out[0, 0])
    return step


def _default_asr(pcm: np.ndarray) -> str:
    from backend.audio import transcriber
    return transcriber.transcribe(pcm)


def _default_warm() -> None:
    from backend.audio import transcriber
    transcriber.warm()


def _default_turn_prob(pcm: np.ndarray):
    from backend.audio import turn_detect
    return turn_detect.complete_probability(pcm)


def _default_commit_after(prob) -> float:
    try:
        from backend.audio import turn_detect
    except ImportError:
        return DEFAULT_COMMIT_MS
    return turn_detect.commit_after_ms(prob)


class LiveSession:
    def __init__(self, mode: str, send: Callable[[dict], Awaitable[None]], *,
                 vad_factory=None, asr=None, warm=None, turn_prob=None, commit_after=None,
                 now=time.monotonic) -> None:
        self.mode = "note" if mode == "note" else "conversation"
        self._send = send
        self._vad_factory = vad_factory or default_vad_factory
        self._asr = asr or _default_asr
        self._warm = warm or _default_warm
        self._turn_prob = turn_prob or _default_turn_prob
        self._commit_after = commit_after or _default_commit_after
        self._now = now
        cap = _ms((MAX_TOTAL_S + 5) * 1000)
        self._buf = np.zeros(cap, np.float32)
        self._cap = cap
        self._n = 0                 # samples received
        self._nproc = 0             # samples the VAD has seen (whole frames)
        self._odd = b""
        self._vad = None
        self._probs: list[float] = []
        self._seg_start: int | None = None
        self._prev_end = 0
        self._last_voiced_end: int | None = None
        self._voiced = 0
        self._voice_gen = 0
        self._ever_voiced = False
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._texts: list[str] = []
        self._error = ""
        self._wake = asyncio.Event()
        self._reason = ""
        self._pause: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._warm_task: asyncio.Task | None = None
        # numbers for the one log line
        self._segments = 0
        self._asr_ms = 0.0
        self._last_asr_ms = 0.0
        self._turn_p = None
        self._last_voiced_wall: float | None = None

    # ------------------------------------------------------------------ public

    async def run(self, receive: Callable[[], Awaitable]) -> Result:
        """Drive the session. `receive()` returns bytes (PCM), "end", "cancel", or None
        (the phone went away). Returns once the turn is decided."""
        from backend.tasks import spawn
        loop = asyncio.get_running_loop()
        self._warm_task = loop.run_in_executor(None, self._warm)
        self._tasks.append(spawn(self._worker(), name="live-asr"))
        result = Result("error", detail="no result")
        try:
            while not self._reason:
                rx = asyncio.ensure_future(receive())
                waker = asyncio.ensure_future(self._wake.wait())
                try:
                    await asyncio.wait({rx, waker}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waker.cancel()
                if not rx.done():
                    rx.cancel()
                    break
                msg = rx.result()
                if msg is None:
                    self._request("disconnect")
                elif msg == "end":
                    self._request("end")
                elif msg == "cancel":
                    self._request("cancel")
                else:
                    await self.feed(msg)
            result = await self._finalize()
        except Exception as exc:                      # a model failed: tell the phone, never hang
            log.exception("live voice session failed")
            result = Result("error", detail=str(exc) or exc.__class__.__name__)
        finally:
            for t in self._tasks:
                t.cancel()
            self._log(result)
        return result

    async def feed(self, data: bytes) -> None:
        """PCM int16 little-endian. Odd byte counts keep the leftover byte for the next frame."""
        if self._reason or not data:
            return
        data = self._odd + bytes(data)
        usable = len(data) - (len(data) % 2)
        self._odd = data[usable:]
        pcm = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
        pcm = pcm[: self._cap - self._n]
        self._buf[self._n:self._n + pcm.size] = pcm
        self._n += pcm.size
        async with self._lock:
            if self._reason:
                return
            gen = self._voice_gen
            closed = await asyncio.get_running_loop().run_in_executor(None, self._ingest)
            if self._voice_gen != gen:
                self._last_voiced_wall = self._now()
        for start, end in closed:
            self._enqueue(start, end)
        self._after_chunk()

    async def quiet(self) -> None:
        """Wait for the transcription queue and any pause check to finish (tests, mostly)."""
        await self._queue.join()
        while self._pause and self._pause.get("task") and not self._pause["task"].done():
            await asyncio.sleep(0)
            await asyncio.sleep(0.005)
        await self._queue.join()

    # ------------------------------------------------------------------ the VAD (executor)

    def _ingest(self) -> list[tuple[int, int]]:
        if self._vad is None:
            self._vad = self._vad_factory()
        closed: list[tuple[int, int]] = []
        while self._n - self._nproc >= FRAME:
            s = self._nproc
            p = float(self._vad(self._buf[s:s + FRAME]))
            self._probs.append(p)
            self._nproc = s + FRAME
            self._step(s, p, closed)
        return closed

    def _step(self, s: int, p: float, closed: list) -> None:
        e = s + FRAME
        if self._seg_start is None:
            if p >= START_P:
                self._seg_start = max(self._prev_end, s - _ms(PRE_ROLL_MS))
                self._voiced_frame(e)
            return
        if p >= END_P:
            self._voiced_frame(e)
        if e - self._last_voiced_end >= _ms(CUT_SILENCE_MS):
            end = min(e, self._last_voiced_end + _ms(POST_PAD_MS))
            closed.append((self._seg_start, end))
            self._prev_end = end
            self._seg_start = None
        elif e - self._seg_start >= MAX_SEGMENT_S * SAMPLE_RATE:
            first = max((e - CUT_WINDOW_S * SAMPLE_RATE) // FRAME, self._seg_start // FRAME + 1)
            window = self._probs[first:e // FRAME]
            low = min(window)
            idx = first + max(i for i, v in enumerate(window) if v == low)
            cut = idx * FRAME + FRAME // 2
            closed.append((self._seg_start, cut))
            self._prev_end = self._seg_start = cut

    def _voiced_frame(self, e: int) -> None:
        self._last_voiced_end = e
        self._voiced += FRAME
        self._voice_gen += 1
        self._ever_voiced = True

    # ------------------------------------------------------------------ turn logic (loop)

    def _silence_samples(self) -> int:
        if self._last_voiced_end is None:
            return 0
        return self._nproc - self._last_voiced_end

    def _after_chunk(self) -> None:
        if self._reason:
            return
        if self._n >= MAX_TOTAL_S * SAMPLE_RATE:
            self._request("cap")
            return
        if self.mode != "conversation":
            return
        if not self._ever_voiced:
            if self._nproc >= NO_SPEECH_S * SAMPLE_RATE:
                self._request("no_speech")
            return
        pause = self._pause
        if pause and pause["gen"] != self._voice_gen:     # the owner spoke again: the pause is void
            if pause.get("task"):
                pause["task"].cancel()
            self._pause = pause = None
        if self._voiced < _ms(VOICED_MS_TO_COUNT):
            return
        sil = self._silence_samples()
        if pause is None and sil >= _ms(END_CHECK_MS):
            end = min(self._nproc, self._last_voiced_end + _ms(POST_PAD_MS))
            pause = self._pause = {"gen": self._voice_gen, "threshold": None, "task": None,
                                   "audio": self._buf[:end].copy()}
            from backend.tasks import spawn
            pause["task"] = spawn(self._check(pause), name="live-turn-check")
            self._tasks.append(pause["task"])
        self._maybe_commit()

    def _maybe_commit(self) -> None:
        pause = self._pause
        if pause and pause["threshold"] is not None and not self._reason:
            if self._silence_samples() >= _ms(pause["threshold"]):
                self._request("silence")

    async def _check(self, pause: dict) -> None:
        loop = asyncio.get_running_loop()
        await self._queue.join()                          # after any pending transcription
        if self._pause is not pause:
            return
        try:
            prob = await loop.run_in_executor(None, self._turn_prob, pause["audio"])
        except Exception:
            log.exception("turn detector failed")
            prob = None
        if self._pause is not pause or self._reason:
            return
        self._turn_p = prob
        pause["threshold"] = float(self._commit_after(prob))
        self._maybe_commit()

    def _request(self, reason: str) -> None:
        if not self._reason:
            self._reason = reason
            self._wake.set()

    # ------------------------------------------------------------------ transcription

    def _enqueue(self, start: int, end: int) -> None:
        if end > start:
            self._segments += 1
            self._queue.put_nowait(self._buf[start:end].copy())

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self._warm_task
        except Exception:
            log.exception("live voice warm-up failed")
        while True:
            pcm = await self._queue.get()
            t0 = self._now()
            try:
                text = (await loop.run_in_executor(None, self._asr, pcm) or "").strip()
            except Exception as exc:
                log.exception("live voice transcription failed")
                self._error = str(exc) or exc.__class__.__name__
                text = ""
            finally:
                self._last_asr_ms = (self._now() - t0) * 1000
                self._asr_ms += self._last_asr_ms
                self._queue.task_done()
            if text:
                self._texts.append(text)
                await self._send({"type": "heard", "text": " ".join(self._texts)})

    async def _finalize(self) -> Result:
        reason = self._reason
        if reason in ("cancel", "disconnect"):
            return Result(reason)
        if reason == "no_speech":
            return Result("no_speech")
        async with self._lock:
            if self._seg_start is not None and self._last_voiced_end is not None:
                end = min(self._nproc, self._last_voiced_end + _ms(POST_PAD_MS))
                self._enqueue(self._seg_start, end)
                self._seg_start = None
        await self._queue.join()
        if self._error:
            return Result("error", detail=self._error)
        text = " ".join(self._texts)
        return Result("turn" if text else "empty", text)

    def _log(self, result: Result) -> None:
        wait = None
        if self._last_voiced_wall is not None:
            wait = round((self._now() - self._last_voiced_wall) * 1000)
        info = {"audio_s": round(self._n / SAMPLE_RATE, 1), "segments": self._segments,
                "asr_ms": round(self._asr_ms), "last_asr_ms": round(self._last_asr_ms),
                "end_wait_ms": wait, "turn_p": self._turn_p, "mode": self.mode,
                "chars": len(result.text), "outcome": result.outcome, "reason": self._reason}
        log.info("mobile live voice: %s", json.dumps(info))
