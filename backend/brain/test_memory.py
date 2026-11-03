"""Tests for backend/brain/memory.py — the unified, relevance-ranked recall().

Covers the redesign that replaced a fixed facts-always-first priority order
with semindex.interleave_by_rank() (whichever of facts/vault_passage/exchange
is most relevant THIS turn fills the shared budget first), the per-kind fair
budget share that stops one kind from structurally crowding out the others,
and query_vec threading so recall() doesn't re-embed the same turn's text
multiple times. No real mem0/Qdrant/embedder is touched.
"""

import asyncio

import pytest

from backend.brain import memory as mem


def run(coro):
    return asyncio.run(coro)


def _patch(monkeypatch, **kwargs):
    """Monkeypatch recall()'s three gathered sources. Each kwarg is the return
    value (already the shape the real function would return) -- `exchange`
    is a single dict (test convenience), wrapped into the list
    exchange_candidates() actually returns."""
    async def vault(query, query_vec=None):
        return kwargs.get("vault_passage")

    async def exchange(query, deep=True, query_vec=None, k=3, channel=None, exclude_since=None):
        c = kwargs.get("exchange")
        return [c] if c else []

    async def sleep():
        return kwargs.get("sleep_info", "")

    monkeypatch.setattr(mem, "_semantic_vault_passage", vault)
    monkeypatch.setattr("backend.brain.threads.exchange_candidates", exchange)
    monkeypatch.setattr(mem, "_format_sleep_info", sleep)


# ---------------------------------------------------------------------------
# Ranking — the core of the redesign
# ---------------------------------------------------------------------------

def test_both_kinds_fit_when_there_is_room(monkeypatch):
    _patch(
        monkeypatch,
        exchange={"text": "weak exchange", "score": 0.1},
        vault_passage={"text": "strong vault passage", "score": 0.95},
    )
    result = run(mem.recall("some query", deep=True))
    assert result.vault_passage == "strong vault passage"
    assert result.exchange == "weak exchange"


def test_tight_budget_drops_the_lower_ranked_candidate_not_the_last_checked_one(monkeypatch):
    # Leaves only 50 chars of budget after vault_passage fills -- too little
    # for the 200-char exchange that would otherwise have fit fine on its own,
    # so it must be the one dropped, not simply whichever ran last.
    big_text = "x" * (mem._MEMORY_BUDGET_CHARS - 50)
    _patch(
        monkeypatch,
        exchange={"text": "y" * 200, "score": 0.05},
        vault_passage={"text": big_text, "score": 0.9},
    )
    result = run(mem.recall("q", deep=True))
    assert result.vault_passage == big_text
    assert result.exchange == "", "the weaker-ranked candidate should be the one dropped for space"


def test_recall_never_searches_mem0(monkeypatch):
    """Passive fact recall is gone (2026-10-11). Replaying 40 Discord messages on the
    server it injected nothing in 40 of 40 deep turns: facts are marked consolidated when
    their session closes, so only the open session's facts were eligible, and the live
    history already holds that session. memory_search still searches facts on request."""
    def no_mem():
        raise AssertionError("recall() reached mem0")
    monkeypatch.setattr(mem, "_get_mem", no_mem)
    monkeypatch.setattr(mem, "_semantic_vault_passage", lambda query, query_vec=None: _async_none())
    monkeypatch.setattr("backend.brain.threads.exchange_candidates",
                        lambda *a, **kw: _async_list([]))
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))
    assert run(mem.recall("q", deep=True)).facts == ""


def test_no_candidates_returns_all_empty(monkeypatch):
    _patch(monkeypatch)
    result = run(mem.recall("q", deep=True))
    assert result.facts == result.vault_passage == result.exchange == result.sleep_info == ""


def test_only_one_kind_present_still_works(monkeypatch):
    _patch(monkeypatch, exchange={"text": "past exchange text", "score": 0.7})
    result = run(mem.recall("q", deep=True))
    assert result.exchange == "past exchange text"
    assert result.facts == ""
    assert result.vault_passage == ""


