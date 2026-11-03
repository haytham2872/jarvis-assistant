"""A03 slice 6: a note written this turn is findable before indexing catches up.

The gap: `vault_write()` fires `semindex.index_file()` fire-and-forget, and embedding
plus upsert is measured at ~2.8s. Until it lands the note is on disk but invisible to
`memory_search` — so a turn that writes a note and searches for it two tool calls later
finds nothing and can reasonably conclude the write failed.
"""
import asyncio

import pytest

from backend.context import recent_writes
from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    recent_writes.reset()
    yield
    recent_writes.reset()


# ------------------------------------------------------------------ registry
def test_recorded_write_is_findable_by_token_overlap():
    recent_writes.record("topics", "nvda-thesis.md",
                         "NVDA datacenter demand remains the core thesis.")
    hits = recent_writes.matching_hits("what is my nvda thesis")
    assert len(hits) == 1
    assert hits[0]["filename"] == "nvda-thesis.md"
    assert hits[0]["_pending_index"] is True


def test_unrelated_query_matches_nothing():
    recent_writes.record("topics", "nvda-thesis.md", "NVDA datacenter demand.")
    assert recent_writes.matching_hits("what did I have for lunch") == []


def test_short_tokens_are_ignored():
    """Two-letter noise must not make everything match everything."""
    recent_writes.record("topics", "x.md", "a b c the and of")
    assert recent_writes.matching_hits("the and of a b c") == []


def test_score_is_zero_not_a_fabricated_similarity():
    """These notes are unindexed by definition -- there is no vector to compare, and
    inventing a similarity would put them on a scale they do not share with real hits."""
    recent_writes.record("topics", "espresso.md", "Lelit Bianca grinder settings")
    assert recent_writes.matching_hits("espresso grinder")[0]["score"] == 0.0


def test_already_indexed_notes_are_excluded():
    """A note the index already returned must appear once, with its real score."""
    recent_writes.record("topics", "nvda.md", "NVDA thesis")
    assert recent_writes.matching_hits("nvda", exclude={("topics", "nvda.md")}) == []


def test_rewriting_a_file_replaces_its_entry():
    recent_writes.record("topics", "n.md", "first version datacenter")
    recent_writes.record("topics", "n.md", "second version datacenter")
    hits = recent_writes.matching_hits("datacenter")
    assert len(hits) == 1
    assert "second version" in hits[0]["text"]


def test_registry_is_bounded():
    for i in range(40):
        recent_writes.record("topics", f"note{i}.md", "datacenter content here")
    assert len(recent_writes.pending()) == recent_writes._MAX_ENTRIES


def test_content_preview_is_bounded():
    recent_writes.record("topics", "big.md", "datacenter " * 5000)
    assert len(recent_writes.pending()[0].content) == recent_writes._PREVIEW_CHARS


def test_reset_clears_between_turns():
    recent_writes.record("topics", "n.md", "datacenter")
    recent_writes.reset()
    assert recent_writes.pending() == []


# ------------------------------------------------- wired into memory_search
def test_merge_appends_pending_writes_for_vault_kinds():
    recent_writes.record("topics", "fresh.md", "brand new datacenter note")
    merged = vault._merge_pending_writes([], "datacenter note", "vault", 3)
    assert [h["filename"] for h in merged] == ["fresh.md"]


def test_merge_is_skipped_for_non_vault_kinds():
    recent_writes.record("topics", "fresh.md", "brand new datacenter note")
    assert vault._merge_pending_writes([], "datacenter", "exchange", 3) == []
    assert vault._merge_pending_writes([], "datacenter", "fact", 3) == []


def test_real_hits_come_first_and_are_not_displaced():
    """A genuine semantic match is better evidence than a lexical fallback."""
    recent_writes.record("topics", "fresh.md", "datacenter note")
    real = [{"_kind": "vault", "folder": "topics", "filename": "established.md",
             "text": "datacenter", "score": 0.91}]
    merged = vault._merge_pending_writes(real, "datacenter", "all", 3)
    assert merged[0]["filename"] == "established.md"
    assert any(h.get("_pending_index") for h in merged[1:])


def test_indexed_note_is_not_duplicated():
    recent_writes.record("topics", "same.md", "datacenter note")
    real = [{"_kind": "vault", "folder": "topics", "filename": "same.md",
             "text": "datacenter note", "score": 0.88}]
    merged = vault._merge_pending_writes(real, "datacenter", "all", 3)
    assert len(merged) == 1
    assert merged[0]["score"] == 0.88          # the real hit, not the fallback


def test_merge_failure_returns_the_original_hits(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(recent_writes, "matching_hits", _boom)
    original = [{"_kind": "vault", "folder": "t", "filename": "a.md", "score": 0.9}]
    assert vault._merge_pending_writes(original, "q", "all", 3) == original


def test_pending_hit_is_labelled_in_the_rendered_source():
    """A '(score 0.00)' with no explanation would read as a weak match."""
    label = vault._format_hit_source({
        "_kind": "vault", "folder": "topics", "filename": "fresh.md",
        "_pending_index": True,
    })
    assert "topics/fresh.md" in label
    assert "not yet indexed" in label


def test_vault_write_records_into_the_registry(tmp_path, monkeypatch):
    """The real wiring: writing a note must make it findable in the same turn."""
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    for folder in vault.FOLDERS:
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    run(vault.vault_write("datacenter-thesis.md",
                          "NVDA datacenter demand is the thesis.", "topics"))
    hits = recent_writes.matching_hits("datacenter thesis")
    assert [h["filename"] for h in hits] == ["datacenter-thesis.md"]
