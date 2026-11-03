"""Tests for backend/brain/threads.py's exchange recall path (Step 2, 2026-09-08):
rank_exchange() (with a same-session/cross-session split decay),
_dedupe_exchange_hits() (chunk dedup), and exchange_candidates() (one cross-session
search on deep turns, deduped and re-ranked, leaving out what the live history holds).

No real Qdrant/embedder is touched -- semindex.search is monkeypatched.
"""

import asyncio

import pytest

from backend.brain import threads as th


def run(coro):
    return asyncio.run(coro)


def _patch_search(monkeypatch, hits=None, capture=None):
    """Fake semindex.search() returning `hits`, recording what it was asked."""
    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None,
                           query_vec=None, session_id=None, rerank=False):
        if capture is not None:
            capture["query_vec"] = query_vec
            capture["session_id"] = session_id
        return list(hits or [])

    monkeypatch.setattr("backend.brain.semindex.search", fake_search)


def _freeze_time(monkeypatch, now):
    monkeypatch.setattr("backend.brain.threads.time", type("T", (), {"time": staticmethod(lambda: now)}))


# ---------------------------------------------------------------------------
# rank_exchange -- split decay
# ---------------------------------------------------------------------------

def test_rank_exchange_cross_session_uses_30_day_decay():
    import math
    now = 1_000_000.0
    hit = {"score": 0.9, "significance": 1.0, "timestamp": now - 30 * 86400, "session_id": "other"}
    rank = th.rank_exchange(hit, now, current_session_id="mine")
    # 30 days = the decay tau, so the decay factor is exactly 1/e at this age.
    expected = 0.9 * 1.0 * math.exp(-1.0)
    assert rank == pytest.approx(expected, rel=1e-6)


def test_rank_exchange_same_session_uses_minute_decay_not_day_decay():
    """The bug this fixes: under the old single (30-day) curve, anything said
    within the last few hours of the SAME conversation was age ~ 0 days, so
    recency contributed nothing to same-session ordering at all."""
    now = 1_000_000.0
    hit_5_min_ago  = {"score": 0.8, "significance": 0.5, "timestamp": now - 5 * 60, "session_id": "s1"}
    hit_2_hours_ago = {"score": 0.8, "significance": 0.5, "timestamp": now - 2 * 3600, "session_id": "s1"}
    rank_recent = th.rank_exchange(hit_5_min_ago, now, current_session_id="s1")
    rank_older  = th.rank_exchange(hit_2_hours_ago, now, current_session_id="s1")
    assert rank_recent > rank_older
    same_session_gap = rank_recent - rank_older
    # Under the OLD cross-session (30-day) curve, both are still < 1 day old,
    # so the gap between them is far smaller than under the new minute-scale
    # curve -- recency barely moved the needle at all for a same-session turn.
    cross_session_rank_recent = th.rank_exchange(hit_5_min_ago, now, current_session_id=None)
    cross_session_rank_older  = th.rank_exchange(hit_2_hours_ago, now, current_session_id=None)
    cross_session_gap = cross_session_rank_recent - cross_session_rank_older
    assert same_session_gap > cross_session_gap * 10


def test_rank_exchange_only_uses_fast_decay_when_session_ids_actually_match():
    """A hit from a DIFFERENT session must not get the fast same-session decay
    just because a current_session_id was supplied -- only a genuine match does.
    At a 1-hour age, the fast (60-minute-tau) curve has already decayed
    substantially while the slow (30-day-tau) curve has barely moved --
    so a matching session_id must rank LOWER here, not higher."""
    now = 1_000_000.0
    hit = {"score": 0.8, "significance": 0.5, "timestamp": now - 3600, "session_id": "other-session"}
    same_session_treatment  = th.rank_exchange(hit, now, current_session_id="other-session")
    cross_session_treatment = th.rank_exchange(hit, now, current_session_id="my-session")
    assert same_session_treatment < cross_session_treatment


def test_rank_exchange_no_session_context_behaves_like_cross_session():
    now = 1_000_000.0
    hit = {"score": 0.8, "significance": 0.5, "timestamp": now - 3600, "session_id": "s1"}
    assert th.rank_exchange(hit, now) == th.rank_exchange(hit, now, current_session_id=None)


# ---------------------------------------------------------------------------
# _dedupe_exchange_hits
# ---------------------------------------------------------------------------

def test_dedupe_keeps_best_ranked_chunk_per_exchange():
    hits = [
        {"exchange_id": "e1", "_rank": 0.3, "text": "chunk 1 of e1"},
        {"exchange_id": "e1", "_rank": 0.7, "text": "chunk 2 of e1 -- higher rank"},
        {"exchange_id": "e2", "_rank": 0.5, "text": "only chunk of e2"},
    ]
    out = th._dedupe_exchange_hits(hits)
    assert len(out) == 2
    e1_kept = next(h for h in out if h["exchange_id"] == "e1")
    assert e1_kept["text"] == "chunk 2 of e1 -- higher rank"


def test_dedupe_legacy_hits_without_exchange_id_never_collide():
    hits = [
        {"_rank": 0.5, "text": "legacy point A"},
        {"_rank": 0.5, "text": "legacy point B"},
    ]
    out = th._dedupe_exchange_hits(hits)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# exchange_candidates
# ---------------------------------------------------------------------------

def test_empty_query_returns_empty_list(monkeypatch):
    assert run(th.exchange_candidates("")) == []
    assert run(th.exchange_candidates("   ")) == []


def test_no_hits_returns_empty_list(monkeypatch):
    _patch_search(monkeypatch, hits=[])
    assert run(th.exchange_candidates("q", deep=True)) == []


