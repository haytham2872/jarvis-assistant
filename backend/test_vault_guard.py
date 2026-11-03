"""The real-vault rail in conftest.py must actually detect changes (not pass vacuously).

Exercised against a temp directory standing in for the vault -- proving a guard by
writing to the thing it guards would be the very mistake it exists to catch.
"""
import time

from backend import conftest


def _fp(tmp_path, monkeypatch):
    monkeypatch.setattr(conftest, "_REAL_VAULT", tmp_path)
    return conftest._vault_fingerprint()


def test_detects_a_modified_file(tmp_path, monkeypatch):
    (tmp_path / "queue.md").write_text("a", encoding="utf-8")
    before = _fp(tmp_path, monkeypatch)
    time.sleep(0.01)
    (tmp_path / "queue.md").write_text("fixture text", encoding="utf-8")
    assert _fp(tmp_path, monkeypatch) != before


def test_detects_created_and_deleted_files_in_subfolders(tmp_path, monkeypatch):
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "evolution.md").write_text("x", encoding="utf-8")
    before = _fp(tmp_path, monkeypatch)
    (tmp_path / "self" / "evolution.md").unlink()
    (tmp_path / "self" / "new.md").write_text("y", encoding="utf-8")
    after = _fp(tmp_path, monkeypatch)
    assert "self/evolution.md" in before and "self/evolution.md" not in after
    assert "self/new.md" in after


def test_ignores_obsidian_workspace_churn(tmp_path, monkeypatch):
    (tmp_path / ".obsidian").mkdir()
    before = _fp(tmp_path, monkeypatch)
    (tmp_path / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    assert _fp(tmp_path, monkeypatch) == before


def test_a_missing_vault_is_not_an_error(tmp_path, monkeypatch):
    assert _fp(tmp_path / "absent", monkeypatch) is None
