"""A01: Mem0 fact extraction runs on the provider's standard tier, reasoning disabled.

These cover the adapter's request shape (the reason it exists at all) and its usage
capture.
"""
import pytest

from backend.brain import memory
from backend.providers import mem0_llm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from backend.providers import gateway
    gateway._reset_for_tests()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    yield
    gateway._reset_for_tests()


def _adapter(monkeypatch, provider=None):
    from backend.brain.router import OPENROUTER
    provider = provider or OPENROUTER
    assert mem0_llm.register()
    monkeypatch.setattr("backend.brain.router.provider", lambda: provider)
    from mem0.utils.factory import LlmFactory
    return LlmFactory.create(
        mem0_llm.PROVIDER_NAME, {"model": provider.standard, "api_key": "sk-fake"})


class _Captured:
    def __init__(self, content="{}", tool_calls=None, usage=None, finish="stop"):
        self.kwargs = {}
        self._content, self._tool_calls = content, tool_calls
        self._usage, self._finish = usage, finish

    def install(self, adapter):
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.kwargs = kw
                msg = type("M", (), {"content": outer._content,
                                     "tool_calls": outer._tool_calls})()
                choice = type("C", (), {"message": msg, "finish_reason": outer._finish})()
                return type("R", (), {"choices": [choice], "usage": outer._usage})()

        chat = type("Chat", (), {"completions": _Completions()})()
        adapter._client = lambda: type("Client", (), {"chat": chat})()
        return self


# ------------------------------------------------------- selection / fallbacks
def test_extraction_uses_the_adapter_on_the_standard_tier():
    from backend.brain.router import OPENROUTER
    cfg = memory._resolve_llm_config()
    assert cfg["provider"] == mem0_llm.PROVIDER_NAME
    assert cfg["config"]["model"] == OPENROUTER.standard


def test_register_is_idempotent():
    assert mem0_llm.register() and mem0_llm.register()
    from mem0.utils.factory import LlmFactory
    assert mem0_llm.PROVIDER_NAME in LlmFactory.provider_to_class


# ------------------------------------------------------------- request shape
def test_reasoning_is_disabled_on_every_extraction_call(monkeypatch):
    """The entire reason this adapter exists. DeepSeek V4.1 Flash reasons by default and
    that reasoning shares max_tokens with the answer; extraction returns strict JSON, so
    it is exactly the call shape that starves. mem0's own OpenAI adapter has no path to
    extra_body and so cannot do this."""
    from backend.brain.router import OPENROUTER
    a = _adapter(monkeypatch)
    cap = _Captured(content='{"facts": []}').install(a)
    a.generate_response([{"role": "user", "content": "hi"}])
    assert cap.kwargs["extra_body"]["reasoning"] == OPENROUTER.no_thinking_body["reasoning"]


def test_provider_routing_preference_is_applied(monkeypatch):
    from backend.brain.router import OPENROUTER
    a = _adapter(monkeypatch)
    cap = _Captured(content="{}").install(a)
    a.generate_response([{"role": "user", "content": "hi"}])
    assert cap.kwargs["extra_body"]["provider"] == OPENROUTER.provider_prefs


def test_no_thinking_temperature_comes_from_the_descriptor(monkeypatch):
    """Moonshot rejects any value but 0.6 once thinking is off. Read from the same
    descriptor field the planner uses, not a second literal."""
    from backend.brain.router import OPENROUTER
    a = _adapter(monkeypatch)
    cap = _Captured(content="{}").install(a)
    a.generate_response([{"role": "user", "content": "hi"}])
    assert cap.kwargs["temperature"] == OPENROUTER.no_thinking_temperature


def test_response_format_is_forwarded(monkeypatch):
    a = _adapter(monkeypatch)
    cap = _Captured(content="{}").install(a)
    a.generate_response([{"role": "user", "content": "hi"}],
                        response_format={"type": "json_object"})
    assert cap.kwargs["response_format"] == {"type": "json_object"}


def test_empty_content_is_never_salvaged_from_reasoning(monkeypatch):
    """The live chat path salvages reasoning into the answer for a real Moonshot quirk,
    and that already shipped one bug (2026-09-05) by reading leaked reasoning aloud.
    Extraction feeds LONG-TERM memory -- a model's private deliberation must never be
    stored as a user fact. Empty means empty."""
    a = _adapter(monkeypatch)
    _Captured(content="", finish="length").install(a)
    assert a.generate_response([{"role": "user", "content": "hi"}]) == ""


def test_usage_is_recorded_including_authoritative_cost(monkeypatch):
    """A09 acceptance names this: "extraction appears in cost". It was invisible before."""
    from backend.brain import usage
    a = _adapter(monkeypatch)
    u = type("U", (), {"prompt_tokens": 100, "completion_tokens": 20,
                       "cost": 0.00005, "prompt_tokens_details": None})()
    _Captured(content='{"facts": []}', usage=u).install(a)
    usage._usage.clear()
    a.generate_response([{"role": "user", "content": "hi"}])
    from backend.brain.router import OPENROUTER
    recorded = usage._usage[OPENROUTER.standard]
    assert recorded["output"] == 20
    assert recorded["cost"] == pytest.approx(0.00005)


def test_cached_tokens_are_split_out_of_input(monkeypatch):
    from backend.brain import usage
    a = _adapter(monkeypatch)
    details = type("D", (), {"cached_tokens": 60})()
    u = type("U", (), {"prompt_tokens": 100, "completion_tokens": 5,
                       "cost": None, "prompt_tokens_details": details})()
    _Captured(content="{}", usage=u).install(a)
    usage._usage.clear()
    a.generate_response([{"role": "user", "content": "hi"}])
    from backend.brain.router import OPENROUTER
    rec = usage._usage[OPENROUTER.standard]
    assert rec["input"] == 40 and rec["cache_read"] == 60


def test_missing_usage_does_not_break_extraction(monkeypatch):
    """Metering must never cost a fact."""
    a = _adapter(monkeypatch)
    _Captured(content='{"facts": ["x"]}', usage=None).install(a)
    assert a.generate_response([{"role": "user", "content": "hi"}]) == '{"facts": ["x"]}'


def test_tool_calls_are_parsed_when_requested(monkeypatch):
    call = type("T", (), {"function": type("F", (), {
        "name": "add_memory", "arguments": '{"text": "likes espresso"}'})()})()
    a = _adapter(monkeypatch)
    _Captured(content=None, tool_calls=[call]).install(a)
    out = a.generate_response([{"role": "user", "content": "hi"}],
                              tools=[{"type": "function"}])
    assert out["tool_calls"][0]["name"] == "add_memory"
    assert out["tool_calls"][0]["arguments"] == {"text": "likes espresso"}
