"""Jev turn signals: one classifier call per turn replacing the keyword triggers.

No network: the HTTP client is faked at the gateway, the same seam production uses.
"""
import asyncio
import inspect
import time

import pytest

from backend.brain import personality, router, turn_signals as ts
from backend.providers import gateway


def run(coro):
    return asyncio.run(coro)


def answers(**probs):
    """A Jev answer set: every question at 0.0 unless given, sections as a choice."""
    sections = probs.pop("sections", {"none": 1.0})
    out = {k: {"type": "noul", "noul": probs.get(k, 0.0)} for k in ts.QUESTIONS}
    out["sections"] = {"type": "choice", "choice": max(sections, key=sections.get),
                       "probabilities": sections, "confidence": 1.0}
    return out


# ------------------------------------------------------------------ interpretation
@pytest.mark.parametrize("name", list(ts.QUESTIONS))
def test_each_decision_fires_at_its_measured_threshold(name):
    thr = ts.THRESHOLDS[name]
    assert ts.interpret(answers(**{name: thr}), "hello").get(name)
    assert not ts.interpret(answers(**{name: thr - 0.01}), "hello").get(name)


def test_the_complex_threshold_is_the_precision_neutral_one():
    """0.4 missed the same 17/111 complex messages as the old gate while wasting the
    thinking tier on 18 instead of 85; 0.5 missed 33. Pinned so it can't drift up."""
    assert ts.THRESHOLDS["complex"] == 0.4


@pytest.mark.parametrize("name,text", [
    ("persistence", "remind me to call the bank"),
    ("correction", "from now on keep it short"),
    ("forecast", "what are the odds it rains"),
    ("recall_precision", "use the amount I gave you"),
])
def test_zero_false_fire_keyword_lists_still_count(name, text):
    assert personality_hit(name, text), "precondition: the keyword list matches"
    assert ts.interpret(answers(), text).get(name)


def personality_hit(name, text):
    return ts._keyword_hits(text)[name]


def test_noisy_keyword_decisions_follow_jev_only():
    """The opinion/what's-new/runtime lists fired wrongly 40/32/11 times on real
    messages -- a keyword hit there must not override Jev."""
    s = ts.interpret(answers(), "what do you think about the new update, how are you running")
    assert not s.get("opinion") and not s.get("whats_new") and not s.get("runtime")


def test_sections_are_the_top_two_above_the_floor_without_none():
    s = ts.interpret(answers(sections={"none": 0.5, "memory": 0.35, "tools": 0.31,
                                       "router": 0.29}), "x")
    assert s.sections == ["memory", "tools"]
    s = ts.interpret(answers(sections={"none": 0.9, "memory": 0.1}), "x")
    assert s.sections == []


def test_unknown_section_labels_are_ignored():
    s = ts.interpret(answers(sections={"made_up": 0.9, "memory": 0.6}), "x")
    assert s.sections == ["memory"]


def test_a_malformed_answer_raises_so_resolve_falls_back():
    bad = answers()
    del bad["opinion"]
    with pytest.raises(KeyError):
        ts.interpret(bad, "x")


