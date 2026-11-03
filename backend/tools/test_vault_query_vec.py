"""Tests for query_vec threading through vault.py's semantic reads.

Scoped narrowly to the embed-reuse change: read_relevant_topic() now accepts
an optional precomputed query_vec and passes it straight through to
semindex.search() instead of
letting those re-embed the same turn's text. No real Qdrant/embedder.
"""

import asyncio

import pytest

from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


def test_read_relevant_topic_passes_query_vec_through(monkeypatch):
    captured = {}

    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None, query_vec=None,
                          session_id=None, rerank=False):
        captured["query_vec"] = query_vec
        return []

    monkeypatch.setattr("backend.brain.semindex.search", fake_search)
    given_vec = [0.5, 0.6]
    run(vault.read_relevant_topic("what's my project about", query_vec=given_vec))
    assert captured["query_vec"] is given_vec


def test_read_relevant_topic_works_with_no_query_vec(monkeypatch):
    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None, query_vec=None,
                          session_id=None, rerank=False):
        assert query_vec is None
        return [{"text": "topic passage"}]

    monkeypatch.setattr("backend.brain.semindex.search", fake_search)
    result = run(vault.read_relevant_topic("some query"))
    assert result == "topic passage"