def test_returns_up_to_three_candidates_ranked_best_first(monkeypatch):
    now = 1_000_000.0
    hits = [
        {"exchange_id": "e1", "score": 0.9, "significance": 1.0, "timestamp": now, "text": "best"},
        {"exchange_id": "e2", "score": 0.6, "significance": 0.5, "timestamp": now, "text": "middle"},
        {"exchange_id": "e3", "score": 0.3, "significance": 0.1, "timestamp": now, "text": "worst"},
        {"exchange_id": "e4", "score": 0.2, "significance": 0.1, "timestamp": now, "text": "fourth -- should be cut"},
    ]
    _patch_search(monkeypatch, hits=hits)
    _freeze_time(monkeypatch, now)
    result = run(th.exchange_candidates("q", deep=True))
    assert len(result) == 3
    assert [c["text"] for c in result] == ["best", "middle", "worst"]


def test_not_deep_searches_no_exchanges(monkeypatch):
    """A not-deep turn recalls no exchanges. The session-scoped search it used to run
    returned the current conversation, which the live history already holds: replaying
    40 Discord messages on the server (2026-10-11), 31 of 40 not-deep turns carried
    ~1,000 chars of it, all from the same session."""
    calls = []

    async def fake_search(*a, **kw):
        calls.append(kw)
        return [{"exchange_id": "e1", "score": 0.9, "significance": 1.0,
                 "timestamp": 1.0, "text": "hit"}]

    monkeypatch.setattr("backend.brain.semindex.search", fake_search)
    assert run(th.exchange_candidates("q", deep=False)) == []
    assert calls == []


def test_one_unfiltered_reranked_search_when_deep(monkeypatch):
    calls = []

    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None,
                          query_vec=None, session_id=None, rerank=False):
        calls.append({"kind": kind, "session_id": session_id, "rerank": rerank})
        return []

    monkeypatch.setattr("backend.brain.semindex.search", fake_search)
    run(th.exchange_candidates("q", deep=True))
    assert calls == [{"kind": "exchange", "session_id": None, "rerank": True}]


def test_exchanges_already_in_the_live_history_are_left_out(monkeypatch):
    now = 1_000_000.0
    since = now - 600                       # the oldest turn the live history holds
    base = {"score": 0.9, "significance": 1.0}
    hits = [
        {**base, "exchange_id": "e1", "channel": "discord", "timestamp": now - 60, "text": "in history"},
        {**base, "exchange_id": "e2", "channel": "discord", "timestamp": since, "text": "oldest in history"},
        {**base, "exchange_id": "e3", "channel": "discord", "timestamp": since - 1, "text": "before it"},
        {**base, "exchange_id": "e4", "channel": "local", "timestamp": now - 30, "text": "at the desk"},
    ]
    _patch_search(monkeypatch, hits=hits)
    _freeze_time(monkeypatch, now)
    result = run(th.exchange_candidates("q", deep=True, channel="discord", exclude_since=since))
    assert sorted(c["text"] for c in result) == ["at the desk", "before it"]


def test_nothing_left_out_without_a_live_history(monkeypatch):
    now = 1_000_000.0
    hits = [{"exchange_id": "e1", "channel": "discord", "score": 0.9, "significance": 1.0,
             "timestamp": now - 60, "text": "recent"}]
    _patch_search(monkeypatch, hits=hits)
    _freeze_time(monkeypatch, now)
    result = run(th.exchange_candidates("q", deep=True, channel="discord", exclude_since=None))
    assert [c["text"] for c in result] == ["recent"]


def test_text_capped_at_passage_length(monkeypatch):
    now = 1_000_000.0
    long_text = "z" * (th._EXCHANGE_PASSAGE_CAP + 500)
    hits = [{"exchange_id": "e1", "score": 0.9, "significance": 1.0, "timestamp": now, "text": long_text}]
    _patch_search(monkeypatch, hits=hits)
    _freeze_time(monkeypatch, now)
    result = run(th.exchange_candidates("q", deep=True))
    assert len(result[0]["text"]) == th._EXCHANGE_PASSAGE_CAP


def test_score_field_is_the_composite_rank_not_raw_cosine(monkeypatch):
    now = 1_000_000.0
    hits = [{"exchange_id": "e1", "score": 0.8, "significance": 0.5, "timestamp": now, "text": "hit"}]
    _patch_search(monkeypatch, hits=hits)
    _freeze_time(monkeypatch, now)
    result = run(th.exchange_candidates("q", deep=True))
    expected_rank = th.rank_exchange(hits[0], now, current_session_id=None)
    assert result[0]["score"] == pytest.approx(expected_rank)
    assert result[0]["score"] != 0.8, "score must be the composite rank, not the raw semindex score"


def test_query_vec_is_passed_through_to_search(monkeypatch):
    capture = {}
    _patch_search(monkeypatch, hits=[], capture=capture)
    run(th.exchange_candidates("q", deep=True, query_vec=[1.0, 2.0]))
    assert capture["query_vec"] == [1.0, 2.0]


def test_search_failure_degrades_to_empty_list(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("qdrant down")
    monkeypatch.setattr("backend.brain.semindex.search", boom)
    assert run(th.exchange_candidates("q", deep=True)) == []


def test_hits_with_blank_text_are_skipped(monkeypatch):
    now = 1_000_000.0
    hits = [
        {"exchange_id": "e1", "score": 0.9, "significance": 1.0, "timestamp": now, "text": "   "},
        {"exchange_id": "e2", "score": 0.5, "significance": 0.5, "timestamp": now, "text": "real text"},
    ]
    _patch_search(monkeypatch, hits=hits)
    _freeze_time(monkeypatch, now)
    result = run(th.exchange_candidates("q", deep=True))
    assert len(result) == 1
    assert result[0]["text"] == "real text"
