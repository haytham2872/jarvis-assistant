"""Guardrails for the 2026-09-21 openrouter pass: provider routing preference +
the explicit tier decision that had to come with the v4.1-flash thinking-tier swap.

Both exist because the same fact stopped being recoverable from the model string:
once standard and thinking are the SAME model at different reasoning efforts,
`model == provider.thinking` is True for a standard turn too.
"""
import copy

from backend.brain import router
from backend.brain.router import OPENROUTER, with_provider_prefs, route_tier


# --------------------------------------------------------- with_provider_prefs
def test_openrouter_body_gains_provider_prefs():
    out = with_provider_prefs(OPENROUTER.no_thinking_body, OPENROUTER)
    assert out["reasoning"] == OPENROUTER.no_thinking_body["reasoning"]
    assert out["provider"] == OPENROUTER.provider_prefs


def test_shared_constants_are_never_mutated():
    """The bodies passed in are shared module-level constants. Mutating one in place
    would leak the routing preference into every other call site importing the same
    object -- the exact trap CLAUDE.md already calls out for MAX_EFFORT_BODY."""
    from backend.brain import escalation

    snapshots = {
        "thinking": copy.deepcopy(router.OPENROUTER_THINKING_BODY),
        "no_thinking": copy.deepcopy(router.OPENROUTER_NO_THINKING_BODY),
        "max_effort": copy.deepcopy(escalation.MAX_EFFORT_BODY),
    }
    for _ in range(5):
        with_provider_prefs(router.OPENROUTER_THINKING_BODY, OPENROUTER)
        with_provider_prefs(router.OPENROUTER_NO_THINKING_BODY, OPENROUTER)
        with_provider_prefs(escalation.MAX_EFFORT_BODY, OPENROUTER)
    assert router.OPENROUTER_THINKING_BODY == snapshots["thinking"]
    assert router.OPENROUTER_NO_THINKING_BODY == snapshots["no_thinking"]
    assert escalation.MAX_EFFORT_BODY == snapshots["max_effort"]
    assert "provider" not in router.OPENROUTER_THINKING_BODY


def test_prefs_use_order_with_fallbacks_never_only():
    """`only` would make a single host's outage a hard failure. During the live
    measurement that produced this ordering, GMICloud returned a 520 -- host failure
    is an observed event here, not a hypothetical."""
    prefs = OPENROUTER.provider_prefs
    assert prefs is not None
    assert prefs["allow_fallbacks"] is True
    assert "only" not in prefs
    assert prefs["order"] and isinstance(prefs["order"], list)


def test_provider_order_is_disableable_by_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "")
    import importlib
    reloaded = importlib.reload(router)
    try:
        assert reloaded.OPENROUTER_PROVIDER_PREFS is None
        assert reloaded.with_provider_prefs({"a": 1}, reloaded.OPENROUTER) == {"a": 1}
    finally:
        monkeypatch.delenv("OPENROUTER_PROVIDER_ORDER", raising=False)
        importlib.reload(router)


# ------------------------------------------------------------------ route_tier
def test_openrouter_tier_follows_complexity_not_model_identity(monkeypatch):
    """The regression this whole pass exists to prevent: both openrouter tiers are one
    model now, so a simple turn must still resolve to the STANDARD tier. Before
    route_tier() this was verified live to send reasoning.effort=high on 'hey what's
    up'."""
    monkeypatch.setattr(router, "provider", lambda: OPENROUTER)

    simple_model, simple_thinking = route_tier("hey what's up")
    assert simple_thinking is False
    complex_model, complex_thinking = route_tier(
        "debug why the vault write fails and explain the root cause")
    assert complex_thinking is True
    # Same model both ways -- which is precisely why the boolean has to be returned.
    assert simple_model == complex_model == OPENROUTER.standard == OPENROUTER.thinking


def test_no_provider_preference_returns_the_body_unchanged(monkeypatch):
    from dataclasses import replace
    bare = replace(OPENROUTER, provider_prefs=None)
    assert with_provider_prefs(bare.no_thinking_body, bare) is bare.no_thinking_body
    assert with_provider_prefs(None, bare) is None


def test_route_still_returns_just_the_model(monkeypatch):
    monkeypatch.setattr(router, "provider", lambda: OPENROUTER)
    assert router.route("hey what's up") == route_tier("hey what's up")[0]
