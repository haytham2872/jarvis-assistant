"""A01 acceptance: a text-only startup must not initialize Whisper, TTS or PortAudio.

`main.py` used to run `stt = SpeechToText()` at module scope, loading the faster-whisper
model on every startup -- including the headless Linux/GCP deployment, which has no
microphone and only needs transcription for the occasional Discord voice note. Importing
`backend.audio.{stt,tts}` at module scope also pulled in `sounddevice` (PortAudio) on a
box that may not have it.

Run in a subprocess: the assertion is about what a *fresh* interpreter loads, which
cannot be observed once this test session has already imported things. That also keeps
the check honest if some other test imports an audio module first.
"""
import subprocess
import sys

import pytest

_PROBE = r"""
import os, sys, json
os.environ["NO_TTS"] = "1"
os.environ.setdefault("MODEL_MODE", "claude")
sys.path.insert(0, os.getcwd())
import backend.main as m
print(json.dumps({
    "faster_whisper": "faster_whisper" in sys.modules,
    "sounddevice": "sounddevice" in sys.modules,
    "audio_stt": "backend.audio.stt" in sys.modules,
    "audio_tts": "backend.audio.tts" in sys.modules,
    "stt_instance": m._stt is not None,
    "tts_instance": m._tts is not None,
    "transcribe_callable": callable(m.transcribe_file),
}))
"""


@pytest.fixture(scope="module")
def probe():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        pytest.fail(f"importing backend.main failed:\n{result.stderr[-3000:]}")
    import json
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_whisper_is_not_loaded_at_startup(probe):
    assert probe["faster_whisper"] is False
    assert probe["audio_stt"] is False
    assert probe["stt_instance"] is False


def test_portaudio_is_not_imported_at_startup(probe):
    """sounddevice binds PortAudio at import. A headless box may not have it, and a
    text-only runtime never needs it."""
    assert probe["sounddevice"] is False


def test_tts_is_not_constructed_at_startup(probe):
    assert probe["audio_tts"] is False
    assert probe["tts_instance"] is False


def test_voice_transcription_is_still_wired(probe):
    """Lazy must not mean absent -- the Discord bridge still gets a transcribe handler,
    and it constructs the engine on the first voice note rather than at startup."""
    assert probe["transcribe_callable"] is True


def test_main_no_longer_builds_audio_at_import_time():
    """CLAUDE.md long said main.py 'instantiates real audio hardware at import time and
    so is never imported directly by the test suite'. This test IS that import, and it
    passing is the evidence the constraint is gone."""
    import backend.main as m

    assert m._stt is None
    assert callable(m.get_stt)
    assert callable(m.get_tts)
