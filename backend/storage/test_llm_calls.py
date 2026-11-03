"""H0: every LLM call leaves a row saying what it asked for and what it produced.

The rows are what the thinking-headroom roadmap sizes its output limits from, so the
tests pin the fields that decision rests on: reasoning tokens (never logged before),
`finish_reason`, the effort actually sent, which host served the call, and whether the
leak-recovery call fired. The live path is driven through the real
`_run_stream_openai`/`_chat_compat` with only the provider client faked.
"""
import ast
import asyncio
import pathlib
import time
import types

import pytest

from backend.brain import engine, escalation, router
from backend.storage import llm_calls, operations


def run(coro):
    return asyncio.run(coro)


async def _noop_send(msg):
    pass


# --- fakes: an OpenAI-compatible streaming client ---------------------------------------

class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _Client:
    """Returns one scripted stream per create() call, in order."""
    def __init__(self, *streams):
        self._streams = list(streams)
        self.calls = []
        self.chat = types.SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self._streams.pop(0))


def _usage(prompt=100, completion=50, reasoning=30, cached=10, cost=0.001):
    return types.SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, cost=cost,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=types.SimpleNamespace(reasoning_tokens=reasoning),
    )


def _chunk(content=None, reasoning=None, finish=None, usage=None, provider="Together"):
    choices = []
    if content is not None or reasoning is not None or finish is not None:
        delta = types.SimpleNamespace(content=content, reasoning=reasoning,
                                      reasoning_content=None, tool_calls=None)
        choices = [types.SimpleNamespace(delta=delta, finish_reason=finish)]
    return types.SimpleNamespace(choices=choices, usage=usage, provider=provider)


def _answer_stream(text="the answer", thought="thinking", finish="stop", **usage_kw):
    return [_chunk(reasoning=thought), _chunk(content=text, finish=finish),
            _chunk(usage=_usage(**usage_kw))]


# --- effort_of / usage_fields --------------------------------------------------------------

def test_effort_is_read_from_the_body_actually_sent():
    p = router.OPENROUTER
    assert llm_calls.effort_of(router.with_provider_prefs(p.thinking_body, p)) == "high"
    assert llm_calls.effort_of(router.with_provider_prefs(p.no_thinking_body, p)) == "none"
    assert llm_calls.effort_of(escalation.MAX_EFFORT_BODY) == "max"
    assert llm_calls.effort_of(None) == "default"


def test_usage_fields_split_reasoning_from_answer():
    f = llm_calls.usage_fields(_usage(completion=500, reasoning=420))
    assert (f["output_tokens"], f["reasoning_tokens"], f["answer_tokens"]) == (500, 420, 80)
    assert f["cached_tokens"] == 10 and f["cost"] == 0.001


def test_missing_reasoning_is_unknown_not_zero():
    """A provider that does not report reasoning tokens must not look like one that
    generated none -- the percentiles would read far lower than reality."""
    u = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    assert llm_calls.usage_fields(u)["reasoning_tokens"] is None


def test_anthropic_usage_shape_is_read():
    u = types.SimpleNamespace(input_tokens=7, output_tokens=3, cache_read_input_tokens=2)
    f = llm_calls.usage_fields(u)
    assert (f["input_tokens"], f["output_tokens"], f["cached_tokens"]) == (7, 3, 2)


def test_record_never_raises_on_garbage():
    llm_calls.record(model="m", started_at=time.time(), usage=object(), extra_body="nonsense")
    llm_calls.record(model="m", started_at="not a time")          # swallowed


# --- record / labelled -----------------------------------------------------------------------

def test_record_roundtrip_carries_run_and_label():
    token = operations.current_run_id.set("run-1")
    try:
        with llm_calls.labelled("research.synthesis"):
            llm_calls.record(model="m", started_at=time.time() - 2, usage=_usage(),
                             finish_reason="length", max_tokens=8000, host="Novita",
                             outcome=llm_calls.ANSWER)
    finally:
        operations.current_run_id.reset(token)
    (row,) = llm_calls.recent()
    assert row["purpose"] == "research.synthesis" and row["run_id"] == "run-1"
    assert row["finish_reason"] == "length" and row["max_tokens"] == 8000
    assert row["host"] == "Novita" and row["latency_ms"] >= 2000


def test_label_does_not_leak_past_its_block():
    with llm_calls.labelled("brief"):
        pass
    llm_calls.record(model="m", started_at=time.time())
    assert llm_calls.recent()[0]["purpose"] == "unlabelled"


# --- the live path ---------------------------------------------------------------------------

