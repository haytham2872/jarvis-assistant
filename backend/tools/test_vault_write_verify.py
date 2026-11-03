"""Tests for vault_write / vault_write_queue's write-then-read-back-verify
discipline (2026-09-08 hardening — part of the FOMC scheduling-failure fix).

A persistence tool must never report success unless a read-back of the file
actually confirms the write landed. No real Qdrant/embedder: semindex
indexing is monkeypatched out (fire-and-forget, irrelevant to this contract).
"""

import asyncio

import pytest

from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    monkeypatch.setattr(vault, "_QUEUE_FILE", tmp_path / "queue.md")
    monkeypatch.setattr(vault, "_index_note_async", lambda *a, **k: None)
    return tmp_path


def test_vault_write_confirms_and_names_the_real_path(tmp_vault):
    reply = run(vault.vault_write("note.md", "hello world", "topics"))
    assert "Confirmed written to jarvis-vault/topics/note.md" in reply
    assert (tmp_vault / "topics" / "note.md").read_text(encoding="utf-8") == "hello world"


def test_vault_write_errors_when_readback_does_not_match(tmp_vault, monkeypatch):
    """Simulates a write that silently didn't land as written -- the tool
    must return an Error, never a false 'written' claim."""
    import pathlib
    real_read_text = pathlib.Path.read_text

    def fake_read_text(self, *a, **k):
        if self.name == "note.md":
            return "SOMETHING ELSE ENTIRELY"
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", fake_read_text)
    reply = run(vault.vault_write("note.md", "hello world", "topics"))
    assert reply.startswith("Error:")
    assert "did not verify" in reply


def test_vault_write_queue_confirms_and_names_the_real_path(tmp_vault):
    reply = run(vault.vault_write_queue("[open] 2026-09-08 — test entry"))
    assert "Confirmed written to jarvis-vault/queue.md" in reply
    assert (tmp_vault / "queue.md").read_text(encoding="utf-8") == "[open] 2026-09-08 — test entry"


def test_vault_write_queue_errors_when_readback_does_not_match(tmp_vault, monkeypatch):
    import pathlib
    real_read_text = pathlib.Path.read_text

    def fake_read_text(self, *a, **k):
        if self.name == "queue.md":
            return "STALE CONTENT"
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", fake_read_text)
    reply = run(vault.vault_write_queue("[open] new content"))
    assert reply.startswith("Error:")
    assert "did not verify" in reply
