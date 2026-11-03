"""Tests for backend/brain/semindex.py — vector reuse and rank interleaving.

Scoped to two things added while unifying memory retrieval: `query_vec`
threading through search()/_search_sync() (so a precomputed embedding can be
reused instead of re-embedding the same turn's text 3-4 times), and
interleave_by_rank(), moved here from tools/vault.py so both memory.py's
recall() and vault.py's memory_search share one implementation. No real
Qdrant or embedder is touched — _qdrant()/embeddings are monkeypatched.
"""

import asyncio

import pytest

from backend.brain import semindex


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# interleave_by_rank — pure, no I/O
# ---------------------------------------------------------------------------

def test_interleave_takes_best_of_each_kind_first():
    """Round 0 is each kind's own #1 candidate, sorted by raw score as a
    tie-break within that round -- vault's 0.9 beats facts' 0.5 for the
    first slot, but facts' #1 (f1) still outranks facts' #2 (f2), which
    only appears in round 1."""
    facts = [{"score": 0.5, "id": "f1"}, {"score": 0.4, "id": "f2"}]
    vault = [{"score": 0.9, "id": "v1"}]
    merged = semindex.interleave_by_rank([facts, vault], top_k=3)
    assert [m["id"] for m in merged] == ["v1", "f1", "f2"]


def test_interleave_respects_top_k_cap():
    facts = [{"score": 0.9, "id": f"f{i}"} for i in range(5)]
    merged = semindex.interleave_by_rank([facts], top_k=2)
    assert len(merged) == 2


def test_interleave_empty_input_returns_empty():
    assert semindex.interleave_by_rank([], top_k=3) == []


def test_interleave_skips_empty_kind_lists():
    facts = [{"score": 0.5, "id": "f1"}]
    merged = semindex.interleave_by_rank([facts, []], top_k=5)
    assert [m["id"] for m in merged] == ["f1"]


def test_interleave_missing_score_sorts_last_in_its_round():
    a = [{"score": 0.5, "id": "a"}]
    b = [{"id": "b"}]   # no score key at all
    merged = semindex.interleave_by_rank([a, b], top_k=2)
    assert [m["id"] for m in merged] == ["a", "b"]


def test_interleave_second_round_pulls_each_kinds_runner_up():
    facts = [{"score": 0.9, "id": "f1"}, {"score": 0.3, "id": "f2"}]
    vault = [{"score": 0.8, "id": "v1"}, {"score": 0.7, "id": "v2"}]
    merged = semindex.interleave_by_rank([facts, vault], top_k=4)
    assert [m["id"] for m in merged] == ["f1", "v1", "v2", "f2"]


# ---------------------------------------------------------------------------
# search() / _search_sync() — query_vec reuse
# ---------------------------------------------------------------------------

class _FakePoint:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


class _FakeQdrant:
    def __init__(self, points):
        self._points = points
        self.last_query_vec = None

    def query_points(self, collection_name, query, query_filter, limit, with_payload):
        self.last_query_vec = query
        class R:
            pass
        r = R()
        r.points = self._points[:limit]
        return r


def test_search_reuses_a_given_query_vec_without_embedding(monkeypatch):
    fake_client = _FakeQdrant([_FakePoint(0.9, {"text": "hit"})])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)

    embed_calls = []
    monkeypatch.setattr(
        "backend.brain.embeddings._embed_many_sync",
        lambda texts, background=False: embed_calls.append(texts) or [[9.9]],
    )

    given_vec = [1.0, 2.0, 3.0]
    hits = run(semindex.search("some query", kind="vault", query_vec=given_vec))

    assert embed_calls == [], "must not re-embed when a vector is already given"
    assert fake_client.last_query_vec == given_vec
    assert hits and hits[0]["text"] == "hit"


def test_search_embeds_when_no_query_vec_given(monkeypatch):
    fake_client = _FakeQdrant([_FakePoint(0.9, {"text": "hit"})])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)

    embed_calls = []
    monkeypatch.setattr(
        "backend.brain.embeddings._embed_many_sync",
        lambda texts, background=False: embed_calls.append(texts) or [[4.0, 5.0]],
    )

    hits = run(semindex.search("some query", kind="vault"))

    assert embed_calls == [["some query"]]
    assert fake_client.last_query_vec == [4.0, 5.0]
    assert hits and hits[0]["text"] == "hit"


