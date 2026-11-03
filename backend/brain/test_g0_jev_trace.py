"""G0: every Jev call leaves an llm_calls row (purpose "jev").

Before this, Jev's latency, cost and deadline misses existed only in process memory
(usage.record) and a debug log, so the server could not say how often Jev missed its
0.8-second deadline or what it costs. The goal question (G3) is to be measured against
this baseline. Routing is unchanged: these rows are written, never read by a turn.
"""
import asyncio
import time

import pytest

from backend.brain import turn_signals as ts
from backend.brain.test_turn_signals import FakeClient, jev_on  # noqa: F401  (fixture)
from backend.providers import gateway
from backend.storage import db, llm_calls, operations


def _rows():
    llm_calls._ensure()
    return [dict(r) for r in db.connect().execute(
        "SELECT run_id, purpose, outcome, finish_reason, input_tokens, cost, latency_ms"
        " FROM llm_calls WHERE purpose='jev'").fetchall()]


def _turn(text, run_id="run-1"):
    async def go():
        operations.current_run_id.set(run_id)
        t0 = time.perf_counter()
        return await ts.resolve(ts.start(text), text, t0)
    return asyncio.run(go())


def test_an_answered_call_is_recorded_with_its_cost_and_run(jev_on):
    assert _turn("why does my research loop repeat?") is not None
    [r] = _rows()
    assert (r["run_id"], r["outcome"], r["finish_reason"]) == ("run-1", "answer", "stop")
    assert r["input_tokens"] == 1400 and r["cost"] == pytest.approx(0.00006)


def test_a_deadline_miss_is_recorded(monkeypatch):
    monkeypatch.setenv("JARVIS_JEV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(ts, "DEADLINE_S", 0.05)
    monkeypatch.setattr(gateway, "get_http_client", lambda: FakeClient(delay=0.5))
    assert _turn("anything") is None
    [r] = _rows()
    assert r["finish_reason"] == "deadline" and r["outcome"] == "empty"


def test_an_http_error_is_recorded(monkeypatch):
    monkeypatch.setenv("JARVIS_JEV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(gateway, "get_http_client", lambda: FakeClient(status=502))
    assert _turn("anything") is None
    [r] = _rows()
    assert r["finish_reason"] == "error" and r["outcome"] == "empty"


def test_disabled_jev_records_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_JEV", "0")
    assert _turn("anything") is None
    assert _rows() == []
