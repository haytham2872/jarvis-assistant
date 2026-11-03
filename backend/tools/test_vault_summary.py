"""Tests for vault.vault_summary() -- the compact, sync vault snapshot added
in Step 5 (2026-09-09) to close a real prompt lie: vault_list's own tool
description has always claimed "the current listing is already injected at
the top of your system prompt," but nothing did the injecting. This is that
injection's data source. No real filesystem beyond tmp_path; no network.
"""

import pytest

from backend.tools import vault


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    return tmp_path


def _touch(path, name, content="x"):
    (path / name).write_text(content, encoding="utf-8")


def test_lists_every_top_level_folder(tmp_vault):
    summary = vault.vault_summary()
    top_level = [f for f in vault.FOLDERS if "/" not in f]
    for folder in top_level:
        assert f"{folder}/" in summary


def test_empty_folder_marked_empty(tmp_vault):
    summary = vault.vault_summary()
    assert "people/ (empty)" in summary


def test_counts_files_and_shows_newest_first(tmp_vault):
    import time
    people_dir = tmp_vault / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    _touch(people_dir, "Old.md")
    time.sleep(0.01)
    _touch(people_dir, "owner.md")

    summary = vault.vault_summary()
    line = [l for l in summary.splitlines() if l.startswith("people/")][0]
    assert "2 files" in line
    # Newest file (owner.md, written last) must be named before Old.md.
    assert line.index("owner.md") < line.index("Old.md")


def test_more_than_recent_cap_shows_a_plus_count(tmp_vault):
    topics_dir = tmp_vault / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        _touch(topics_dir, f"note-{i}.md")

    summary = vault.vault_summary()
    line = [l for l in summary.splitlines() if l.startswith("topics/")][0]
    assert "5 files" in line
    assert "+2 more" in line   # 5 files, only _SUMMARY_RECENT=3 named


def test_subfolder_conversations_significant_excluded(tmp_vault):
    """Matches vault_list(folder=None)'s own existing top-level-only
    convention -- conversations/significant is a subfolder, not a top-level
    one, so it does not get its own line."""
    summary = vault.vault_summary()
    assert "conversations/significant/" not in summary


def test_per_folder_iterdir_failure_degrades_that_folder_only(tmp_vault, monkeypatch):
    """A folder-level failure (permission error, race with a delete) must
    not take down the whole snapshot -- it degrades to '(empty)' for that
    one folder while the rest of the summary is unaffected."""
    def _broken(self):
        raise OSError("disk gone")
    monkeypatch.setattr(vault.Path, "iterdir", _broken)
    summary = vault.vault_summary()
    assert "people/ (empty)" in summary
    assert "topics/ (empty)" in summary


class _ExplodesOnSecondIteration:
    """Stands in for vault.FOLDERS to trigger a failure OUTSIDE the
    per-folder iterdir guard, without monkeypatching pathlib.Path globally
    (that broke pytest's own internal traceback formatting the first time
    this was tried -- vault.Path IS the real pathlib.Path class, process
    wide). vault_summary() calls _ensure_vault() -- which also iterates
    FOLDERS -- before its own try block, so that first iteration must
    succeed; the second (building top_level, inside the try) is where this
    raises."""
    def __init__(self, real):
        self._real = list(real)
        self._used_once = False

    def __iter__(self):
        if not self._used_once:
            self._used_once = True
            return iter(self._real)
        raise RuntimeError("folders exploded")


def test_degrades_to_empty_string_on_a_whole_function_failure(tmp_vault, monkeypatch):
    monkeypatch.setattr(vault, "FOLDERS", _ExplodesOnSecondIteration(vault.FOLDERS))
    assert vault.vault_summary() == ""