def test_search_score_min_still_filters_with_a_given_query_vec(monkeypatch):
    fake_client = _FakeQdrant([_FakePoint(0.1, {"text": "too weak"})])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)
    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", lambda texts, background=False: [[0.0]])

    hits = run(semindex.search("q", kind="vault", score_min=0.5, query_vec=[1.0]))
    assert hits == []


# ---------------------------------------------------------------------------
# Passive-recall default floors -- regression guard for the 2026-09-09
# incident (see semindex.py's _VAULT_SCORE_MIN/_EXCHANGE_SCORE_MIN comment).
# The bug was these defaults sitting at 0.30, three times below the measured
# 0.72-0.85 unrelated-query noise ceiling on this same collection/embedder --
# effectively no gate at all for every caller that omits score_min (i.e.
# every passive per-turn recall path: _semantic_vault_passage,
# exchange_candidates, read_relevant_topic). Pinned to the SAME calibrated
# numbers tools/vault.py's memory_search tool already uses
# (_SEARCH_SCORE_MIN), so the two can't silently drift apart again.
# ---------------------------------------------------------------------------

def test_passive_recall_default_floors_match_calibrated_tool_floors():
    from backend.tools.vault import _SEARCH_SCORE_MIN

    assert semindex._VAULT_SCORE_MIN == _SEARCH_SCORE_MIN["vault"]
    assert semindex._EXCHANGE_SCORE_MIN == _SEARCH_SCORE_MIN["exchange"]


def test_passive_recall_default_floors_are_not_the_old_uncalibrated_placeholder():
    # 0.30 is well below the documented 0.72-0.85 unrelated-query baseline
    # for this embedder/collection -- a floor anywhere near it filters
    # nothing and is a regression back to the incident, not a valid tuning.
    assert semindex._VAULT_SCORE_MIN >= 0.7
    assert semindex._EXCHANGE_SCORE_MIN >= 0.7


def test_search_default_floor_rejects_mid_relevance_vault_hit(monkeypatch):
    """A hit that would have passed the old 0.30 default (and could have,
    live, been an unrelated-but-locally-best-match vault passage) must now
    be rejected by the real default score_min, not just an explicit one."""
    fake_client = _FakeQdrant([_FakePoint(0.5, {"text": "mid-relevance noise"})])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)

    hits = run(semindex.search("q", kind="vault", query_vec=[1.0]))
    assert hits == []


def test_search_default_floor_rejects_mid_relevance_exchange_hit(monkeypatch):
    fake_client = _FakeQdrant([_FakePoint(0.5, {"text": "mid-relevance noise"})])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)

    hits = run(semindex.search("q", kind="exchange", query_vec=[1.0]))
    assert hits == []


def test_search_empty_query_returns_empty_even_with_query_vec(monkeypatch):
    """Empty-string guard must fire before touching Qdrant at all."""
    called = {"qdrant": False}

    def fake_qdrant():
        called["qdrant"] = True
        return _FakeQdrant([])

    monkeypatch.setattr(semindex, "_qdrant", fake_qdrant)
    hits = run(semindex.search("   ", kind="vault", query_vec=[1.0]))
    assert hits == []
    assert called["qdrant"] is False


# ---------------------------------------------------------------------------
# search() — session_id filter (Step 2, 2026-09-08)
# ---------------------------------------------------------------------------

class _FilterCapturingQdrant:
    def __init__(self, points):
        self._points = points
        self.last_filter = None

    def query_points(self, collection_name, query, query_filter, limit, with_payload):
        self.last_filter = query_filter
        class R:
            pass
        r = R()
        r.points = self._points[:limit]
        return r


def test_search_without_session_id_has_no_session_condition(monkeypatch):
    fake_client = _FilterCapturingQdrant([])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)
    run(semindex.search("q", kind="exchange", query_vec=[1.0]))
    field_keys = [c.key for c in fake_client.last_filter.must]
    assert "session_id" not in field_keys


def test_search_with_session_id_adds_a_matching_condition(monkeypatch):
    fake_client = _FilterCapturingQdrant([])
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)
    run(semindex.search("q", kind="exchange", query_vec=[1.0], session_id="s1"))
    session_conds = [c for c in fake_client.last_filter.must if c.key == "session_id"]
    assert len(session_conds) == 1
    assert session_conds[0].match.value == "s1"


# ---------------------------------------------------------------------------
# _index_exchange_sync — chunking (Step 2, 2026-09-08)
# ---------------------------------------------------------------------------

