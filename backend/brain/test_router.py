"""Tests for backend/brain/router.py — model routing.

Covers is_complex_result()'s OR-of-two-signals contract: the keyword regex
(is_complex, the degradation floor) and an optional intent.IntentResult (the
primary signal). No real embedder is loaded -- IntentResult is constructed by
hand with whatever scores each test needs.
"""

from backend.brain import router
from backend.brain.intent import IntentResult


def _result(**scores) -> IntentResult:
    return IntentResult(scores=scores, query_vec=[])


# ---------------------------------------------------------------------------
# is_complex — the pure regex/length floor, unchanged behavior
# ---------------------------------------------------------------------------

def test_is_complex_keyword_hit():
    assert router.is_complex("please debug this for me")


def test_is_complex_long_prompt():
    assert router.is_complex("x" * 301)


def test_is_complex_false_on_simple_greeting():
    assert not router.is_complex("hey what's up")


# ---------------------------------------------------------------------------
# is_complex_result — OR of keyword floor and semantic route
# ---------------------------------------------------------------------------

def test_no_result_falls_back_to_keyword_only():
    assert router.is_complex_result("please debug this", None) is True
    assert router.is_complex_result("hey what's up", None) is False


def test_semantic_hit_wins_when_keyword_misses():
    """The exact case this rewrite exists for: no shared keyword at all."""
    res = _result(complex=0.90)
    assert router.is_complex_result(
        "what would break if you changed how you write to your vault", res
    ) is True


def test_semantic_miss_falls_back_to_keyword():
    res = _result(complex=0.10)
    assert router.is_complex_result("please debug this", res) is True   # keyword still wins
    assert router.is_complex_result("hey what's up", res) is False


def test_both_signals_negative():
    res = _result(complex=0.10)
    assert router.is_complex_result("hey what's up", res) is False


def test_route_uses_result(monkeypatch):
    hit, miss = _result(complex=0.90), _result(complex=0.10)
    assert router.route("structural question with no keywords at all", hit) == router.OPENROUTER_THINKING
    assert router.route("hey what's up", miss) == router.OPENROUTER_STANDARD


def test_route_tier_returns_the_tier_with_the_model():
    """Both tiers are one model, so the tier must come back beside it."""
    hit, miss = _result(complex=0.90), _result(complex=0.10)
    assert router.route_tier("structural question with no keywords at all", hit)[1] is True
    assert router.route_tier("hey what's up", miss)[1] is False
    assert router.route_tier("hey", complex_=True) == (router.OPENROUTER_THINKING, True)


def test_route_without_result_uses_the_keyword_floor():
    """background.py calls route(task) with no second argument at all."""
    assert router.route_tier("please debug this") == (router.OPENROUTER_THINKING, True)
    assert router.route_tier("hey what's up") == (router.OPENROUTER_STANDARD, False)


def test_log_startup_mode_names_the_models_and_endpoint(caplog):
    import logging
    caplog.set_level(logging.INFO, logger=router.log.name)
    router.log_startup_mode()
    assert len(caplog.records) == 1
    message = caplog.records[0].message
    assert router.OPENROUTER_STANDARD in message and router.OPENROUTER_BASE_URL in message


def test_provider_is_openrouter():
    assert router.provider() is router.OPENROUTER


def test_provider_dataclass_fields():
    # DeepSeek V4's own API contract (not OpenRouter's generic third-party-model
    # convention) uses reasoning_content -- confirmed via DeepSeek's own error text
    # ("The reasoning_content in the thinking mode must be passed back to the API")
    # reproduced across multiple independent integrations. Required, not just preferred:
    # the field must be present (empty string if nothing streamed) on a thinking-tier
    # tool-call turn, or the next request 400s.
    assert router.OPENROUTER.reasoning_echo_key == "reasoning_content"
    assert router.OPENROUTER.reasoning_echo_required is True
    assert router.OPENROUTER.thinking_body == {"reasoning": {"effort": router.OPENROUTER_REASONING_EFFORT}}
    assert router.OPENROUTER.no_thinking_body == {"reasoning": {"enabled": False}}


def test_provider_configured_reads_env_live(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert router.OPENROUTER.configured is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert router.OPENROUTER.configured is True
    assert router.OPENROUTER.api_key == "sk-or-test"
