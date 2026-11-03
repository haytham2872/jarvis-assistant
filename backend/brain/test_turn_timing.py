"""The per-turn timing line: where the time before the first model call goes.

On the server, the stage before a Discord turn's first model call was a 2.0s median (19% of
turn time) and the runtime database could not say which part: it records model and tool
calls, nothing before them. chat() now logs one line per turn naming the embed, retrieval
(and its slowest sources), the wait on Jev, assembly, depth, tier and reranker cost.
"""
import asyncio
import logging
import time

import pytest

from backend.brain import engine, rerank
from backend.brain.memory import RecallResult
from backend.brain.threads import SessionDecision
from backend.context import deadlines


# --- deadlines.bounded records each source's duration -------------------------------------

def test_bounded_records_duration_of_each_source():
    async def slow():
        await asyncio.sleep(0.05)
        return "x"

    async def failing():
        raise RuntimeError("store down")

    async def hangs():
        await asyncio.sleep(5)

    async def run():
        deadlines.reset()
        await asyncio.gather(
            deadlines.bounded(slow(), source_id="slow", timeout=1.0),
            deadlines.bounded(failing(), source_id="failing", timeout=1.0),
            deadlines.bounded(hangs(), source_id="hangs", timeout=0.1),
        )
        return deadlines.durations()

    d = asyncio.run(run())
    assert set(d) == {"slow", "failing", "hangs"}
    assert 0.04 <= d["slow"] < 0.5
    assert d["failing"] < 0.05
    assert 0.09 <= d["hangs"] < 0.5          # a timeout counts its deadline, not 5s
    deadlines.reset()
    assert deadlines.durations() == {}


# --- the reranker counts what it scored ------------------------------------------------------

class _FakeModel:
    def predict(self, pairs, **_):
        time.sleep(0.01)
        return [1.0] * len(pairs)


def test_rerank_counts_calls_pairs_and_time(monkeypatch):
    monkeypatch.setattr(rerank, "_get_model", lambda: _FakeModel())
    monkeypatch.setenv("JARVIS_RERANK", "1")
    rerank.reset_turn()
    rerank.gate_sync("q", [{"text": "a", "score": 0.9}, {"text": "b", "score": 0.8}])
    rerank.gate_sync("q", [{"text": "c", "score": 0.9}])
    stats = rerank.turn_stats()
    assert stats["calls"] == 2 and stats["pairs"] == 3 and stats["seconds"] >= 0.015
    rerank.reset_turn()
    assert rerank.turn_stats() == {"calls": 0, "pairs": 0, "seconds": 0.0}


def test_rerank_counts_a_failed_scoring_too(monkeypatch):
    class Broken:
        def predict(self, pairs, **_):
            raise RuntimeError("boom")
    monkeypatch.setattr(rerank, "_get_model", lambda: Broken())
    monkeypatch.setenv("JARVIS_RERANK", "1")
    rerank.reset_turn()
    assert rerank.gate_sync("q", [{"text": "a"}]) is None
    assert rerank.turn_stats()["calls"] == 1


# --- the line itself -----------------------------------------------------------------------------

def test_timing_line_splits_the_stages():
    line = engine._timing_line(
        10.0, 10.05, 12.15, 12.15, 12.31, deep=True, thinking=True, jev="answered",
        sources={"recall": 2.1, "vault:topic": 1.9, "vault:queue": 0.001},
        rerank_stats={"calls": 3, "pairs": 30, "seconds": 6.0})
    assert "model call at 2.31s" in line
    assert "embed 0.05s" in line and "retrieval 2.10s" in line
    assert "[recall 2.10s, vault:topic 1.90s]" in line          # fast sources left out
    assert "jev wait 0.00s (answered)" in line and "assembly 0.16s" in line
    assert "deep=yes tier=thinking" in line
    assert "rerank 3 calls, 30 pairs" in line


def test_timing_line_omits_rerank_when_nothing_was_scored():
    line = engine._timing_line(0.0, 0.05, 0.07, 0.4, 0.42, deep=False, thinking=False,
                               jev="missed", sources={}, rerank_stats={"calls": 0})
    assert "rerank" not in line and "retrieval 0.02s," in line and "(missed)" in line


# --- chat() logs it, with the real stages --------------------------------------------------

def test_chat_logs_one_timing_line_naming_the_slow_source(monkeypatch, caplog):
    async def classify(_text):
        return None

    async def slow_recall(*a, **k):
        await asyncio.sleep(0.2)
        return RecallResult()

    async def session(*a, **k):
        return SessionDecision(session_id="s-timing", ended_session_id=None)

    async def empty(*a, **k):
        return ""

    reached = {}

    async def fake_compat(model, system, messages, *a, **k):
        reached["model"] = True
        return "Okay."

    async def noop(*a, **k):
        return None

    async def send(_m):
        pass

    monkeypatch.setattr(engine.intent, "classify", classify)
    monkeypatch.setattr(engine, "recall", slow_recall)
    monkeypatch.setattr(engine, "assign_session", session)
    for name in ("_read_owner", "_read_latest_letter", "_read_queue", "_read_relevant_topic"):
        monkeypatch.setattr(engine, name, empty)
    monkeypatch.setattr(engine.predictions, "read_pending_block", empty)
    monkeypatch.setattr(engine.turn_signals, "start", lambda _t: None)
    monkeypatch.setattr(engine, "_chat_compat", fake_compat)
    monkeypatch.setattr(engine, "_ingest_mem", noop)
    monkeypatch.setattr(engine, "persist_mood", noop)
    monkeypatch.setattr(engine, "_record_thread_task", noop)

    with caplog.at_level(logging.INFO, logger="backend.brain.engine"):
        out = asyncio.run(engine.chat("what time is it", [], send, None, channel="discord"))

    assert out == "Okay." and reached
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn timing:")]
    assert len(lines) == 1
    line = lines[0]
    assert "[recall 0.2" in line                      # the slow source is named
    assert "retrieval 0.2" in line                   # and the stage it belongs to
    assert "(off)" in line                           # Jev disabled here, said so
    secs = float(line.split("model call at ")[1].split("s")[0])
    assert secs >= 0.2