def test_multiple_exchange_candidates_are_accumulated_not_overwritten(monkeypatch):
    """Step 2 (2026-09-08): exchange_candidates() can return up to 3 items,
    each independently winning a slot in interleave_by_rank's fill order --
    the second and third must be ACCUMULATED into result.exchange, not
    silently clobber the first one (a real bug the naive re-use of the old
    single-field-overwrite loop would have reintroduced)."""
    async def exchange(query, deep=True, query_vec=None, k=3, channel=None, exclude_since=None):
        return [
            {"text": "best exchange", "score": 0.9},
            {"text": "second exchange", "score": 0.6},
        ]

    monkeypatch.setattr(mem, "_semantic_vault_passage", lambda query, query_vec=None: _async_none())
    monkeypatch.setattr("backend.brain.threads.exchange_candidates", exchange)
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))

    result = run(mem.recall("q", deep=True))
    assert "best exchange" in result.exchange
    assert "second exchange" in result.exchange


def test_exchange_candidate_dropped_when_budget_too_tight(monkeypatch):
    """A second exchange candidate that doesn't fit the remaining budget must
    be skipped, not force its way in and blow past the shared budget."""
    big_text = "x" * (mem._MEMORY_BUDGET_CHARS - 20)

    async def exchange(query, deep=True, query_vec=None, k=3, channel=None, exclude_since=None):
        return [
            {"text": big_text, "score": 0.9},
            {"text": "y" * 100, "score": 0.6},  # doesn't fit after the first
        ]

    monkeypatch.setattr(mem, "_semantic_vault_passage", lambda query, query_vec=None: _async_none())
    monkeypatch.setattr("backend.brain.threads.exchange_candidates", exchange)
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))

    result = run(mem.recall("q", deep=True))
    assert big_text in result.exchange
    assert "y" * 100 not in result.exchange


# ---------------------------------------------------------------------------
# Sleep info — deliberately NOT part of the relevance ranking
# ---------------------------------------------------------------------------

def test_sleep_info_always_last_priority_regardless_of_ranking(monkeypatch):
    """Sleep info competes for leftover budget only, after all three ranked
    candidates -- it is never promoted ahead of a relevant memory hit."""
    _patch(
        monkeypatch,
        vault_passage={"text": "a passage", "score": 0.9},
        sleep_info="[Last sleep]\nslept a while",
    )
    result = run(mem.recall("q", deep=True))
    assert result.vault_passage == "a passage"
    assert result.sleep_info == "[Last sleep]\nslept a while"


def test_sleep_info_dropped_when_no_room_left(monkeypatch):
    big = {"text": "x" * (mem._MEMORY_BUDGET_CHARS - 10), "score": 0.9}
    _patch(monkeypatch, vault_passage=big, sleep_info="[Last sleep]\nslept a while")
    result = run(mem.recall("q", deep=True))
    assert result.vault_passage == big["text"]
    assert result.sleep_info == ""


# ---------------------------------------------------------------------------
# deep gating: the vault passage and exchanges are recalled on deep turns only.
# The session-scoped exchange search that used to run on every turn returned what
# the live history already holds (2026-10-11).
# ---------------------------------------------------------------------------

def test_shallow_turn_skips_the_vault(monkeypatch):
    called = {"vault": False}

    async def vault_spy(query, query_vec=None):
        called["vault"] = True
        return None

    monkeypatch.setattr(mem, "_semantic_vault_passage", vault_spy)
    monkeypatch.setattr("backend.brain.threads.exchange_candidates",
                         lambda *a, **kw: _async_list([]))
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))

    run(mem.recall("q", deep=False))
    assert called["vault"] is False