def test_a_live_call_records_reasoning_host_and_limit():
    client = _Client(_answer_stream(completion=900, reasoning=800))
    body = router.with_provider_prefs(router.OPENROUTER.thinking_body, router.OPENROUTER)
    run(engine._run_stream_openai(client, "m", [], _noop_send, None, extra_body=body,
                                  iteration=3, plan_active=True))
    (row,) = llm_calls.recent()
    assert row["purpose"] == "chat" and row["effort"] == "high"
    assert row["reasoning_tokens"] == 800 and row["answer_tokens"] == 100
    assert row["host"] == "Together" and row["finish_reason"] == "stop"
    assert row["max_tokens"] == engine._OUTPUT_LIMITS["high"] == 64000
    assert row["iteration"] == 3 and row["plan_active"] == 1
    assert row["outcome"] == llm_calls.ANSWER


def test_the_leak_recovery_is_counted(monkeypatch):
    """The 2026-09-28 shape: the thinking tier reasons to its limit with no answer, and a
    direct call recovers. Both calls are recorded, the second as a recovery."""
    client = _Client(
        [_chunk(reasoning="let me re-read..."), _chunk(finish="length"),
         _chunk(usage=_usage(completion=16000, reasoning=16000))],
        _answer_stream(text="The conclusion.", thought=None, reasoning=0),
    )
    monkeypatch.setattr(engine, "_get_compat_client", lambda: client)
    out = run(engine._chat_compat(
        router.OPENROUTER_THINKING, "system", [{"role": "user", "content": "compile it"}],
        _noop_send, None, user_text="", use_thinking=True,
    ))
    assert out == "The conclusion."
    first, second = llm_calls.recent()
    assert first["finish_reason"] == "length" and first["outcome"] == llm_calls.EMPTY
    assert first["effort"] == "high" and first["reasoning_tokens"] == 16000
    assert second["purpose"] == llm_calls.RECOVERY and second["effort"] == "none"
    assert "no-answer recoveries: 1" in "\n".join(llm_calls.status_lines())


def test_a_cheap_tier_call_is_attributed_to_its_caller(monkeypatch):
    from backend.brain import research
    from backend.providers import gateway

    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(
            finish_reason="length",
            message=types.SimpleNamespace(content='{"x": 1', tool_calls=None))],
        usage=_usage(completion=8000, reasoning=0), provider="BaseTen")

    class _NonStream:
        def __init__(self):
            self.chat = types.SimpleNamespace(completions=self)

        async def create(self, **kw):
            return resp

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(research, "_provider", lambda: router.OPENROUTER)
    monkeypatch.setattr(gateway, "get_async_client", lambda p: _NonStream())
    with llm_calls.labelled("research.synthesis"):
        run(research._llm("prompt", max_tokens=8000))
    (row,) = llm_calls.recent()
    assert row["purpose"] == "research.synthesis" and row["effort"] == "none"
    assert row["finish_reason"] == "length" and row["host"] == "BaseTen"
    assert row["max_tokens"] == 8000


# --- status ----------------------------------------------------------------------------------

def _row(effort, reasoning, purpose="chat", finish="stop", ago=0):
    llm_calls.record(model="m", started_at=time.time() - ago, purpose=purpose,
                     effort=effort, usage=_usage(completion=reasoning + 10, reasoning=reasoning),
                     finish_reason=finish)


def test_status_reports_percentiles_by_effort():
    for n in range(1, 21):
        _row("high", n * 100)
    _row("none", 0)
    line = llm_calls.status_lines()[0]
    assert "high p50 1,100 / p95 1,900 (n=20)" in line
    assert "none" not in line          # reasoning off is not a reasoning measurement


def test_status_names_calls_that_hit_their_limit():
    _row("none", 0, purpose="research.synthesis", finish="length")
    lines = "\n".join(llm_calls.status_lines())
    assert "research.synthesis 1" in lines and "no-answer recoveries: 0" in lines


def test_status_ignores_rows_older_than_a_day_and_is_quiet_when_empty():
    assert llm_calls.status_lines() == []
    _row("high", 500, ago=2 * 86400)
    assert llm_calls.status_lines() == []


def test_runtime_status_shows_the_line():
    from backend.brain import runtime_status
    _row("high", 500)
    assert "Reasoning tokens per call (24h): high p50 500" in runtime_status.runtime_status_text()


# --- structural guard ------------------------------------------------------------------------

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_EXEMPT = {
    # Jev is a classifier on OpenRouter's decisions endpoint, not a generation call.
    ("backend/brain/turn_signals.py", "*"),
}


