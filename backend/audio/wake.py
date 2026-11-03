import asyncio
import threading
import logging
import numpy as np
import sounddevice as sd
from openwakeword.model import Model

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms at 16kHz — required by openWakeWord


def ensure_models():
    """Download openWakeWord ONNX models if they are missing."""
    try:
        import openwakeword
        openwakeword.utils.download_models()
        log.info("openWakeWord models ready.")
    except Exception as e:
        log.warning("openWakeWord model download failed: %s", e)


class WakeWordDetector:
    def __init__(self, on_wake, threshold: float = 0.5):
        ensure_models()
        self._on_wake = on_wake
        self._threshold = threshold
        # Restrict to the "hey jarvis" model only. Without this filter,
        # openWakeWord loads every downloaded pretrained model (alexa,
        # hey_mycroft, weather, …) and the score loop below would wake on
        # any of them.
        self._model = Model(
            wakeword_models=["hey_jarvis"], inference_framework="onnx"
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        buffer = np.zeros(0, dtype=np.int16)

        def audio_cb(indata, frames, time, status):
            nonlocal buffer
            chunk = (indata[:, 0] * 32767).astype(np.int16)
            buffer = np.concatenate([buffer, chunk])
            while len(buffer) >= CHUNK:
                frame, buffer = buffer[:CHUNK], buffer[CHUNK:]
                scores = self._model.predict(frame)
                for score in scores.values():
                    if score >= self._threshold:
                        asyncio.run_coroutine_threadsafe(
                            self._on_wake(), self._loop
                        )
                        return

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK,
            callback=audio_cb,
        ):
            while self._running:
                sd.sleep(100)
