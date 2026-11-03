"""The topic note's filename matcher runs only when the index cannot be searched.

Measured on the server (replay of 40 Discord messages, 2026-10-11): the topic block was
4,000 chars in 21 of 40 deep turns, a size only the filename matcher returns. It ran
whenever the search came back empty, including when the cross-encoder had judged every
passage irrelevant, so "nothing relevant" became the largest block in the prompt, picked
by words in a filename. Reproduced on the desk index: "what time is my stock research call
tomorrow" got 4,000 chars of the Tesla research note.
"""
import asyncio

from backend.brain import semindex
from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


def _vault_with_note(tmp_path, monkeypatch):
    topics = tmp_path / "topics"
    topics.mkdir()
    (topics / "tesla-stock-research.md").write_text(
        "# Tesla stock research\n\n" + "Delivery numbers and margins. " * 300, encoding="utf-8")
    monkeypatch.setattr(vault, "_VAULT", tmp_path)


def _search_finds_nothing(monkeypatch):
    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None,
                          query_vec=None, session_id=None, rerank=False):
        return []
    monkeypatch.setattr(semindex, "search", fake_search)


def test_nothing_relevant_stays_nothing(tmp_path, monkeypatch):
    _vault_with_note(tmp_path, monkeypatch)
    _search_finds_nothing(monkeypatch)
    monkeypatch.setattr(semindex, "can_search", lambda kind, folder=None: True)
    assert run(vault.read_relevant_topic("what time is my stock research call tomorrow")) == ""


def test_filename_match_when_the_index_cannot_be_searched(tmp_path, monkeypatch):
    _vault_with_note(tmp_path, monkeypatch)
    _search_finds_nothing(monkeypatch)
    monkeypatch.setattr(semindex, "can_search", lambda kind, folder=None: False)
    out = run(vault.read_relevant_topic("what about my tesla stock"))
    assert out.startswith("# Tesla stock research")
    assert len(out) == vault._TOPIC_PASSAGE_CAP          # the search path's size, not 4,000


def test_search_hits_are_used_as_before(tmp_path, monkeypatch):
    _vault_with_note(tmp_path, monkeypatch)

    async def fake_search(query, kind="vault", k=3, score_min=None, folder=None,
                          query_vec=None, session_id=None, rerank=False):
        return [{"text": "a relevant passage"}]
    monkeypatch.setattr(semindex, "search", fake_search)
    monkeypatch.setattr(semindex, "can_search", lambda kind, folder=None: True)
    assert run(vault.read_relevant_topic("tesla margins")) == "a relevant passage"


class _Count:
    def __init__(self, n):
        self.count = n


class _FakeClient:
    def __init__(self, n=None, boom=False):
        self.n, self.boom, self.filters = n, boom, []

    def count(self, collection_name, count_filter=None, exact=True):
        if self.boom:
            raise RuntimeError("qdrant down")
        self.filters.append(count_filter)
        return _Count(self.n)


def test_can_search_needs_indexed_points_of_that_folder(monkeypatch):
    client = _FakeClient(n=12)
    monkeypatch.setattr(semindex, "_qdrant", lambda: client)
    assert semindex.can_search("vault", folder="topics") is True
    conds = {(c.key, c.match.value) for c in client.filters[0].must}
    assert conds == {("kind", "vault"), ("folder", "topics")}


def test_cannot_search_an_empty_index(monkeypatch):
    monkeypatch.setattr(semindex, "_qdrant", lambda: _FakeClient(n=0))
    assert semindex.can_search("vault", folder="topics") is False


def test_cannot_search_without_qdrant(monkeypatch):
    monkeypatch.setattr(semindex, "_qdrant", lambda: None)
    assert semindex.can_search("vault", folder="topics") is False
    monkeypatch.setattr(semindex, "_qdrant", lambda: _FakeClient(boom=True))
    assert semindex.can_search("vault", folder="topics") is False
