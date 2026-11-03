"""Sharper recall: a cross-encoder gate over the cosine shortlist for passive recall.

The model and Qdrant are faked; what is pinned is the contract around them -- the
reranker decides relevance when present, and when absent or failing the answer is
exactly today's cosine answer.
"""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from backend.brain import rerank, semindex


class FakeQdrant:
    """query_points over a fixed hit list, honouring `limit` like the real client."""

    def __init__(self, hits):
        self.hits = sorted(hits, key=lambda h: -h["score"])
        self.limits = []

    def query_points(self, collection_name=None, query=None, query_filter=None,
                     limit=10, with_payload=True):
        self.limits.append(limit)
        pts = [SimpleNamespace(score=h["score"], payload={k: v for k, v in h.items() if k != "score"})
               for h in self.hits[:limit]]
        return SimpleNamespace(points=pts)


class FakeModel:
    def __init__(self, scores_by_text):
        self.scores_by_text = scores_by_text
        self.pairs = []

    def predict(self, pairs, batch_size=16, show_progress_bar=False):
        self.pairs += pairs
        return [self.scores_by_text.get(text, -10.0) for _, text in pairs]


HITS = [
    {"score": 0.86, "text": "notes about the semantic index", "folder": "self"},
    {"score": 0.84, "text": "Sam holds 200 NVDA shares", "folder": "topics"},
    {"score": 0.82, "text": "---\ntype: topic\n---\nNVDA thesis: datacenter margins", "folder": "topics"},
    {"score": 0.74, "text": "weak but relevant: NVDA earnings date", "folder": "topics"},
    {"score": 0.60, "text": "far below the prefilter", "folder": "topics"},
]


@pytest.fixture
def env(monkeypatch):
    q = FakeQdrant(HITS)
    model = FakeModel({
        "notes about the semantic index": -9.0,
        "Sam holds 200 NVDA shares": 4.0,
        "NVDA thesis: datacenter margins": 2.0,
        "weak but relevant: NVDA earnings date": -1.0,
    })
    monkeypatch.setattr(semindex, "_qdrant", lambda: q)
    monkeypatch.setenv("JARVIS_RERANK", "1")
    monkeypatch.setattr(rerank, "_get_model", lambda: model)
    return SimpleNamespace(q=q, model=model)


def search(**kw):
    args = dict(query="what's my NVDA position", kind="vault", k=2, score_min=0.80,
                folder=None, query_vec=[0.1] * 384, session_id=None)
    args.update(kw)
    return semindex._search_sync(**args)


def texts(hits):
    return [h["text"] for h in hits]


# ------------------------------------------------------------------ the gate
def test_the_reranker_decides_relevance_not_cosine(env):
    """Cosine ranked the semantic-index note first; the cross-encoder drops it."""
    out = search(rerank=True)
    assert texts(out) == ["Sam holds 200 NVDA shares",
                          "---\ntype: topic\n---\nNVDA thesis: datacenter margins"]
    assert out[0]["rerank"] == 4.0 and out[0]["score"] == 0.84    # cosine kept for callers


def test_the_shortlist_is_widened_below_the_cosine_floor(env):
    """A relevant hit under the 0.80 floor can now be kept -- the floor becomes a loose
    pre-filter and the reranker makes the decision."""
    out = search(rerank=True, k=5)
    assert "weak but relevant: NVDA earnings date" in texts(out)
    assert "far below the prefilter" not in texts(out)
    assert env.q.limits == [rerank.POOL]


def test_frontmatter_is_stripped_before_scoring(env):
    search(rerank=True)
    scored = [text for _, text in env.model.pairs]
    assert "NVDA thesis: datacenter margins" in scored
    assert not any(t.startswith("---") for t in scored)


