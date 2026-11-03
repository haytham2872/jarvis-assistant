import asyncio
import logging
import threading
import numpy as np

from backend.audio import transcriber

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SILENCE_THRESHOLD = 0.015
SILENCE_DURATION = 0.8   # seconds of silence before stopping (was 1.5s)


class SpeechToText:
    """The desk microphone: records until silence. Transcription is backend/audio/transcriber.py,
    shared with Discord voice notes (Parakeet, English and French)."""

    async def listen_and_transcribe(self) -> str:
        audio = await asyncio.get_event_loop().run_in_executor(
            None, self._record_until_silence
        )
        if len(audio) < SAMPLE_RATE * 0.3:
            return ""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._transcribe, audio
        )

    def _record_until_silence(self) -> np.ndarray:
        # Imported here, not at the top: loading sounddevice starts PortAudio, which raises on
        # a machine with PulseAudio installed but not running (a server, a CI runner), and
        # only the desk microphone needs it.
        import sounddevice as sd
        recorded: list[np.ndarray] = []
        silent_chunks = 0
        silence_limit = int(SILENCE_DURATION * SAMPLE_RATE / 512)

        def cb(indata, frames, time, status):
            nonlocal silent_chunks
            chunk = indata[:, 0].copy()
            recorded.append(chunk)
            rms = float(np.sqrt(np.mean(chunk**2)))
            if rms < SILENCE_THRESHOLD:
                silent_chunks += 1
            else:
                silent_chunks = 0

        stop_event = threading.Event()

        def _monitor():
            while not stop_event.is_set():
                if silent_chunks >= silence_limit and len(recorded) > silence_limit:
                    stop_event.set()
                    break
                sd.sleep(50)

        monitor = threading.Thread(target=_monitor, daemon=True)
        monitor.start()

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=512,
            callback=cb,
        ):
            stop_event.wait(timeout=30)

        return np.concatenate(recorded) if recorded else np.zeros(0, dtype=np.float32)

    def _transcribe(self, audio: np.ndarray) -> str:
        return transcriber.transcribe(audio)

    def transcribe_file(self, path: str) -> str:
        return transcriber.transcribe_file(path)
