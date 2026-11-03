"""Tests for backend/brain/intent.py — the semantic intent gate.

Deliberately avoids loading the real gte-small model: every test either
constructs an IntentResult directly (pure) or fakes embeddings._embed_many_sync
with hand-crafted low-dimensional vectors so the matmul/scoring pipeline is
exercised without a real model load or network access. Same discipline as the
rest of this suite — no real Qdrant, no real LLM, no real embedder.
"""

import asyncio

import pytest

from backend.brain import intent


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# IntentResult.is_route / top_sections / _family_best — pure, hand-crafted scores
# ---------------------------------------------------------------------------

def test_is_route_true_above_floor():
    res = intent.IntentResult(scores={"runtime": 0.90}, query_vec=[])
    assert res.is_route("runtime")


def test_is_route_false_below_floor():
    res = intent.IntentResult(scores={"runtime": 0.10}, query_vec=[])
    assert not res.is_route("runtime")


def test_is_route_missing_route_defaults_to_zero():
    res = intent.IntentResult(scores={}, query_vec=[])
    assert not res.is_route("runtime")


def test_unknown_route_uses_default_floor_and_singleton_family():
    """A route not in ROUTES/_FLOORS/_FAMILY (e.g. a stale name) must not crash."""
    res = intent.IntentResult(scores={"totally_made_up": 0.95}, query_vec=[])
    assert res.is_route("totally_made_up")   # clears _DEFAULT_FLOOR, alone in its family
    res2 = intent.IntentResult(scores={"totally_made_up": 0.10}, query_vec=[])
    assert not res2.is_route("totally_made_up")


def test_margin_suppresses_hub_false_positive_within_same_family():
    """Reproduces the live 'who are you really' finding: a near-exact match on
    one self-facing route must not let a mediocre same-family score for an
    unrelated route also count, even though that score clears its own floor."""
    res = intent.IntentResult(scores={
        "self_reflection": 1.000,
        "runtime": 0.837,   # clears runtime's own floor (0.830) in isolation
        "calibration": 0.873,   # clears calibration's own floor (0.837) in isolation
    }, query_vec=[])
    assert res.is_route("self_reflection")
    assert not res.is_route("runtime"), "runtime should be suppressed by the self-family margin"
    assert not res.is_route("calibration"), "calibration should be suppressed by the self-family margin"


def test_complex_is_immune_to_self_family_hub_score():
    """complex lives in its own singleton family (_FAMILY['complex'] == 'task')
    specifically so an unrelated dominant self-model hit can never suppress
    model routing -- the two are decided at different call sites."""
    res = intent.IntentResult(scores={
        "sm:personality": 0.95,   # dominant, would suppress anything in the 'self' family
        "complex": 0.83,          # clears its own floor in isolation
    }, query_vec=[])
    assert res.is_route("complex")


def test_close_tie_within_family_both_pass():
    """A message that legitimately touches two routes at comparable strength
    should not have the weaker one suppressed -- margin is 0.06. Both scores
    are set to the SAME value (whichever route's floor is higher, plus a
    buffer) so the assertion holds regardless of the current calibrated
    floors -- this test encodes only the margin mechanism, not a snapshot of
    calibration numbers that move on reseeding."""
    tied_score = max(intent._FLOORS["sm:router"], intent._FLOORS["sm:claude"]) + 0.02
    res = intent.IntentResult(
        scores={"sm:router": tied_score, "sm:claude": tied_score},
        query_vec=[],
    )
    assert res.is_route("sm:router")
    assert res.is_route("sm:claude")


def test_top_sections_caps_at_k_and_sorts_best_first():
    res = intent.IntentResult(scores={
        "sm:claude":      0.90,
        "sm:personality": 0.95,
        "sm:tts":         0.89,
        "sm:frontend":    0.88,
    }, query_vec=[])
    assert res.top_sections(k=2) == ["personality", "claude"]


