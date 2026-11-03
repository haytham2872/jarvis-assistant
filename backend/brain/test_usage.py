"""Tests for backend/brain/usage.py — token/cost accounting.

Pure in-memory accounting, no network/Qdrant. Run with:
    .venv\\Scripts\\pytest backend/brain/test_usage.py -v
"""

import pytest

from backend.brain import usage


@pytest.fixture(autouse=True)
def _reset_usage():
    """Every test starts and ends with a clean accumulator -- usage._usage is
    module-level global state shared across the whole test session."""
    usage.reset()
    yield
    usage.reset()


# ---------------------------------------------------------------------------
# record() — accumulation
# ---------------------------------------------------------------------------

def test_record_accumulates_across_repeated_calls_same_model():
    usage.record("deepseek/deepseek-v4.1-flash", 100, 50)
    usage.record("deepseek/deepseek-v4.1-flash", 200, 75)
    counters = usage._usage["deepseek/deepseek-v4.1-flash"]
    assert counters["input"] == 300
    assert counters["output"] == 125
    assert counters["calls"] == 2


def test_record_keeps_models_separate():
    usage.record("deepseek/deepseek-v4.1-flash", 100, 50)
    usage.record("deepseek/deepseek-v4-flash-0731", 10, 5)
    assert usage._usage["deepseek/deepseek-v4.1-flash"]["input"] == 100
    assert usage._usage["deepseek/deepseek-v4-flash-0731"]["input"] == 10


def test_record_accumulates_cache_fields():
    usage.record("deepseek/deepseek-v4-pro-0813", 100, 50, cache_read=20, cache_write=10)
    usage.record("deepseek/deepseek-v4-pro-0813", 100, 50, cache_read=5, cache_write=0)
    counters = usage._usage["deepseek/deepseek-v4-pro-0813"]
    assert counters["cache_read"] == 25
    assert counters["cache_write"] == 10


def test_record_negative_tokens_clamped_to_zero():
    usage.record("deepseek/deepseek-v4.1-flash", -5, -10)
    counters = usage._usage["deepseek/deepseek-v4.1-flash"]
    assert counters["input"] == 0
    assert counters["output"] == 0


def test_record_never_raises_on_bad_input():
    usage.record("deepseek/deepseek-v4.1-flash", "not-a-number", None)  # must not raise
    usage.record(None, 1, 1)  # must not raise


def test_reset_clears_everything():
    usage.record("deepseek/deepseek-v4.1-flash", 100, 50)
    usage.reset()
    assert usage.session_summary() == "No LLM calls recorded yet this session."


# ---------------------------------------------------------------------------
# estimate_cost() — pinned arithmetic against the known pricing table
# ---------------------------------------------------------------------------

def test_estimate_cost_input_only():
    # 1,000,000 input tokens at v4.1-flash's peak cache-miss rate ($0.30/Mtok) = $0.30
    usage.record("deepseek/deepseek-v4.1-flash", 1_000_000, 0)
    assert usage.estimate_cost() == pytest.approx(0.30)


def test_estimate_cost_output_only():
    # 1,000,000 output tokens at $1.20/Mtok = $1.20
    usage.record("deepseek/deepseek-v4.1-flash", 0, 1_000_000)
    assert usage.estimate_cost() == pytest.approx(1.20)


def test_estimate_cost_cache_hit_cheaper_than_miss():
    usage.record("deepseek/deepseek-v4.1-flash", 1_000_000, 0)  # cache miss
    miss_cost = usage.estimate_cost()
    usage.reset()
    usage.record("deepseek/deepseek-v4.1-flash", 0, 0, cache_read=1_000_000)  # cache hit
    hit_cost = usage.estimate_cost()
    assert hit_cost < miss_cost
    assert hit_cost == pytest.approx(0.006)


def test_estimate_cost_sums_across_models():
    usage.record("deepseek/deepseek-v4.1-flash", 1_000_000, 0)     # $0.30
    usage.record("deepseek/deepseek-v4-flash-0731", 1_000_000, 0)  # $0.04
    assert usage.estimate_cost() == pytest.approx(0.30 + 0.04)


def test_estimate_cost_unknown_model_contributes_zero():
    usage.record("some-future-model-not-in-pricing-table", 1_000_000, 1_000_000)
    assert usage.estimate_cost() == 0.0