def _llm_call_functions():
    """(file, function) for every function that calls a chat-completions or Anthropic
    messages endpoint, excluding tests and the gateway itself."""
    found = []
    for path in _BACKEND.rglob("*.py"):
        rel = path.relative_to(_BACKEND.parent).as_posix()
        if "/test_" in rel or rel.endswith("conftest.py") or rel.startswith("backend/providers/gateway"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            src = ast.unparse(fn)
            direct = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("create", "stream")
                      and isinstance(n.func.value, ast.Attribute)
                      and n.func.value.attr in ("completions", "messages")]
            if direct:
                found.append((rel, fn.name, src))
    return found


def test_every_llm_call_site_records_its_call():
    """H0's premise is "every call, not just foreground turns". A new call site that
    forgets to record would quietly drop out of the measurements."""
    missing = [
        f"{rel}:{name}" for rel, name, src in _llm_call_functions()
        if (rel, "*") not in _EXEMPT and (rel, name) not in _EXEMPT
        and "llm_calls" not in src
    ]
    assert missing == []


def test_the_guard_sees_the_known_call_sites():
    names = {(rel, name) for rel, name, _ in _llm_call_functions()}
    for expected in [("backend/brain/engine.py", "_run_stream_openai"),
                     ("backend/brain/research.py", "_llm"),
                     ("backend/brain/consolidation.py", "_call_compat"),
                     ("backend/providers/mem0_llm.py", "generate_response")]:
        assert expected in names


# --- H1: per-turn totals, /cost, the morning brief ------------------------------------------

def _turn_rows(run_id, preview, costs, out=1000):
    from backend.storage import runs
    token = operations.current_run_id.set(run_id)
    try:
        conn = runs._ensure()
        with conn:
            conn.execute("INSERT INTO runs (run_id, channel, state, started_at, user_preview)"
                         " VALUES (?, 'discord', 'completed', ?, ?)", (run_id, time.time(), preview))
        for c in costs:
            llm_calls.record(model="m", started_at=time.time(), purpose="chat",
                             usage=_usage(completion=out, reasoning=out - 100, cost=c))
    finally:
        operations.current_run_id.reset(token)


def test_turn_totals_sum_a_turns_calls_and_name_its_request():
    _turn_rows("r-deep", "compile the NVDA research into a conclusion", [0.2, 0.15], out=40000)
    _turn_rows("r-light", "hey", [0.001])
    deep, light = llm_calls.turn_totals()
    assert deep["run_id"] == "r-deep" and deep["calls"] == 2
    assert deep["output_tokens"] == 80000 and abs(deep["cost"] - 0.35) < 1e-9
    assert llm_calls.run_cost("r-light") == pytest.approx(0.001)
    lines = llm_calls.deepest_turns_lines()
    assert lines[0] == "Deepest turns (24h):" and "compile the NVDA research" in lines[1]


def test_only_turns_over_the_threshold_reach_the_brief():
    _turn_rows("r-deep", "compile the NVDA research", [0.2, 0.15])
    _turn_rows("r-cheap", "what's the weather", [0.01])
    text = llm_calls.expensive_turns_text()
    assert "compile the NVDA research" in text and "weather" not in text


def test_cost_report_adds_the_deepest_turns():
    from backend.brain import usage
    _turn_rows("r-deep", "compile it", [0.3])
    assert "Deepest turns (24h):" in usage.cost_report()


def test_the_brief_is_told_about_an_expensive_turn(monkeypatch):
    from backend.brain import chronos, research
    prompts = []

    async def fake_llm(prompt, max_tokens=0):
        prompts.append(prompt)
        return "Morning."
    monkeypatch.setattr(research, "_llm", fake_llm)
    monkeypatch.setattr(chronos, "_collect_calendar", lambda: _none())
    # An otherwise quiet night: the expensive turn alone must still reach the brief.
    for name in ("_collect_research_trail", "_collect_todays_reminders"):
        monkeypatch.setattr(chronos, name, lambda now: "")
    monkeypatch.setattr(chronos, "_collect_open_intents", lambda: "")
    _turn_rows("r-deep", "compile the NVDA research", [0.4])
    run(chronos.compose_brief(night_results=[]))
    assert "compile the NVDA research" in prompts[0]


async def _none():
    return ""


def test_the_expensive_turn_is_logged(caplog):
    import logging
    from backend import main
    _turn_rows("r-deep", "compile it", [0.4])
    with caplog.at_level(logging.WARNING):
        main._log_expensive_turn("r-deep", "compile it")
        main._log_expensive_turn(None, "hey")
    assert sum("Expensive turn" in r.message for r in caplog.records) == 1