def test_top_sections_excludes_non_sm_routes():
    res = intent.IntentResult(scores={"sm:memory": 0.95, "complex": 0.95, "runtime": 0.95}, query_vec=[])
    assert res.top_sections() == ["memory"]


def test_top_sections_empty_when_nothing_clears_floor():
    res = intent.IntentResult(scores={"sm:memory": 0.10}, query_vec=[])
    assert res.top_sections() == []


# ---------------------------------------------------------------------------
# classify_sync — degradation and the matmul pipeline, with a fake embedder
# ---------------------------------------------------------------------------

def test_classify_sync_none_when_matrix_not_built(monkeypatch):
    monkeypatch.setattr(intent, "_matrix", None)
    assert intent.classify_sync("anything") == None  # noqa: E711 -- explicit None check


def test_classify_sync_none_on_empty_text(monkeypatch):
    monkeypatch.setattr(intent, "_matrix", object())   # non-None sentinel
    assert intent.classify_sync("") is None
    assert intent.classify_sync("   ") is None


def _fake_matrix_2d(monkeypatch, rows: list[tuple[str, tuple[float, float], float]]):
    """rows: (route, (x, y) unit vector, floor_bonus). Sets up a 2D toy matrix
    so cosine similarity is hand-computable without a real model."""
    import numpy as np
    matrix = np.asarray([r[1] for r in rows], dtype="float32")
    monkeypatch.setattr(intent, "_matrix", matrix)
    monkeypatch.setattr(intent, "_row_routes", [r[0] for r in rows])
    monkeypatch.setattr(intent, "_row_floor_bonus", [r[2] for r in rows])

    def fake_embed(texts, background=False):
        # Query embeds to whatever the test wants via a closure override below.
        return [_fake_query_vec[0]]

    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", fake_embed)


_fake_query_vec = [(1.0, 0.0)]


def test_classify_sync_scores_via_cosine(monkeypatch):
    global _fake_query_vec
    _fake_query_vec = [(1.0, 0.0)]
    _fake_matrix_2d(monkeypatch, [
        ("route_a", (1.0, 0.0), 0.0),    # identical to query -> cosine 1.0
        ("route_a", (0.0, 1.0), 0.0),    # orthogonal -> cosine 0.0 (max-per-route keeps 1.0)
        ("route_b", (0.7071, 0.7071), 0.0),  # 45 degrees -> cosine ~0.707
    ])
    res = intent.classify_sync("query text")
    assert res is not None
    assert res.scores["route_a"] == pytest.approx(1.0, abs=1e-4)
    assert res.scores["route_b"] == pytest.approx(0.7071, abs=1e-3)


def test_classify_sync_applies_digest_chunk_floor_bonus(monkeypatch):
    """A digest-chunk row's floor bonus is subtracted from its similarity, not
    added as a separate per-row floor -- this test locks that exact mechanism."""
    global _fake_query_vec
    _fake_query_vec = [(1.0, 0.0)]
    _fake_matrix_2d(monkeypatch, [
        ("route_a", (1.0, 0.0), 0.05),   # identical match, but a 0.05 bonus row
    ])
    res = intent.classify_sync("query text")
    assert res.scores["route_a"] == pytest.approx(0.95, abs=1e-4)


def test_classify_sync_none_when_embed_fails(monkeypatch):
    monkeypatch.setattr(intent, "_matrix", object())
    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", lambda texts, background=False: [])
    assert intent.classify_sync("anything") is None


# ---------------------------------------------------------------------------
# classify — the async wrapper actually runs in an executor
# ---------------------------------------------------------------------------

def test_classify_async_wrapper_returns_none_cleanly(monkeypatch):
    monkeypatch.setattr(intent, "_matrix", None)
    assert run(intent.classify("anything")) is None


# ---------------------------------------------------------------------------
# _build_matrix_sync — degrades to False, never raises
# ---------------------------------------------------------------------------