def test_the_best_hit_gets_a_second_chance_when_cosine_agrees():
    """The NVDA shape: 'how is my NVDA position doing' vs the NVDA technical-setup note
    scored -3.9 -- relevant background that MS MARCO does not see as an answer."""
    hits = [{"score": 0.863, "text": "NVDA technical setup"},
            {"score": 0.844, "text": "NVDA decline analysis"}]
    model = FakeModel({"NVDA technical setup": -3.9, "NVDA decline analysis": -3.95})
    import backend.brain.rerank as r
    old = r._get_model
    try:
        r._get_model = lambda: model
        import os
        os.environ["JARVIS_RERANK"] = "1"
        kept = r.gate_sync("how is my NVDA position doing", hits, lead_cosine=0.80)
        assert [h["text"] for h in kept] == ["NVDA technical setup"]     # only the lead
        # the second chance needs the cosine floor too
        assert r.gate_sync("q", [dict(hits[0], score=0.76)], lead_cosine=0.80) == []
        # and without a cosine floor to agree with, there is no second chance
        assert r.gate_sync("q", hits) == []
    finally:
        r._get_model = old
        os.environ["JARVIS_RERANK"] = "0"


def test_the_search_seam_passes_the_callers_floor_as_the_lead_cosine():
    assert "lead_cosine=score_min" in inspect.getsource(semindex._search_reranked_sync)


def test_nothing_relevant_means_nothing_injected(env, monkeypatch):
    monkeypatch.setattr(rerank, "_get_model", lambda: FakeModel({}))
    assert search(rerank=True) == []


# ------------------------------------------------------------------ degradation
def _plain():
    return search(rerank=False)


def test_an_unavailable_model_returns_exactly_the_cosine_answer(env, monkeypatch):
    monkeypatch.setattr(rerank, "_get_model", lambda: None)
    assert search(rerank=True) == _plain()


def test_the_fallback_keeps_the_old_floor_even_though_the_pool_is_wider(env, monkeypatch):
    """The widened pool reaches below the floor (0.74 here); without the reranker those
    hits must not leak into what used to be a floor-gated answer."""
    monkeypatch.setattr(rerank, "_get_model", lambda: None)
    out = search(rerank=True, k=5)
    assert out == search(rerank=False, k=5)
    assert min(h["score"] for h in out) >= 0.80


def test_a_failing_model_returns_exactly_the_cosine_answer(env, monkeypatch):
    class Boom:
        def predict(self, *a, **k):
            raise RuntimeError("oom")
    monkeypatch.setattr(rerank, "_get_model", lambda: Boom())
    assert search(rerank=True) == _plain()


def test_disabled_by_env_returns_exactly_the_cosine_answer(env, monkeypatch):
    monkeypatch.setenv("JARVIS_RERANK", "0")
    assert search(rerank=True) == _plain()


def test_the_suite_never_loads_the_real_model():
    assert not rerank.enabled()


def test_the_model_loads_from_the_local_cache_only_and_sets_its_flag_last(monkeypatch):
    src = inspect.getsource(rerank._get_model)
    assert "local_files_only=True" in src
    assert src.index("_model = CrossEncoder(") < src.index("_load_attempted = True")


# ------------------------------------------------------------------ wiring
def test_exactly_the_passive_paths_opt_in():
    from backend.brain import memory, threads
    from backend.tools import vault
    assert "rerank=True" in inspect.getsource(memory._semantic_vault_passage)
    assert "rerank=True" in inspect.getsource(vault.read_relevant_topic)
    exch = inspect.getsource(threads.exchange_candidates)
    assert exch.count("rerank=True") == 1                       # its one search
    assert "session_id=" not in exch      # no same-session search: the history holds it
    # on-demand search is left alone: when he asks, recall matters more than precision
    assert "rerank" not in inspect.getsource(vault._search_one_kind)
    assert not hasattr(semindex, "recall_skill")      # skills are not recalled by similarity


def test_the_reranker_is_warmed_at_startup():
    from backend import main
    assert "rerank.warm()" in inspect.getsource(main)


def test_async_search_passes_the_flag_through(env):
    out = asyncio.run(semindex.search("what's my NVDA position", kind="vault", k=2,
                                      query_vec=[0.1] * 384, rerank=True))
    assert texts(out)[0] == "Sam holds 200 NVDA shares"
