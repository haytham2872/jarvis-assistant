"""A machine that cannot play audio answers in text instead of failing the turn.

Found by the fresh-install test: on a headless Linux server a typed turn (the terminal
client) failed with "PortAudio library not found" because it tried to start desk speech.
"""
import sys
import types

from backend import main


def _broken_audio(monkeypatch):
    fake = types.ModuleType("backend.audio.tts")

    class TextToSpeech:
        def __init__(self):
            raise OSError("PortAudio library not found")
    fake.TextToSpeech = TextToSpeech
    monkeypatch.setitem(sys.modules, "backend.audio.tts", fake)
    monkeypatch.setattr(main, "_tts", None)
    monkeypatch.setattr(main, "_tts_unavailable", False)
    monkeypatch.setattr(main, "_no_tts", False)


def test_no_audio_means_no_speech_not_a_failed_turn(monkeypatch):
    _broken_audio(monkeypatch)
    assert main.get_tts() is None
    assert main._tts_unavailable is True


def test_the_failure_is_not_retried_on_every_turn(monkeypatch):
    _broken_audio(monkeypatch)
    calls = []
    real = sys.modules["backend.audio.tts"].TextToSpeech

    class Counting(real):
        def __init__(self):
            calls.append(1)
            super().__init__()
    sys.modules["backend.audio.tts"].TextToSpeech = Counting
    main.get_tts()
    main.get_tts()
    assert calls == [1]


def test_the_turn_drains_the_sentences_when_there_is_no_speech():
    import inspect
    src = inspect.getsource(main._execute_turn_once)
    assert "tts = get_tts() if want_tts else None" in src
    assert "if tts is None:\n            await _drain_sentence_queue(sentence_queue)" in src