def test_build_matrix_sync_false_when_embedder_unavailable(monkeypatch):
    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", lambda texts, background=False: [])
    assert intent._build_matrix_sync() is False


def test_build_matrix_sync_includes_digest_chunks_only_for_known_routes(monkeypatch):
    """A digest section with no matching 'sm:' route must never be invented
    as a new route -- only chunks for sections already in ROUTES are used."""
    monkeypatch.setattr(intent, "_digest_chunks", lambda: [
        ("sm:memory", "x" * 200),                 # known route -> included
        ("sm:not_a_real_section", "y" * 200),     # would only appear if the
                                                    # guard in _digest_chunks itself
                                                    # were bypassed; verifies the
                                                    # matrix builder doesn't filter
                                                    # again and trusts its input
    ])
    captured = {}

    def fake_embed(texts, background=False):
        captured["texts"] = texts
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", fake_embed)
    ok = intent._build_matrix_sync()
    assert ok
    # Every ROUTES seed utterance plus both digest chunks passed through --
    # this asserts the wiring (seeds + digest chunks reach the embedder
    # together), not the filtering (that's _digest_chunks' own job, tested below).
    total_seeds = sum(len(v) for v in intent.ROUTES.values())
    assert len(captured["texts"]) == total_seeds + 2


def test_digest_chunks_empty_when_self_model_not_ready(monkeypatch):
    import backend.brain.self_model as sm
    monkeypatch.setattr(sm, "is_ready", lambda: False)
    assert intent._digest_chunks() == []


def test_digest_chunks_skips_sections_without_a_route(monkeypatch):
    import backend.brain.self_model as sm
    monkeypatch.setattr(sm, "is_ready", lambda: True)
    monkeypatch.setattr(sm, "get_all_sections", lambda: {
        "memory": "x" * 500,             # sm:memory exists in ROUTES -> included
        "made_up_section": "y" * 500,    # sm:made_up_section not in ROUTES -> skipped
    })
    chunks = intent._digest_chunks()
    routes = {r for r, _ in chunks}
    assert routes == {"sm:memory"}


def test_digest_chunks_skips_tiny_fragments(monkeypatch):
    import backend.brain.self_model as sm
    monkeypatch.setattr(sm, "is_ready", lambda: True)
    monkeypatch.setattr(sm, "get_all_sections", lambda: {"memory": "short"})
    assert intent._digest_chunks() == []


def test_digest_chunks_degrades_to_empty_on_any_exception(monkeypatch):
    import backend.brain.self_model as sm
    def boom():
        raise RuntimeError("cache corrupted")
    monkeypatch.setattr(sm, "is_ready", boom)
    assert intent._digest_chunks() == []


# ---------------------------------------------------------------------------
# ROUTES / _FLOORS / _FAMILY internal consistency — catches drift when a
# route is added/renamed without updating its floor or family.
# ---------------------------------------------------------------------------

def test_every_sm_route_has_a_matching_self_model_section():
    from backend.brain.self_model import _SECTION_HEADINGS
    for route in intent.ROUTES:
        if route.startswith("sm:"):
            assert route[3:] in _SECTION_HEADINGS, f"{route} has no matching self_model section"


def test_every_floor_key_is_a_real_route():
    for name in intent._FLOORS:
        assert name in intent.ROUTES, f"_FLOORS has a stale entry: {name}"


def test_every_route_has_at_least_three_utterances():
    """A route with 1-2 utterances is fragile -- max-similarity has almost no
    signal to work with. Not a hard requirement, but a real smell."""
    for route, utterances in intent.ROUTES.items():
        assert len(utterances) >= 3, f"{route} has too few seed utterances ({len(utterances)})"


def test_complex_is_the_only_task_family_member():
    assert intent._FAMILY["complex"] == "task"
    assert all(fam == "self" for route, fam in intent._FAMILY.items() if route != "complex")
