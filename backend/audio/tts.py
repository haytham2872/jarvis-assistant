import asyncio
import io
import logging
import os
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)

TTS_VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")
TTS_RATE = os.getenv("TTS_RATE", "+8%")
TTS_PITCH = os.getenv("TTS_PITCH", "-5Hz")
TTS_FALLBACK_VOICE = os.getenv("TTS_FALLBACK_VOICE", "am_adam")

# ElevenLabs — primary voice when ELEVENLABS_API_KEY is set. Adam voice.
_ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"   # Adam
_ELEVENLABS_MODEL = "eleven_flash_v2_5"

# One shared connection-pooled client — a per-sentence AsyncClient pays a fresh
# TLS handshake on every sentence of every reply.
_eleven_client = None


def _get_eleven_client():
    global _eleven_client
    if _eleven_client is None:
        import httpx
        _eleven_client = httpx.AsyncClient(timeout=10.0)
    return _eleven_client

_AMP_FPS = 30
_KOKORO_SAMPLE_RATE = 24000


class TextToSpeech:
    def __init__(self):
        self._interrupt = threading.Event()
        self._kokoro_lock = threading.Lock()
        self._kokoro = None
        # Set when ElevenLabs reports its quota exhausted — no point paying a
        # doomed HTTP round-trip (plus a warning line) per sentence for the
        # rest of the session. Resets on restart.
        self._eleven_quota_exhausted = False

        if os.getenv("ELEVENLABS_API_KEY"):
            # Kokoro is the third-choice fallback here — don't pay its torch
            # model load (significant RAM, has OOM'd at startup alongside
            # whisper + embedder) for a path that's rarely taken. It still
            # lazy-loads on first actual use via _get_kokoro().
            log.info("TTS: ElevenLabs (Adam) → edge-tts (fallback) → Kokoro (lazy fallback)")
        else:
            # No ElevenLabs key: Kokoro is one network failure away — warm it
            # in the background so the fallback is instant.
            threading.Thread(target=self._warm_kokoro, daemon=True).start()
            log.info("TTS: edge-tts (%s) → Kokoro (fallback)", TTS_VOICE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interrupt(self) -> None:
        # Cooperative stop: the active playback thread owns its stream and aborts
        # it itself when it sees this flag. We must NOT touch the stream from here
        # (cross-thread PortAudio teardown crashes the process).
        self._interrupt.set()

    async def speak_from_queue(
        self,
        sentence_queue: asyncio.Queue,
        on_state_cb=None,
        on_amplitude=None,
    ) -> None:
        """Play sentences from a queue as they arrive.

        A background synthesis worker reads from sentence_queue and pre-synthesizes
        audio so playback starts as soon as the first sentence is ready.
        The caller must put None into sentence_queue when no more sentences will come.
        """
        self._interrupt.clear()
        audio_queue: asyncio.Queue = asyncio.Queue()

        async def synth_worker() -> None:
            try:
                while not self._interrupt.is_set():
                    sentence = await sentence_queue.get()
                    if sentence is None:
                        return
                    audio_data = await self._synthesize(sentence)
                    await audio_queue.put(audio_data)
            except asyncio.CancelledError:
                pass
            finally:
                audio_queue.put_nowait(None)  # always unblock the playback loop

        synth_task = asyncio.create_task(synth_worker())
        state_notified = False

        try:
            while True:
                if self._interrupt.is_set():
                    break
                item = await audio_queue.get()
                if item is None:
                    break
                audio, sr = item
                if not state_notified:
                    if on_state_cb:
                        await on_state_cb()
                    state_notified = True
                if audio is not None and len(audio) > 0:
                    await self._play_audio(audio, sr, on_amplitude)
        except asyncio.CancelledError:
            self._interrupt.set()   # signal the playback thread to abort its own stream
            raise
        finally:
            synth_task.cancel()
            if on_amplitude:
                await on_amplitude(0.0)

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    async def _synthesize(self, text: str) -> tuple[np.ndarray | None, int]:
        # ElevenLabs primary — only when ELEVENLABS_API_KEY is set. Self-contained:
        # returns None on missing key / network error / non-200 / decode failure,
        # so edge-tts catches it immediately with no retry needed.
        eleven = await self._speak_elevenlabs(text)
        if eleven is not None:
            audio, sr = eleven
            if len(audio) > 0:
                log.info("TTS: ElevenLabs OK")
                return audio, sr

        # Try edge-tts twice (handles transient network hiccups) with a hard timeout
        for attempt in range(1, 3):
            try:
                audio, sr = await asyncio.wait_for(
                    self._synthesize_edge(text), timeout=8.0
                )
                if len(audio) > 0:
                    log.info("TTS: edge-tts OK (attempt %d)", attempt)
                    return audio, sr
                log.warning("edge-tts returned empty audio (attempt %d/2)", attempt)
            except asyncio.TimeoutError:
                log.warning("edge-tts timed out after 8s (attempt %d/2)", attempt)
            except Exception as exc:
                log.warning("edge-tts failed %s: %s (attempt %d/2)",
                            type(exc).__name__, exc, attempt)

        log.info("TTS: both edge-tts attempts failed — using Kokoro fallback")
        try:
            return await self._synthesize_kokoro(text)
        except Exception as exc:
            log.error("Kokoro fallback also failed: %s", exc)
            return None, _KOKORO_SAMPLE_RATE

    async def _speak_elevenlabs(self, text: str) -> tuple[np.ndarray, int] | None:
        """Synthesize one sentence via ElevenLabs (Adam). Primary engine.

        Returns (float32 audio, sample_rate) on success, or None when the API
        key is absent or anything fails (network, non-200, decode) — the caller
        then falls back to edge-tts. Single attempt, 10s timeout, no retry: a
        failure should hand off to edge-tts immediately.

        Returns a (audio, sr) tuple rather than a bare array because the rest of
        the pipeline (playback, amplitude) needs the real sample rate decoded
        from the MP3 — same shape as _synthesize_edge.
        """
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key or self._eleven_quota_exhausted:
            return None

        try:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{_ELEVENLABS_VOICE_ID}"
            headers = {
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }
            body = {
                "text": text,
                "model_id": _ELEVENLABS_MODEL,
                "voice_settings": {
                    "stability": 0.45,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True,
                },
            }
            resp = await _get_eleven_client().post(url, headers=headers, json=body)

            if resp.status_code != 200:
                if "quota_exceeded" in resp.text:
                    # Quota won't recover mid-session — disable the engine so
                    # every subsequent sentence skips straight to edge-tts
                    # instead of logging a warning per sentence (live: an
                    # exhausted key produced a 401 + warning on every single
                    # sentence for the rest of the day).
                    self._eleven_quota_exhausted = True
                    log.warning(
                        "ElevenLabs quota exhausted — using edge-tts for the rest of this session"
                    )
                    return None
                log.warning("ElevenLabs HTTP %d: %s", resp.status_code, resp.text[:200])
                return None

            # soundfile >= 0.12.0 (libsndfile/minimp3) decodes MP3 — same path as edge-tts
            buf = io.BytesIO(resp.content)
            audio, sr = sf.read(buf, dtype="float32", always_2d=False)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            return audio, int(sr)
        except Exception as exc:
            log.warning("ElevenLabs failed %s: %s", type(exc).__name__, exc)
            return None

    async def _synthesize_edge(self, text: str) -> tuple[np.ndarray, int]:
        import edge_tts

        communicate = edge_tts.Communicate(
            text, voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH
        )
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])

        if not chunks:
            return np.array([], dtype=np.float32), _KOKORO_SAMPLE_RATE

        # soundfile >= 0.12.0 bundles libsndfile 1.1.0 which reads MP3 via minimp3
        buf = io.BytesIO(b"".join(chunks))
        audio, sr = sf.read(buf, dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return audio, int(sr)

    async def _synthesize_kokoro(self, text: str) -> tuple[np.ndarray, int]:
        loop = asyncio.get_event_loop()

        def _run() -> np.ndarray:
            pipeline = self._get_kokoro()
            chunks = []
            for _gs, _ps, audio in pipeline(
                text, voice=TTS_FALLBACK_VOICE, speed=1.0
            ):
                if audio is not None and len(audio) > 0:
                    chunks.append(audio)
            return (
                np.concatenate(chunks)
                if chunks
                else np.array([], dtype=np.float32)
            )

        audio = await loop.run_in_executor(None, _run)
        return audio, _KOKORO_SAMPLE_RATE

    def _get_kokoro(self):
        with self._kokoro_lock:
            if self._kokoro is None:
                from kokoro import KPipeline
                self._kokoro = KPipeline(lang_code="a")
                log.info("Kokoro TTS pipeline ready.")
        return self._kokoro

    def _warm_kokoro(self) -> None:
        try:
            self._get_kokoro()
        except Exception as exc:
            log.warning("Kokoro background warm-up failed: %s", exc)

    # ------------------------------------------------------------------
    # Playback + amplitude streaming
    # ------------------------------------------------------------------

    async def _play_audio(
        self, audio: np.ndarray, sr: int, on_amplitude
    ) -> None:
        loop = asyncio.get_event_loop()
        play_done = asyncio.Event()

        # float32 contiguous is what the output stream expects.
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)
        audio = np.ascontiguousarray(audio)
        channels = audio.shape[1] if audio.ndim > 1 else 1

        def _play() -> None:
            # Own the stream entirely within this thread and tear it down here.
            # An interrupt must never close a PortAudio stream from another thread
            # (sd.play/sd.stop share one global stream — stopping it from the event
            # loop while this thread plays/waits on it races on freed memory and
            # crashes the whole process). Interrupt is cooperative: we stop writing
            # and abort our own stream.
            stream = None
            try:
                stream = sd.OutputStream(samplerate=sr, channels=channels, dtype="float32")
                stream.start()
                block = max(1, sr // 50)  # ~20 ms — keeps interrupt latency low
                idx, n = 0, len(audio)
                while idx < n and not self._interrupt.is_set():
                    stream.write(audio[idx:idx + block])
                    idx += block
                if not self._interrupt.is_set():
                    # Flush trailing buffered frames so the tail isn't clipped.
                    pad = (sr // 8, channels) if channels > 1 else (sr // 8,)
                    stream.write(np.zeros(pad, dtype="float32"))
            except Exception as exc:
                # A write failing mid-interrupt (stream torn down) is expected —
                # only surface errors during normal playback.
                if self._interrupt.is_set():
                    log.debug("playback stopped on interrupt: %s", exc)
                else:
                    log.warning("playback error: %s", exc)
            finally:
                if stream is not None:
                    try:
                        stream.abort() if self._interrupt.is_set() else stream.stop()
                        stream.close()
                    except Exception as exc:
                        log.debug("stream teardown: %s", exc)
                loop.call_soon_threadsafe(play_done.set)

        threading.Thread(target=_play, daemon=True).start()

        samples_per_frame = max(1, sr // _AMP_FPS)
        peak = float(np.max(np.abs(audio))) if len(audio) > 0 else 1.0
        if peak < 1e-6:
            peak = 1.0

        total_frames = max(1, len(audio) // samples_per_frame)
        frame_duration = 1.0 / _AMP_FPS

        for i in range(total_frames):
            if self._interrupt.is_set() or play_done.is_set():
                break
            start = i * samples_per_frame
            end = min(start + samples_per_frame, len(audio))
            rms = float(np.sqrt(np.mean(audio[start:end] ** 2)))
            amp = min(1.0, rms / peak * 3.0)
            if on_amplitude:
                await on_amplitude(amp)
            await asyncio.sleep(frame_duration)

        while not play_done.is_set() and not self._interrupt.is_set():
            await asyncio.sleep(0.05)