# ------------------------------------------------------------------ the call
class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, data=None, delay=0.0, status=200):
        self.data = data or {"answers": answers(complex=0.9), "usage": {"input_tokens": 1400,
                                                                      "cost": 0.00006}}
        self.delay, self.status, self.calls = delay, status, []
        self.is_closed = False

    async def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        await asyncio.sleep(self.delay)
        return FakeResponse(self.data, self.status)


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setenv("JARVIS_JEV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    client = FakeClient()
    monkeypatch.setattr(gateway, "get_http_client", lambda: client)
    return client


def _turn(text, client_delay=0.0, fake=None):
    async def go():
        t0 = time.perf_counter()
        task = ts.start(text)
        return await ts.resolve(task, text, t0)
    return run(go())


def test_a_turn_resolves_to_jev_signals(jev_on):
    s = _turn("why does my research loop repeat, and how would you fix it?")
    assert s is not None and s.get("complex")
    call = jev_on.calls[0]
    assert call["url"] == router.JEV_URL and call["url"].endswith("/alpha/decisions")
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"]["model"] == router.JEV_MODEL


def test_the_model_is_pinned_to_a_version_not_the_rolling_alias():
    assert "latest" not in router.JEV_MODEL and "1.13" in router.JEV_MODEL


def test_yes_no_questions_send_both_criteria_as_openrouter_requires():
    body = ts._request_body("hi")
    for k in ts.QUESTIONS:
        q = body["questions"][k]
        assert q["type"] == "noul" and set(q["criteria"]) == {"true", "false"}
    assert body["questions"]["sections"]["type"] == "choice"
    assert "none" in body["questions"]["sections"]["criteria"]


def test_spend_is_recorded_with_openrouters_cost(jev_on, monkeypatch):
    from backend.brain import usage
    seen = []
    monkeypatch.setattr(usage, "record", lambda *a, **k: seen.append((a, k)))
    _turn("hello")
    (model, tokens, out), kw = seen[0]
    assert model == router.JEV_MODEL and tokens == 1400 and out == 0
    assert kw["cost"] == 0.00006


def test_a_late_answer_falls_back_and_is_abandoned(jev_on, monkeypatch):
    monkeypatch.setattr(ts, "DEADLINE_S", 0.05)
    jev_on.delay = 0.5
    t0 = time.perf_counter()
    assert _turn("hello") is None
    assert time.perf_counter() - t0 < 0.3          # did not wait for the slow call


def test_the_deadline_counts_from_the_start_of_the_turn(jev_on, monkeypatch):
    """Time spent in retrieval is already spent: resolve() only waits for what's left."""
    monkeypatch.setattr(ts, "DEADLINE_S", 0.2)
    jev_on.delay = 0.15

    async def go():
        t0 = time.perf_counter()
        task = ts.start("hello")
        await asyncio.sleep(0.1)                   # retrieval
        return await ts.resolve(task, "hello", t0)
    assert run(go()) is not None


def test_retrieval_time_is_deducted_from_the_wait(jev_on, monkeypatch):
    """If retrieval already used the whole budget, resolve() must not wait a full
    DEADLINE_S more -- that would put up to 0.8s back on every slow turn."""
    monkeypatch.setattr(ts, "DEADLINE_S", 0.3)
    jev_on.delay = 2.0

    async def go():
        t0 = time.perf_counter()
        task = ts.start("hello")
        await asyncio.sleep(0.3)                   # retrieval ate the whole budget
        out = await ts.resolve(task, "hello", t0)
        return out, time.perf_counter() - t0
    out, elapsed = run(go())
    assert out is None and elapsed < 0.45


@pytest.mark.parametrize("status,data", [(500, {}), (200, {"answers": {"complex": {}}})])
def test_http_errors_and_malformed_answers_fall_back(jev_on, status, data):
    jev_on.status, jev_on.data = status, data
    assert _turn("hello") is None


def test_disabled_unconfigured_or_empty_never_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "get_http_client", lambda: calls.append(1))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("JARVIS_JEV", "0")
    assert _turn("hello") is None
    monkeypatch.setenv("JARVIS_JEV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert _turn("hello") is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert _turn("   ") is None
    assert calls == []


def test_the_suite_keeps_jev_off_by_default():
    assert not router.jev_enabled()


# ------------------------------------------------------------------ the prompt
def _volatile(text, signals=None):
    return personality.build_system_prompt(text, signals=signals)[1]


def test_no_signals_renders_exactly_as_before():
    for text in ("remind me to call the bank", "what do you think about camus?", "hi"):
        assert personality.build_system_prompt(text) == personality.build_system_prompt(
            text, signals=None)


def test_signals_add_the_reminder_frames_the_keywords_missed():
    text = "Jot this down somewhere: the router password is on the fridge."
    assert personality._PERSISTENCE_FRAME not in _volatile(text)
    s = ts.interpret(answers(persistence=0.98), text)
    assert personality._PERSISTENCE_FRAME in _volatile(text, s)


def test_signals_silence_a_noisy_keyword_false_fire(monkeypatch):
    """'What's new with the neighbours?' fired the what's-new list and loaded evolution.md."""
    monkeypatch.setattr(personality, "_read_self_file", lambda f, label: "[evolution]")
    text = "What's new with the neighbours?"
    assert personality._EVOLUTION_FRAME in _volatile(text)
    assert personality._EVOLUTION_FRAME not in _volatile(text, ts.interpret(answers(), text))


def test_opinion_comes_from_signals(monkeypatch):
    monkeypatch.setattr(personality, "_load_positions", lambda: "## camus\n**Stance:** x")
    text = "Where do you stand on Camus versus Sartre?"
    assert personality._POSITIONS_FRAME in _volatile(text, ts.interpret(answers(opinion=0.9), text))
    assert personality._POSITIONS_FRAME not in _volatile(
        "what do you think about the weather", ts.interpret(answers(opinion=0.1), "x"))


def test_self_model_sections_come_from_signals(monkeypatch):
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda name: f"<{name} digest>")
    monkeypatch.setattr(personality, "_load_self_vault_files", lambda: "")
    s = ts.interpret(answers(sections={"memory": 0.8, "none": 0.2}), "x")
    out = personality._detect_self_model_sections("how does your recall work", None, s)
    assert "<memory digest>" in out and "<personality digest>" not in out
    s = ts.interpret(answers(sections={"personality": 0.7, "tools": 0.4}), "x")
    out = personality._detect_self_model_sections("who are you", None, s)
    assert "<personality digest>" in out and "<tools digest>" in out
    s = ts.interpret(answers(), "x")
    assert "digest>" not in personality._detect_self_model_sections(
        "use the self model, how does memory work", None, s)


