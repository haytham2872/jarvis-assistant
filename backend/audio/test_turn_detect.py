import numpy as np
import pytest

from backend.audio import turn_detect as td

SR = 16000


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_loaded", None)
    monkeypatch.setattr(td, "_failed", False)
    monkeypatch.setattr(td, "_warned", False)


@pytest.mark.parametrize("p,ms", [(None, 1200), (0.95, 250), (0.7, 250), (0.5, 900),
                                  (0.3, 900), (0.1, 2000)])
def test_commit_after_ms(p, ms):
    assert td.commit_after_ms(p) == ms


class _FE:
    def __init__(self):
        self.seen = None

    def __call__(self, pcm, **kw):
        self.seen = np.array(pcm)

        class R:
            input_features = np.zeros((1, 80, 800), dtype=np.float32)
        return R()


class _Sess:
    def __init__(self, out=0.8, boom=False):
        self.out, self.boom = out, boom

    def run(self, _o, _feed):
        if self.boom:
            raise RuntimeError("bad")
        return [np.array([[self.out]], dtype=np.float32)]


def _install(monkeypatch, sess, fe):
    monkeypatch.setattr(td, "_load", lambda: (sess, fe))


def test_pads_short_audio_at_start(monkeypatch):
    fe = _FE()
    _install(monkeypatch, _Sess(), fe)
    pcm = np.ones(SR, dtype=np.float32)
    assert td.complete_probability(pcm) == pytest.approx(0.8)
    assert fe.seen.shape == (8 * SR,)
    assert not fe.seen[: 7 * SR].any() and fe.seen[7 * SR:].all()


def test_keeps_last_8s(monkeypatch):
    fe = _FE()
    _install(monkeypatch, _Sess(), fe)
    pcm = np.arange(10 * SR, dtype=np.float32)
    td.complete_probability(pcm)
    assert np.array_equal(fe.seen, pcm[-8 * SR:])


def test_failing_session_returns_none(monkeypatch):
    _install(monkeypatch, _Sess(boom=True), _FE())
    assert td.complete_probability(np.zeros(SR, np.float32)) is None


def test_clamped(monkeypatch):
    _install(monkeypatch, _Sess(out=1.7), _FE())
    assert td.complete_probability(np.zeros(SR, np.float32)) == 1.0
    _install(monkeypatch, _Sess(out=-0.4), _FE())
    assert td.complete_probability(np.zeros(SR, np.float32)) == 0.0


def test_missing_model(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(td, "hf_hub_download", boom)
    assert td.available() is False
    assert td.complete_probability(np.zeros(SR, np.float32)) is None


def test_real_model_smoke():
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download(td.MODEL_REPO, td.MODEL_FILE, local_files_only=True)
    except Exception:
        pytest.skip("Smart Turn model not in local cache")
    p = td.complete_probability(np.zeros(2 * SR, np.float32))
    assert isinstance(p, float) and 0.0 <= p <= 1.0
