"""Has the speaker finished the sentence? Pipecat Smart Turn v3.2 (ONNX, CPU) on the turn's audio."""
import logging
import threading

import numpy as np
from huggingface_hub import hf_hub_download

log = logging.getLogger(__name__)

MODEL_REPO = "pipecat-ai/smart-turn-v3"
MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
END_CHECK_MS = 250

SURE_P = 0.7
MAYBE_P = 0.3
COMMIT_SURE_MS = 250
COMMIT_MAYBE_MS = 900
COMMIT_UNSURE_MS = 2000
COMMIT_NO_MODEL_MS = 1200

_SR = 16000
_WINDOW = 8 * _SR

_lock = threading.Lock()
_loaded = None       # (session, feature_extractor) once loaded
_failed = False      # a load attempt failed; do not retry
_warned = False


def _warn(msg: str, *args) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning(msg, *args)


def commit_after_ms(prob: float | None) -> float:
    if prob is None:
        return COMMIT_NO_MODEL_MS
    if prob >= SURE_P:
        return COMMIT_SURE_MS
    if prob >= MAYBE_P:
        return COMMIT_MAYBE_MS
    return COMMIT_UNSURE_MS


def _load():
    """(session, feature extractor) or None. Offline: the file must already be downloaded."""
    global _loaded, _failed
    with _lock:
        if _loaded is not None:
            return _loaded
        if _failed:
            return None
        try:
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor
            from backend.audio.transcriber import _threads
            path = hf_hub_download(MODEL_REPO, MODEL_FILE, local_files_only=True)
            so = ort.SessionOptions()
            so.intra_op_num_threads = _threads()
            so.inter_op_num_threads = 1
            from backend.audio.transcriber import no_spin
            no_spin(so)
            _loaded = (ort.InferenceSession(path, so), WhisperFeatureExtractor(chunk_length=8))
            return _loaded
        except Exception as e:
            _failed = True
            _warn("Smart Turn unavailable: %s", e)
            return None


def available() -> bool:
    try:
        return _load() is not None
    except Exception:
        return False


def complete_probability(pcm: np.ndarray) -> float | None:
    """P(turn complete) from 16 kHz mono float32 audio, or None. Blocking."""
    try:
        loaded = _load()
        if loaded is None:
            return None
        session, fe = loaded
        pcm = np.asarray(pcm, dtype=np.float32)[-_WINDOW:]
        pcm = np.pad(pcm, (_WINDOW - len(pcm), 0))
        feats = fe(pcm, sampling_rate=_SR, return_tensors="np", padding="max_length",
                   max_length=_WINDOW, truncation=True, do_normalize=True
                   ).input_features.astype(np.float32)
        p = float(session.run(None, {"input_features": feats})[0].reshape(-1)[0])
        return min(1.0, max(0.0, p))
    except Exception as e:
        _warn("Smart Turn failed: %s", e)
        return None


def download() -> None:
    path = hf_hub_download(MODEL_REPO, MODEL_FILE)
    print(f"Smart Turn ready: {path}")
