"""A03 slice 4: chat() assembles its prompt through the context service.

The first A03 change that touches the live foreground prompt. These pin the two
properties that make it safe: the swapped path produces exactly what the old direct call
produced, and a failure in the new machinery degrades to the old path rather than to a
failed turn.
"""
import pytest

from backend.brain import engine, personality
from backend.context import profiles


@pytest.fixture(autouse=True)
def _freeze_now_line(monkeypatch):
    """Pin the [Now] line for every prompt comparison in this module.

    `personality.build_system_prompt` injects `chronos.now_line()`, which has MINUTE
    granularity. These tests byte-compare two separate assemblies, so any of them fails
    if the minute happens to tick between the two calls -- a real intermittent failure
    observed in a full-suite run. The clock is not what these tests are about.
    """
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "now_line",
                        lambda: "[Now] Monday 2026-01-01 00:00 (Europe/Paris)")


USER = "what did we decide about the stock positions?"

INPUTS = dict(
    facts="- Sam holds NVDA and Take Two",
    sleep_info="[Last sleep] 6h ago",
    owner_md="Sam is direct and dislikes hedging.",
    vault_letter="You left the position sizing question unresolved.",
    intent_context="[open] 2026-09-01 — review the NVDA entry",
    relevant_topic="NVDA: bought at 118, thesis is datacenter demand.",
    vault_passage="from the vault: sizing rules of thumb",
    exchange="earlier: we discussed the entry price",
    pending_predictions="1. He'll bring up position sizing again",
    history_bridge="turn 3: discussed sizing",
)


def _direct(**overrides):
    args = {**INPUTS, **overrides}
    return personality.build_system_prompt(
        USER,
        facts=args["facts"], sleep_info=args["sleep_info"],
        owner_md=args["owner_md"], vault_letter=args["vault_letter"],
        intent=args["intent_context"], relevant_topic=args["relevant_topic"],
        vault_passage=args["vault_passage"],
        exchange=args["exchange"], pending_predictions=args["pending_predictions"],
        history_bridge=args["history_bridge"],
    )


def test_swapped_path_matches_direct_assembly_byte_for_byte():
    assert engine._build_prompt_via_context(USER, **INPUTS) == _direct()


def test_matches_with_nothing_retrieved():
    """Cold start: first turn of a fresh session."""
    empty = {k: "" for k in INPUTS}
    assert engine._build_prompt_via_context(USER, **empty) == _direct(**empty)


@pytest.mark.parametrize("only", sorted(INPUTS))
def test_matches_with_each_source_in_isolation(only):
    """A single mis-mapped source cannot hide inside a combined comparison."""
    kwargs = {k: ("" if k != only else INPUTS[k]) for k in INPUTS}
    assert engine._build_prompt_via_context(USER, **kwargs) == _direct(**kwargs)


def test_trigger_gated_blocks_still_fire():
    """Positions/self-model/runtime blocks are injected INSIDE build_system_prompt from
    the user text, not passed in -- the swap must not disturb them."""
    for probe in ("what do you think about Camus?",
                  "how does your memory actually work?",
                  "how are you running right now?",
                  "what's new with you?"):
        got = engine._build_prompt_via_context(probe, **INPUTS)
        expected = personality.build_system_prompt(
            probe,
            facts=INPUTS["facts"], sleep_info=INPUTS["sleep_info"],
            owner_md=INPUTS["owner_md"], vault_letter=INPUTS["vault_letter"],
            intent=INPUTS["intent_context"], relevant_topic=INPUTS["relevant_topic"],
            vault_passage=INPUTS["vault_passage"], exchange=INPUTS["exchange"],
            pending_predictions=INPUTS["pending_predictions"],
            history_bridge=INPUTS["history_bridge"],
        )
        assert got == expected, f"diverged on {probe!r}"


def test_service_failure_falls_back_to_the_old_path(monkeypatch):
    """The prompt is the most load-bearing thing in the system: a bug in new machinery
    must degrade to previous behavior, not to a failed turn."""
    from backend.context import service

    def _boom(*a, **kw):
        raise RuntimeError("context service exploded")

    monkeypatch.setattr(service, "build_foreground", _boom)
    assert engine._build_prompt_via_context(USER, **INPUTS) == _direct()


def test_render_failure_also_falls_back(monkeypatch):
    from backend.context import service

    def _boom(*a, **kw):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(service, "render_foreground", _boom)
    assert engine._build_prompt_via_context(USER, **INPUTS) == _direct()


def test_oversized_source_is_dropped_and_the_rest_survive():
    """The budget doing its job: one huge note must not cost the others."""
    kwargs = dict(INPUTS, relevant_topic="z" * 300000)
    stable, volatile = engine._build_prompt_via_context(USER, **kwargs)
    rendered = stable + volatile
    assert "z" * 300000 not in rendered
    assert INPUTS["owner_md"] in rendered
    assert INPUTS["vault_letter"] in rendered


def test_chat_calls_the_service_path_not_build_system_prompt_directly():
    """Static guard: the swap must not be quietly reverted by a later edit."""
    import inspect

    src = inspect.getsource(engine.chat)
    assert "_build_prompt_via_context(" in src
    assert "build_system_prompt(" not in src, (
        "chat() should assemble through the context service, not call "
        "build_system_prompt directly"
    )