def test_estimate_cost_empty_is_zero():
    assert usage.estimate_cost() == 0.0


# ---------------------------------------------------------------------------
# session_summary() — formatting
# ---------------------------------------------------------------------------

def test_session_summary_empty():
    assert usage.session_summary() == "No LLM calls recorded yet this session."


def test_session_summary_includes_every_model_and_a_total():
    usage.record("deepseek/deepseek-v4.1-flash", 100, 50)
    usage.record("deepseek/deepseek-v4-flash-0731", 10, 5)
    text = usage.session_summary()
    assert "deepseek/deepseek-v4.1-flash" in text
    assert "deepseek/deepseek-v4-flash-0731" in text
    assert "Total estimated" in text


def test_session_summary_shows_cache_breakdown_only_when_present():
    usage.record("deepseek/deepseek-v4.1-flash", 100, 50)  # no cache activity
    text = usage.session_summary()
    assert "cache" not in text.lower()


def test_session_summary_mentions_cache_when_used():
    usage.record("deepseek/deepseek-v4.1-flash", 100, 50, cache_read=10, cache_write=5)
    text = usage.session_summary()
    assert "cache" in text.lower()


# ---------------------------------------------------------------------------
# DeepSeek/OpenRouter pricing rows
# ---------------------------------------------------------------------------

def test_estimate_cost_deepseek_flash_rates():
    """0731 has no time-of-day overrides -- $0.04/$0.16 flat, re-verified against
    /api/v1/models 2026-09-21 (the old 0.05 row was slightly off)."""
    usage.record("deepseek/deepseek-v4-flash-0731", 1_000_000, 0)
    assert usage.estimate_cost() == pytest.approx(0.04)


def test_estimate_cost_deepseek_pro_rates():
    """Pro's row is its PEAK rate, matching v4.1-flash's basis. It previously held the
    OFF-peak rate ($0.66/$1.98) while v4.1-flash held peak -- two bases in one table."""
    usage.record("deepseek/deepseek-v4-pro-0813", 1_000_000, 1_000_000)
    assert usage.estimate_cost() == pytest.approx(1.32 + 3.96)


def test_deepseek_rows_are_all_on_the_peak_basis():
    """Guards the inconsistency this pass fixed: every DeepSeek row must be the peak
    (conservative) rate, so a fallback estimate never underestimates real spend."""
    p = usage._PRICING
    assert p["deepseek/deepseek-v4.1-flash"]["input"] == 0.30
    assert p["deepseek/deepseek-v4-pro-0813"]["input"] == 1.32
    # peak is exactly 2x off-peak for both models that carry overrides
    assert p["deepseek/deepseek-v4-pro-0813"]["output"] == pytest.approx(2 * 1.98)
    assert p["deepseek/deepseek-v4.1-flash"]["output"] == pytest.approx(2 * 0.60)


# ---------------------------------------------------------------------------
# record(cost=...) — OpenRouter's authoritative per-call cost overrides the
# pricing-table estimate entirely, once any call for a model reports one.
# ---------------------------------------------------------------------------

def test_record_cost_override_wins_over_pricing_table():
    # List price for 1,000 input tokens on this model would be $0.00005 --
    # an authoritative reported cost of $1.23 must win instead.
    usage.record("deepseek/deepseek-v4-flash-0731", 1_000, 0, cost=1.23)
    assert usage.estimate_cost() == pytest.approx(1.23)


def test_record_cost_override_accumulates_across_calls():
    usage.record("deepseek/deepseek-v4-pro-0813", 100, 50, cost=0.01)
    usage.record("deepseek/deepseek-v4-pro-0813", 100, 50, cost=0.02)
    assert usage.estimate_cost() == pytest.approx(0.03)


def test_record_without_cost_falls_back_to_pricing_table():
    usage.record("deepseek/deepseek-v4.1-flash", 1_000_000, 0)  # no cost kwarg at all
    assert usage.estimate_cost() == pytest.approx(0.30)


def test_record_cost_none_is_a_no_op_on_authoritative_flag():
    usage.record("deepseek/deepseek-v4.1-flash", 1_000_000, 0, cost=None)
    assert usage.estimate_cost() == pytest.approx(0.30)  # table rate, not zero