# ------------------------------------------------------------------ the tier
def test_route_tier_takes_the_decision_it_is_given(monkeypatch):
    simple = "hi"
    assert router.route_tier(simple, None, complex_=True)[1] is True
    long_ = "x" * 400                                # the keyword floor would say complex
    assert router.route_tier(long_, None, complex_=False)[1] is False
    assert router.route_tier(long_, None)[1] is True   # unchanged without a decision


def test_chat_starts_jev_before_the_embedding_and_decides_the_tier_after_retrieval():
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    start = src.index("turn_signals.start(user_text)")
    assert start < src.index("await intent.classify(user_text)")
    gather = src.index("await asyncio.gather(")
    resolve = src.index("await turn_signals.resolve(")
    tier = src.index("route_tier(")
    assert gather < resolve < tier
    assert "complex_=signals.get(\"complex\")" in src
    assert "signals=signals" in src


def test_the_context_fallback_path_keeps_the_signals_too():
    from backend.brain import engine
    src = inspect.getsource(engine._build_prompt_via_context)
    assert src.count("signals=signals") == 2


def test_the_gateway_owns_and_closes_the_http_client(monkeypatch):
    gateway._reset_for_tests()
    c1 = gateway.get_http_client()
    assert gateway.get_http_client() is c1
    run(gateway.close_all())
    assert c1.is_closed and gateway.get_http_client() is not c1
    gateway._reset_for_tests()


def test_known_person_uses_its_measured_threshold_and_the_relation_words():
    """Measured on 336 real messages (8 about someone he knows): Jev alone at 0.5 caught
    3 with 11 false fires; at 0.65 OR'd with the relation-word list, 5 with 2."""
    assert ts.THRESHOLDS["known_person"] == 0.65
    assert ts.interpret(answers(known_person=0.1), "my sister Lina just got engaged").get("known_person")
    assert not ts.interpret(answers(known_person=0.6), "how are you feeling now?").get("known_person")


def test_warm_opens_the_connection_and_never_raises(monkeypatch):
    from backend.providers import gateway
    calls = []

    class C:
        async def get(self, url, **kw):
            calls.append(url)
            raise RuntimeError("offline")
    monkeypatch.setattr(gateway, "get_http_client", lambda: C())
    monkeypatch.setattr("backend.brain.router.jev_enabled", lambda: True)
    run(ts.warm())
    assert calls and calls[0].endswith("/models")
    import inspect
    from backend import main
    assert "spawn(turn_signals.warm())" in inspect.getsource(main)
