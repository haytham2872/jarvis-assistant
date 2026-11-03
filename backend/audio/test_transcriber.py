"""Speech to text (4 October 2026): Parakeet for Discord voice notes and the desk microphone,
English and French, with Whisper as the fallback. The old setup forced every note to English:
measured, French came out as invented English (100-104% word error)."""
import inspect
import os
import subprocess
import sys
import threading

import numpy as np
import pytest

from backend.audio import transcriber as T

SECOND = np.zeros(T.SAMPLE_RATE, dtype=np.float32)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("STT_ENGINE", raising=False)
    T._run, T._kind, T._last_used = None, "", 0.0
    yield
    if T._timer is not None:
        T._timer.cancel()
    T._run, T._kind = None, ""


def fake_engines(monkeypatch, parakeet_fails=False):
    loads = []

    def parakeet():
        loads.append("parakeet")
        if parakeet_fails:
            raise RuntimeError("model not downloaded")
        return lambda pcm: "Salut Jarvis, check my calendar"

    def whisper():
        loads.append("whisper")
        return lambda pcm: "whisper text"
    monkeypatch.setattr(T, "_load_parakeet", parakeet)
    monkeypatch.setattr(T, "_load_whisper", whisper)
    return loads


def test_parakeet_loads_once_on_the_first_note_and_is_reused(monkeypatch):
    loads = fake_engines(monkeypatch)
    assert T.engine() == ""                                # nothing loaded before a note
    assert T.transcribe(SECOND) == "Salut Jarvis, check my calendar"
    assert T.transcribe(SECOND) == "Salut Jarvis, check my calendar"
    assert loads == ["parakeet"] and T.engine() == "parakeet"


def test_whisper_takes_over_when_parakeet_cannot_load(monkeypatch):
    loads = fake_engines(monkeypatch, parakeet_fails=True)
    assert T.transcribe(SECOND) == "whisper text"
    assert loads == ["parakeet", "whisper"] and T.engine() == "whisper"


def test_stt_engine_whisper_skips_parakeet(monkeypatch):
    loads = fake_engines(monkeypatch)
    monkeypatch.setenv("STT_ENGINE", "whisper")
    T.transcribe(SECOND)
    assert loads == ["whisper"]


def test_a_click_shorter_than_three_tenths_of_a_second_loads_nothing(monkeypatch):
    loads = fake_engines(monkeypatch)
    assert T.transcribe(np.zeros(T.SAMPLE_RATE // 5, dtype=np.float32)) == ""
    assert loads == []


def test_release_gives_the_memory_back_and_the_next_note_reloads(monkeypatch):
    loads = fake_engines(monkeypatch)
    T.transcribe(SECOND)
    assert T.release() is True and T.engine() == ""
    assert T.release() is False                            # nothing left to release
    T.transcribe(SECOND)
    assert loads == ["parakeet", "parakeet"]


def test_idle_release_waits_for_ten_quiet_minutes(monkeypatch):
    fake_engines(monkeypatch)
    T.transcribe(SECOND)
    T._release_if_idle()                                   # just used: kept
    assert T.engine() == "parakeet"
    T._last_used -= T._IDLE_RELEASE_S
    T._release_if_idle()
    assert T.engine() == ""
    assert T._IDLE_RELEASE_S == 600.0


def test_a_note_being_transcribed_is_never_released_under_it(monkeypatch):
    fake_engines(monkeypatch)
    T.transcribe(SECOND)
    with T._lock:                                          # a transcription in progress
        assert T.release() is False
    assert T.engine() == "parakeet"


def test_every_note_schedules_the_idle_release(monkeypatch):
    fake_engines(monkeypatch)
    T.transcribe(SECOND)
    first = T._timer
    T.transcribe(SECOND)
    assert first is not T._timer and first.finished.is_set()    # the earlier countdown cancelled
    assert T._timer.daemon and T._timer.interval == T._IDLE_RELEASE_S


def test_parakeet_runs_without_the_onnxruntime_arena(monkeypatch):
    """Measured: arena on, +1,070 MB; off, +700 MB. The server has 2.2 GB free."""
    seen = {}

    class Model:
        def with_vad(self, vad):
            return self

        def recognize(self, pcm, sample_rate):
            return [type("S", (), {"text": " bonjour "})(), type("S", (), {"text": "  "})()]

    class FakeOnnxAsr:
        @staticmethod
        def load_model(name, quantization=None, sess_options=None):
            seen.update(name=name, q=quantization, so=sess_options)
            return Model()

        @staticmethod
        def load_vad(name, sess_options=None):
            return object()
    monkeypatch.setitem(sys.modules, "onnx_asr", FakeOnnxAsr)
    run = T._load_parakeet()
    assert run(SECOND) == "bonjour"                        # blank VAD segments dropped
    assert seen["name"] == "nemo-parakeet-tdt-0.6b-v3" and seen["q"] == "int8"
    assert seen["so"].enable_cpu_mem_arena is False and seen["so"].enable_mem_pattern is False


def test_threads_follow_the_machine_or_stt_threads(monkeypatch):
    monkeypatch.setenv("STT_THREADS", "2")
    assert T._threads() == 2
    monkeypatch.setenv("STT_THREADS", "")
    assert 1 <= T._threads() <= 4


def test_an_ogg_opus_voice_note_decodes_to_16khz_mono():
    import av
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "note.ogg")
    out = av.open(path, "w", format="ogg")
    stream = out.add_stream("libopus", rate=48000)
    stream.layout = "mono"
    tone = (np.sin(np.linspace(0, 2 * np.pi * 440 * 2, 96000)) * 8000).astype(np.int16)
    frame = av.AudioFrame.from_ndarray(tone[None, :], format="s16", layout="mono")
    frame.sample_rate = 48000
    for p in stream.encode(frame):
        out.mux(p)
    for p in stream.encode(None):
        out.mux(p)
    out.close()
    pcm = T.decode(path)
    assert pcm.dtype == np.float32 and pcm.ndim == 1
    assert abs(len(pcm) / T.SAMPLE_RATE - 2.0) < 0.1        # two seconds of audio
    assert 0.05 < float(np.abs(pcm).max()) <= 1.0


def test_nothing_forces_english_any_more():
    from backend.audio import stt
    assert 'language="en"' not in inspect.getsource(stt)
    assert 'language="en"' not in inspect.getsource(T)
    assert "WhisperModel" not in inspect.getsource(stt)    # the desk mic uses the shared engine


def test_discord_voice_notes_go_straight_to_the_transcriber(monkeypatch):
    import backend.main as main
    monkeypatch.setattr(T, "transcribe_file", lambda path: f"heard {path}")
    monkeypatch.setattr(main, "get_stt", lambda: (_ for _ in ()).throw(AssertionError("desk STT built")))
    assert main.transcribe_file("x.ogg") == "heard x.ogg"


def test_importing_the_transcriber_loads_no_model_library():
    code = ("import sys, backend.audio.transcriber; "
            "print(any(m in sys.modules for m in ('onnx_asr', 'faster_whisper', 'onnxruntime', 'av')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert out.stdout.strip() == "False", out.stderr
