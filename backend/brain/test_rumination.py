"""Tests for backend/brain/rumination.py — l'esprit de l'escalier.

Pure-function tests for eligibility/cleanup, plus monkeypatched
integration tests for the generation + pop-once delivery path. No real
Qdrant or LLM call is ever made. Run with:
    .venv\\Scripts\\pytest backend/brain/test_rumination.py -v
"""

import asyncio

import pytest

from backend.brain import rumination as rum
from backend.tools import vault as vault_mod


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(rum, "_STATE_FILE", tmp_path / "rumination.json")
    monkeypatch.setattr(rum, "_VAULT", tmp_path / "jarvis-vault")
    # vault_write() (used to persist ruminations.md) resolves paths through
    # its OWN module-level _VAULT constant, independent of rumination.py's —
    # both must point at the tmp vault or a "real" write escapes into the
    # actual project vault.
    monkeypatch.setattr(vault_mod, "_VAULT", tmp_path / "jarvis-vault")
    for folder in vault_mod.FOLDERS:
        (tmp_path / "jarvis-vault" / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _thread(ts, sig=0.6, high=True, summary=""):
    return {
        "timestamp": ts, "significance": sig, "high": high,
        "user": "hello", "assistant": "world", "summary": summary,
    }


# ---------------------------------------------------------------------------
# _eligible — pure gate
# ---------------------------------------------------------------------------

def test_eligible_rejects_too_few_threads():
    threads = [_thread(100), _thread(200)]
    ok, reason = rum._eligible(threads, now=1000)
    assert not ok
    assert "too few" in reason


def test_eligible_rejects_all_low_significance():
    threads = [_thread(100, sig=0.1), _thread(200, sig=0.2), _thread(300, sig=0.3)]
    ok, reason = rum._eligible(threads, now=1000)
    assert not ok
    assert "nothing significant" in reason


def test_eligible_rejects_stale_session():
    threads = [_thread(100, sig=0.8), _thread(200, sig=0.8), _thread(300, sig=0.8)]
    now = 300 + rum._MAX_AGE_S + 1
    ok, reason = rum._eligible(threads, now=now)
    assert not ok
    assert "too old" in reason


def test_eligible_passes_good_session():
    threads = [_thread(100, sig=0.8), _thread(200, sig=0.3), _thread(300, sig=0.7)]
    ok, reason = rum._eligible(threads, now=400)
    assert ok
    assert reason == ""


def test_eligible_rejects_empty_threads():
    ok, reason = rum._eligible([], now=1000)
    assert not ok


# ---------------------------------------------------------------------------
# _clean_thought — pure cleanup
# ---------------------------------------------------------------------------

def test_clean_thought_nothing_becomes_empty():
    assert rum._clean_thought("NOTHING") == ""
    assert rum._clean_thought("nothing") == ""
    assert rum._clean_thought("  NOTHING.  ") == ""


def test_clean_thought_empty_input():
    assert rum._clean_thought("") == ""
    assert rum._clean_thought(None) == ""


def test_clean_thought_strips_code_fences():
    raw = "```\nI keep thinking about what he said.\n```"
    assert rum._clean_thought(raw) == "I keep thinking about what he said."


def test_clean_thought_strips_quotes():
    assert rum._clean_thought('"A real thought."') == "A real thought."


def test_clean_thought_trims_long_text_at_sentence_boundary():
    sentence = "This is a real afterthought about the conversation. "
    raw = sentence * 40  # well over _THOUGHT_MAX_CHARS
    cleaned = rum._clean_thought(raw)
    assert len(cleaned) <= rum._THOUGHT_MAX_CHARS
    assert cleaned.endswith(".")


# ---------------------------------------------------------------------------
# pop_rumination_mention — exactly once
# ---------------------------------------------------------------------------

def test_pop_rumination_mention_empty_when_no_pending(tmp_state):
    assert rum.pop_rumination_mention() == ""


def test_pop_rumination_mention_delivers_once(tmp_state):
    rum._save_state({"pending": {"sid": "abcd1234", "text": "a real thought", "created": 1000}})
    first = rum.pop_rumination_mention()
    assert "a real thought" in first
    assert rum._RUMINATION_FRAME in first
    second = rum.pop_rumination_mention()
    assert second == ""


# ---------------------------------------------------------------------------
# _try_ruminate_channel — integration, everything monkeypatched
# ---------------------------------------------------------------------------

def test_try_ruminate_channel_generates_once(tmp_state, monkeypatch):
    session_threads = [_thread(100, sig=0.8), _thread(200, sig=0.3), _thread(300, sig=0.7)]
    last_thread = {**session_threads[-1], "session_id": "sess-1"}

    async def fake_recent_threads(limit, channel=None):
        return [last_thread]

    async def fake_threads_for_session(sid):
        assert sid == "sess-1"
        return session_threads

    calls = {"llm": 0}

    async def fake_llm(prompt, max_tokens):
        calls["llm"] += 1
        return "I keep thinking about what he said about X."

    monkeypatch.setattr(rum, "recent_threads", fake_recent_threads)
    monkeypatch.setattr(rum, "threads_for_session", fake_threads_for_session)
    monkeypatch.setattr("backend.brain.research._llm", fake_llm)

    now = 300 + rum._IDLE_MIN_S + 1
    asyncio.run(rum._try_ruminate_channel("local", now))

    assert calls["llm"] == 1
    state = rum._load_state()
    assert state["pending"]["sid"] == "sess-1"
    assert "I keep thinking about what he said about X." in state["pending"]["text"]
    assert "sess-1" in state["ruminated"]

    ruminations_file = tmp_state / "jarvis-vault" / "self" / "ruminations.md"
    assert ruminations_file.exists()
    assert "I keep thinking about what he said about X." in ruminations_file.read_text(encoding="utf-8")


def test_try_ruminate_channel_second_tick_noops(tmp_state, monkeypatch):
    session_threads = [_thread(100, sig=0.8), _thread(200, sig=0.3), _thread(300, sig=0.7)]
    last_thread = {**session_threads[-1], "session_id": "sess-1"}

    async def fake_recent_threads(limit, channel=None):
        return [last_thread]

    async def fake_threads_for_session(sid):
        return session_threads

    calls = {"llm": 0}

    async def fake_llm(prompt, max_tokens):
        calls["llm"] += 1
        return "a thought"

    monkeypatch.setattr(rum, "recent_threads", fake_recent_threads)
    monkeypatch.setattr(rum, "threads_for_session", fake_threads_for_session)
    monkeypatch.setattr("backend.brain.research._llm", fake_llm)

    now = 300 + rum._IDLE_MIN_S + 1
    asyncio.run(rum._try_ruminate_channel("local", now))
    assert calls["llm"] == 1

    # pop the pending slot so the second call isn't blocked by the "one
    # pending slot" rule -- we want to isolate the "already ruminated" gate
    rum.pop_rumination_mention()

    asyncio.run(rum._try_ruminate_channel("local", now + 10))
    assert calls["llm"] == 1  # still 1 -- sess-1 already in the ruminated set


def test_try_ruminate_channel_pending_slot_blocks_second_channel(tmp_state, monkeypatch):
    rum._save_state({"pending": {"sid": "other-sess", "text": "already pending", "created": 1000}})

    calls = {"recent": 0}

    async def fake_recent_threads(limit, channel=None):
        calls["recent"] += 1
        return [{"timestamp": 100, "session_id": "sess-2"}]

    monkeypatch.setattr(rum, "recent_threads", fake_recent_threads)

    asyncio.run(rum._try_ruminate_channel("discord", now=100000))
    # Blocked before even checking recent threads
    assert calls["recent"] == 0
    state = rum._load_state()
    assert state["pending"]["sid"] == "other-sess"  # unchanged


def test_try_ruminate_channel_ineligible_session_marks_without_llm_call(tmp_state, monkeypatch):
    session_threads = [_thread(100), _thread(200)]  # too few threads
    last_thread = {**session_threads[-1], "session_id": "sess-thin"}

    async def fake_recent_threads(limit, channel=None):
        return [last_thread]

    async def fake_threads_for_session(sid):
        return session_threads

    calls = {"llm": 0}

    async def fake_llm(prompt, max_tokens):
        calls["llm"] += 1
        return "should never be called"

    monkeypatch.setattr(rum, "recent_threads", fake_recent_threads)
    monkeypatch.setattr(rum, "threads_for_session", fake_threads_for_session)
    monkeypatch.setattr("backend.brain.research._llm", fake_llm)

    now = 200 + rum._IDLE_MIN_S + 1
    asyncio.run(rum._try_ruminate_channel("local", now))

    assert calls["llm"] == 0
    state = rum._load_state()
    assert "sess-thin" in state["ruminated"]
    assert state.get("pending") is None


def test_state_survives_simulated_restart(tmp_state):
    rum._save_state({"pending": {"sid": "abcd1234", "text": "survives restart", "created": 1000},
                      "ruminated": ["abcd1234"]})
    # simulate restart: reload from disk fresh
    reloaded = rum._load_state()
    assert reloaded["pending"]["text"] == "survives restart"
    assert reloaded["ruminated"] == ["abcd1234"]
