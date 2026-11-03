"""Speech to text for Discord voice notes and the desk microphone.

Parakeet TDT 0.6B v3 (NVIDIA, int8 ONNX through onnx-asr, no torch): English and French in
the same note, punctuated, language found by the model itself. faster-whisper is the
fallback when Parakeet cannot load (onnx-asr missing, or the model never downloaded on this
host), with language detection rather than forced English.

Measured 4 October 2026 (desk, 2 threads like the server's 2 vCPUs, OGG/Opus notes like
Discord's, synthetic voices): the old setup, Whisper base forced to English, turned French
into invented English (100-104% word error on French, 87% on a French/English sentence:
"peux check my calendar" became "check Mike Allen Darfur"). Parakeet: 0-6% on all five
notes, about 4x real time. Whisper small was as accurate and two to three times slower.

Memory: with onnxruntime's arena on, the model peaked at +1,070 MB; off, +700 MB with a
+10 MB peak during a note, for ~15% more time. The server has 2.2 GB free, so the arena is
off, the model loads on the first note (~5 s here) and is released after 10 idle minutes.

The model must be downloaded once per host (main.py runs with HF_HUB_OFFLINE=1):
    HF_HUB_OFFLINE=0 python -m backend.audio.transcriber --download
"""
from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
PARAKEET = "nemo-parakeet-tdt-0.6b-v3"
_IDLE_RELEASE_S = 600.0

_lock = threading.Lock()
_run = None            # the loaded engine: callable(pcm) -> text
_kind = ""             # "parakeet" | "whisper"
_last_used = 0.0
_timer: threading.Timer | None = None


def _threads() -> int:
    configured = os.getenv("STT_THREADS", "").strip()
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    return max(1, min(4, os.cpu_count() or 2))


def decode(path: str) -> np.ndarray:
    """Any audio file (Discord sends OGG/Opus) as 16 kHz mono float32."""
    import av
    chunks = []
    with av.open(path) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(container.streams.audio[0]):
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _load_parakeet():
    import onnx_asr
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = _threads()
    so.inter_op_num_threads = 1
    so.enable_cpu_mem_arena = False      # +700 MB instead of +1,070 MB (measured)
    so.enable_mem_pattern = False
    model = onnx_asr.load_model(PARAKEET, quantization="int8", sess_options=so)
    # The model reads up to ~25 s at a time; the VAD cuts a long note into speech segments.
    model = model.with_vad(onnx_asr.load_vad("silero", sess_options=so))

    def run(pcm: np.ndarray) -> str:
        return " ".join(t for t in (s.text.strip() for s in model.recognize(pcm, sample_rate=SAMPLE_RATE)) if t)
    return run


def _load_whisper():
    from faster_whisper import WhisperModel
    model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu", compute_type="int8",
                         cpu_threads=_threads())

    def run(pcm: np.ndarray) -> str:
        segments, _ = model.transcribe(pcm, beam_size=1, language=None, vad_filter=True,
                                       temperature=0, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
    return run


def _load():
    """Parakeet, or Whisper when Parakeet cannot load. STT_ENGINE=whisper skips Parakeet."""
    global _run, _kind
    if os.getenv("STT_ENGINE", "parakeet").strip().lower() != "whisper":
        try:
            t0 = time.monotonic()
            _run, _kind = _load_parakeet(), "parakeet"
            log.info("STT: Parakeet loaded in %.1fs", time.monotonic() - t0)
            return
        except Exception as e:
            log.warning("STT: Parakeet unavailable (%s) -- using Whisper. To install it: "
                        "HF_HUB_OFFLINE=0 python -m backend.audio.transcriber --download", e)
    _run, _kind = _load_whisper(), "whisper"


def engine() -> str:
    """Which engine is loaded right now ('' when none)."""
    return _kind if _run is not None else ""


def release() -> bool:
    """Drop the loaded model if nothing is transcribing. True when something was released."""
    global _run, _kind
    if not _lock.acquire(blocking=False):
        return False
    try:
        if _run is None:
            return False
        _run, _kind = None, ""
    finally:
        _lock.release()
    gc.collect()
    log.info("STT: model released")
    return True


def _release_if_idle() -> None:
    if time.monotonic() - _last_used >= _IDLE_RELEASE_S - 1:
        release()      # 10 minutes without a note: give the ~700 MB back


def _schedule_release() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
    _timer = threading.Timer(_IDLE_RELEASE_S, _release_if_idle)
    _timer.daemon = True
    _timer.start()


def warm() -> bool:
    """Load the model now (the phone has started recording) and restart the idle clock, so the
    upload that follows finds it loaded. Blocking: call from an executor. True if it loaded."""
    global _last_used
    with _lock:
        loaded = _run is None
        if loaded:
            _load()
        _last_used = time.monotonic()
    _schedule_release()
    return loaded


def transcribe(pcm: np.ndarray) -> str:
    """16 kHz mono float32 audio to text. Blocking: call it from an executor."""
    global _last_used
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if pcm.size < SAMPLE_RATE * 0.3:
        return ""
    with _lock:
        if _run is None:
            _load()
        t0 = time.monotonic()
        text = _run(pcm).strip()
        _last_used = time.monotonic()
        log.info("STT (%s): %.2fs for %.1fs of audio", _kind, _last_used - t0, pcm.size / SAMPLE_RATE)
    _schedule_release()
    return text


def transcribe_file(path: str) -> str:
    return transcribe(decode(path))


if __name__ == "__main__":
    if "--download" in sys.argv:
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["STT_ENGINE"] = "parakeet"
        logging.basicConfig(level=logging.INFO)
        _load()
        print("Parakeet ready" if _kind == "parakeet" else "Parakeet could not load: see the warning above")
