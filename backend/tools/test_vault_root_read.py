"""vault_read folder="root": the three vault-root files are readable, nothing else is.

Live (2026-09-28): JARVIS could write queue.md with vault_write_queue but not read it
back -- vault_read only reached the named folders -- so he could not verify his own queue.
"""
import asyncio

import pytest

from backend.tools import vault as V


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "jarvis-vault"
    root.mkdir()
    monkeypatch.setattr(V, "_VAULT", root)
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)
    return root


def test_the_queue_written_can_be_read_back(vault):
    assert run(V.vault_write_queue("[open] 2026-09-28 — read me back")).startswith("Confirmed")
    assert "read me back" in run(V.vault_read("queue.md", "root"))


@pytest.mark.parametrize("name", ["research-queue.md", "schedule.md"])
def test_the_other_root_files_are_readable(vault, name):
    (vault / name).write_text("line one\n", encoding="utf-8")
    assert run(V.vault_read(name, "root")).strip() == "line one"


@pytest.mark.parametrize("name", [".env", "../token.json", "notes.md", ".obsidian/app.json"])
def test_nothing_else_at_the_root_is_readable(vault, name):
    (vault / "notes.md").write_text("secret", encoding="utf-8")
    out = run(V.vault_read(name, "root"))
    assert "secret" not in out and ("Only" in out or "Error" in out or "Not found" in out)


def test_root_is_readable_but_not_writable():
    from backend.tools.registry import TOOLS
    enum = lambda t: next(x for x in TOOLS if x["name"] == t)["input_schema"]["properties"]["folder"]["enum"]
    assert "root" in enum("vault_read") and "root" not in enum("vault_write")
