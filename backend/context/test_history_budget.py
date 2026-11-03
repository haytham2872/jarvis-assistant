"""The foreground budget reserves the history actually being sent, not a guess.

A03 reserved a fixed 8,000 tokens for conversation history. Measured over the real
exchange history, the live 40-turn window runs p50 8,878 / p95 12,985 / max 16,146
tokens, so the guess sat below the median and the budget could never bind on the part
of the request most likely to grow. At observed sizes the honest figure still leaves
every source room inside the 32k working ceiling; it only starts dropping optional
sources for a genuinely oversized session, which is the budget's job.
"""
import inspect
import logging

from backend.brain import engine
from backend.context import budget


def _history(n_turns: int, chars_each: int) -> list[dict]:
    out = []
    for i in range(n_turns):
        out.append({"role": "user", "content": "u" * chars_each})
        out.append({"role": "assistant", "content": "a" * chars_each})
    return out


def test_history_tokens_counts_content_and_framing():
    h = [{"role": "user", "content": "x" * 382}]
    assert budget.history_tokens(h) == budget.estimate_tokens("x" * 382) + 4
    assert budget.history_tokens([]) == 0
    assert budget.history_tokens(None) == 0


def test_the_reservation_is_the_measured_history_not_the_guess():
    h = _history(40, 1000)
    r = budget.for_history(h, "what now?")
    assert r.messages == (budget.history_tokens(h) + budget.estimate_tokens("what now?") + 4)
    assert r.messages != budget.DEFAULT_RESERVATION.messages
    assert (r.tools, r.answer, r.margin) == (budget.DEFAULT_RESERVATION.tools,
                                             budget.DEFAULT_RESERVATION.answer,
                                             budget.DEFAULT_RESERVATION.margin)


def test_a_p95_sized_history_drops_nothing():
    """The measured p95 (~13k tokens) must leave today's prompt untouched."""
    r = budget.for_history(_history(40, int(13000 * budget.CHARS_PER_TOKEN / 80)))
    available = budget.DEFAULT_CONTEXT_WINDOW - r.total
    assert available > 8000, "a normal long session would start losing context"


def _render(history, caplog):
    with caplog.at_level(logging.INFO, logger="backend.brain.engine"):
        engine._build_prompt_via_context(
            "hello", facts="", sleep_info="", owner_md="h" * 4000,
            vault_letter="l" * 6000, intent_context="", relevant_topic="t" * 8000,
            vault_passage="", exchange="", pending_predictions="",
            history_bridge="", history=history)
    return caplog.text


def test_an_oversized_history_makes_the_budget_bind(caplog):
    """With ~28k tokens of history, optional sources give way instead of the request
    silently exceeding the working ceiling."""
    text = _render(_history(40, 1400), caplog)
    assert "source(s) omitted" in text


def test_a_normal_history_renders_everything(caplog):
    assert "source(s) omitted" not in _render(_history(10, 400), caplog)


def test_chat_passes_the_real_history_to_the_budget():
    src = inspect.getsource(engine.chat)
    call = src[src.index("_build_prompt_via_context("):]
    call = call[:call.index(")\n")]
    assert "history=history" in call


def test_budget_reserves_the_real_tool_schemas():
    """Measured live 2026-09-28: 36,631 chars of schema JSON cost 9,022 prompt tokens
    (4.06 chars/token). A registry that grows past the reservation fails here."""
    import json
    from backend.brain import engine
    from backend.tools.registry import TOOLS
    schema_tokens = len(json.dumps(engine._to_openai_tools(TOOLS))) / 4.06
    assert schema_tokens <= budget.DEFAULT_RESERVATION.tools
    assert budget.DEFAULT_RESERVATION.tools - schema_tokens < 1000   # and not padded


def test_output_limits_fit_the_provider_window():
    """Reasoning is not reserved inside the working ceiling; the real bound is the
    provider window, which the largest input plus the largest output must fit."""
    from backend.brain import engine
    assert (budget.DEFAULT_CONTEXT_WINDOW + max(engine._OUTPUT_LIMITS.values())
            <= budget.PROVIDER_CONTEXT_WINDOW)