class _UpsertCapturingQdrant:
    def __init__(self):
        self.upserted_points = None

    def get_collections(self):
        class C:
            collections = []
        return C()

    def create_collection(self, **kwargs):
        pass

    def upsert(self, collection_name, points):
        self.upserted_points = points


def test_short_exchange_indexes_as_a_single_point(monkeypatch):
    fake_client = _UpsertCapturingQdrant()
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)
    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", lambda texts, background=False: [[0.1]] * len(texts))

    ok = semindex._index_exchange_sync(
        text="short exchange", session_id="s1", significance=0.5, timestamp=1000.0,
        channel="local", summary="a summary", exchange_id="e1",
    )
    assert ok is True
    assert len(fake_client.upserted_points) == 1
    payload = fake_client.upserted_points[0].payload
    assert payload["exchange_id"] == "e1"
    assert payload["summary"] == "a summary"
    assert payload["channel"] == "local"


def test_long_exchange_is_chunked_into_multiple_points_sharing_one_exchange_id():
    """Live-verified (2026-09-08): gte-small's max_seq_length is 512 tokens
    (a hard architectural limit -- max_position_embeddings is also 512) and
    SentenceTransformer silently truncates past it with no error or warning
    -- encoding ~2000 words produced a vector with 0.9999 cosine similarity
    to encoding just the first ~505 words. An exchange longer than one
    vault-style chunk (~1200 chars) must therefore be split, same as the
    vault indexing path, or its tail is permanently unfindable."""
    fake_client = _UpsertCapturingQdrant()
    import unittest.mock
    with unittest.mock.patch.object(semindex, "_qdrant", lambda: fake_client), \
         unittest.mock.patch(
             "backend.brain.embeddings._embed_many_sync",
             lambda texts, background=False: [[0.1]] * len(texts),
         ):
        long_text = "This is one exchange. " * 300  # well over 1200 chars
        ok = semindex._index_exchange_sync(
            text=long_text, session_id="s1", significance=0.5, timestamp=1000.0,
            channel="local", summary="summary", exchange_id="e1",
        )
    assert ok is True
    points = fake_client.upserted_points
    assert len(points) > 1, "a long exchange must be split into multiple points"
    assert all(p.payload["exchange_id"] == "e1" for p in points), \
        "every chunk must share the same exchange_id so search-time dedup can collapse them"
    # Reassembling the chunks (accounting for the chunker's overlap) must
    # cover the whole original text, not silently drop the tail.
    assert points[-1].payload["text"] in long_text


def test_blank_text_indexes_nothing(monkeypatch):
    fake_client = _UpsertCapturingQdrant()
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)
    ok = semindex._index_exchange_sync(
        text="   ", session_id="s1", significance=0.0, timestamp=1000.0,
    )
    assert ok is False
    assert fake_client.upserted_points is None


# ---------------------------------------------------------------------------
# Point-count cache (Step 2, 2026-09-08) -- runtime_status.py's growth
# visibility. cached_point_count()/warm_point_count() must never touch
# Qdrant synchronously (runtime_status_text() is documented and called as
# fully synchronous, no network).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_point_count_cache():
    original = semindex._cached_point_count
    semindex._cached_point_count = None
    yield
    semindex._cached_point_count = original


def test_cached_point_count_is_none_before_warming():
    assert semindex.cached_point_count() is None


def test_warm_point_count_seeds_the_cache(monkeypatch):
    async def fake_point_count():
        return 42
    monkeypatch.setattr(semindex, "point_count", fake_point_count)
    run(semindex.warm_point_count())
    assert semindex.cached_point_count() == 42


def test_note_points_indexed_increments_seeded_cache():
    semindex._cached_point_count = 10
    semindex._note_points_indexed(3)
    assert semindex.cached_point_count() == 13


def test_note_points_indexed_is_a_noop_before_seeding():
    assert semindex._cached_point_count is None
    semindex._note_points_indexed(5)
    assert semindex.cached_point_count() is None


def test_index_exchange_sync_increments_the_cache_on_success(monkeypatch):
    fake_client = _UpsertCapturingQdrant()
    monkeypatch.setattr(semindex, "_qdrant", lambda: fake_client)
    monkeypatch.setattr("backend.brain.embeddings._embed_many_sync", lambda texts, background=False: [[0.1]] * len(texts))
    semindex._cached_point_count = 100

    semindex._index_exchange_sync(
        text="short exchange", session_id="s1", significance=0.5, timestamp=1000.0,
        exchange_id="e1",
    )
    assert semindex.cached_point_count() == 101