def test_recall_passes_the_live_history_boundary(monkeypatch):
    """Exchanges the live history already holds are left out: recall() hands the turn's
    channel and the time of the oldest turn in its history to exchange_candidates()."""
    seen = {}

    async def exchange_spy(query, deep=True, query_vec=None, k=3, channel=None, exclude_since=None):
        seen.update(deep=deep, channel=channel, exclude_since=exclude_since)
        return [{"text": "from an older conversation", "score": 0.6}]

    monkeypatch.setattr(mem, "_semantic_vault_passage", lambda query, query_vec=None: _async_none())
    monkeypatch.setattr("backend.brain.threads.exchange_candidates", exchange_spy)
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))

    result = run(mem.recall("q", deep=True, channel="discord", history_since=123.0))
    assert seen == {"deep": True, "channel": "discord", "exclude_since": 123.0}
    assert result.exchange == "from an older conversation"


async def _async_none():
    return None


async def _async_list(x):
    return x


def test_shallow_turn_uses_half_budget():
    assert mem._MEMORY_BUDGET_CHARS // 2 < mem._MEMORY_BUDGET_CHARS


# ---------------------------------------------------------------------------
# query_vec threading
# ---------------------------------------------------------------------------

def test_query_vec_is_passed_to_vault_and_exchange_searches(monkeypatch):
    seen = {}

    async def vault_spy(query, query_vec=None):
        seen["vault_vec"] = query_vec
        return None

    async def exchange_spy(query, deep=True, query_vec=None, k=3, channel=None, exclude_since=None):
        seen["exchange_vec"] = query_vec
        return []

    monkeypatch.setattr(mem, "_semantic_vault_passage", vault_spy)
    monkeypatch.setattr("backend.brain.threads.exchange_candidates", exchange_spy)
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))

    given_vec = [0.1, 0.2, 0.3]
    run(mem.recall("q", deep=True, query_vec=given_vec))

    assert seen["vault_vec"] is given_vec
    assert seen["exchange_vec"] is given_vec


async def _async_str(s):
    return s


# ---------------------------------------------------------------------------
# _search_facts_sync (the memory_search tool) still reaches mem0
# ---------------------------------------------------------------------------

class _FakeMem:
    def __init__(self, items):
        self._items = items

    # Mirrors mem0 2.0.1's real signature: the param is top_k (keyword-only),
    # NOT limit -- the old limit= kwarg is silently swallowed by **kwargs in
    # the real library, which is exactly the bug the 2026-09-01 canary
    # investigation surfaced. No **kwargs here on purpose: a call site that
    # regresses to limit= fails this fake loudly instead of silently.
    def search(self, query, *, top_k=20, filters=None):
        return {"results": self._items[:top_k]}


def test_search_facts_includes_consolidated_facts(monkeypatch):
    monkeypatch.setattr(mem, "_get_mem", lambda: _FakeMem([
        {"id": "1", "memory": "already in vault", "score": 0.9},
        {"id": "2", "memory": "still open", "score": 0.8},
    ]))
    monkeypatch.setattr(mem, "_get_consolidated_ids", lambda: {"1"})
    monkeypatch.setattr(mem, "_get_assistant_attributed_ids", lambda: set())
    out = mem._search_facts_sync("q", 5)
    assert [h["text"] for h in out] == ["already in vault", "still open"]


def test_a_passage_and_three_exchanges_all_fit(monkeypatch):
    async def exchange(query, deep=True, query_vec=None, k=3, channel=None, exclude_since=None):
        return [{"text": f"exchange {i}", "score": 0.9 - i / 10} for i in range(3)]

    async def vault(query, query_vec=None):
        return {"text": "a passage", "score": 0.95}

    monkeypatch.setattr(mem, "_semantic_vault_passage", vault)
    monkeypatch.setattr("backend.brain.threads.exchange_candidates", exchange)
    monkeypatch.setattr(mem, "_format_sleep_info", lambda: _async_str(""))
    result = run(mem.recall("q", deep=True))
    assert result.vault_passage == "a passage"
    assert all(f"exchange {i}" in result.exchange for i in range(3))
