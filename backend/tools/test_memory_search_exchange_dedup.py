"""Test for Step 2 (2026-09-08): memory_search's kind='exchange' path must
dedupe chunks of the same long exchange back down to one candidate.

Before Step 2, an exchange was always indexed as a single, unchunked point,
so this dedup step was a no-op every time it ran. Once semindex._index_exchange_sync
started chunking anything over ~1200 chars (gte-small silently truncates past
512 tokens, so a long exchange needs multiple points to be fully searchable),
_search_one_kind's exchange branch needed the same per-source dedup discipline
_dedupe_by_file already applies to vault/skill hits -- keyed on exchange_id
instead of (folder, filename). No real Qdrant/embedder touched.
"""

import asyncio

from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


def test_exchange_search_dedupes_chunks_of_the_same_exchange(monkeypatch):
    hits = [
        {"exchange_id": "e1", "score": 0.6, "significance": 0.5, "timestamp": 1000.0,
         "text": "chunk 1 of a long exchange"},
        {"exchange_id": "e1", "score": 0.9, "significance": 0.5, "timestamp": 1000.0,
         "text": "chunk 2 of the SAME long exchange -- higher raw score"},
        {"exchange_id": "e2", "score": 0.5, "significance": 0.5, "timestamp": 1000.0,
         "text": "a completely different exchange"},
    ]

    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None, query_vec=None,
                          session_id=None, rerank=False):
        return list(hits)

    monkeypatch.setattr("backend.brain.semindex.search", fake_search)

    results = run(vault._search_one_kind("exchange", "query", top_k=5, cutoff_ts=None))

    exchange_ids = [h["exchange_id"] for h in results]
    assert exchange_ids.count("e1") == 1, "both chunks of e1 must collapse to a single candidate"
    assert "e2" in exchange_ids
    # The surviving e1 candidate must be the higher-ranked chunk, not whichever
    # happened to be dedupe-first in iteration order.
    e1_result = next(h for h in results if h["exchange_id"] == "e1")
    assert "higher raw score" in e1_result["text"]
