"""Tests for backend/tools/backup.py — the vault + Qdrant backup.

Pure-function tests plus a filesystem-only integration test (Qdrant snapshot
calls are monkeypatched out — no live Qdrant, no network). Run with:
    .venv\\Scripts\\pytest backend/tools/test_backup.py -v
"""

from pathlib import Path

import pytest

from backend.tools import backup


# ---------------------------------------------------------------------------
# prunable_backups — pure pruning logic
# ---------------------------------------------------------------------------

def test_prunable_backups_empty_list():
    assert backup.prunable_backups([], keep=7) == []


def test_prunable_backups_under_keep_prunes_nothing():
    dirs = ["2026-07-01_000000", "2026-07-02_000000"]
    assert backup.prunable_backups(dirs, keep=7) == []


def test_prunable_backups_exactly_at_keep_prunes_nothing():
    dirs = [f"2026-07-0{i}_000000" for i in range(1, 8)]
    assert backup.prunable_backups(dirs, keep=7) == []


def test_prunable_backups_over_keep_prunes_oldest_first():
    """Retention is by VERIFIED backup now (A00) -- the caller classifies, so this
    passes all four as verified to exercise the ordinary oldest-first behavior."""
    dirs = ["2026-07-03_000000", "2026-07-01_000000", "2026-07-02_000000", "2026-07-04_000000"]
    pruned = backup.prunable_backups(dirs, keep=2, verified=set(dirs))
    assert pruned == ["2026-07-01_000000", "2026-07-02_000000"]


def test_prunable_backups_keep_zero_prunes_everything():
    dirs = ["2026-07-01_000000", "2026-07-02_000000"]
    assert sorted(backup.prunable_backups(dirs, keep=0, verified=set(dirs))) == sorted(dirs)


def test_prunable_backups_negative_keep_treated_as_zero():
    dirs = ["2026-07-01_000000"]
    assert backup.prunable_backups(dirs, keep=-3, verified=set(dirs)) == dirs


def test_prunable_backups_unverified_never_evicts_a_good_backup():
    """The A00 hazard stated directly: a run of failed backups must not push the last
    known-good copy out of retention -- even though it is the OLDEST directory here, so
    a purely count-and-age rule would have dropped it first."""
    good = "2026-07-01_000000"
    failed = ["2026-07-02_000000", "2026-07-03_000000", "2026-07-04_000000"]
    pruned = backup.prunable_backups([good] + failed, keep=1, verified={good})
    assert good not in pruned
    assert pruned == failed          # the junk goes, the one good copy stays


def test_legacy_backups_age_out_one_at_a_time_not_all_at_once():
    """Regression for a bug this code SHIPPED and caused real loss.

    The first version pruned every unverified directory older than the newest verified
    one. On its first live run -- 7 legacy (manifest-less) backups plus one freshly
    verified backup, keep=7 -- that deleted all 7 historical restore points at once.
    Legacy directories are real restore points; one good backup arriving does not make
    seven older ones worthless. They must age out normally, exactly as they did before
    manifests existed."""
    legacy = [f"2026-07-{d:02d}_000000" for d in range(1, 8)]     # 7 legacy
    fresh = "2026-09-22_000000"
    pruned = backup.prunable_backups(legacy + [fresh], keep=7, verified={fresh})
    assert len(pruned) == 1, f"expected one to age out, got {len(pruned)}: {pruned}"
    assert pruned == [legacy[0]]     # only the oldest
    assert fresh not in pruned


def test_prunable_backups_prunes_nothing_when_nothing_is_verified():
    """No good copy anywhere is the worst moment to delete: an unverified directory may
    be the only surviving vault. Also the default when the caller does not classify."""
    dirs = ["2026-07-01_000000", "2026-07-02_000000", "2026-07-03_000000"]
    assert backup.prunable_backups(dirs, keep=1, verified=set()) == []
    assert backup.prunable_backups(dirs, keep=1) == []


def test_prunable_backups_drops_unverified_superseded_by_a_good_copy():
    """Once a verified backup exists, clearly older junk directories can go."""
    dirs = ["2026-07-01_000000", "2026-07-02_000000", "2026-07-03_000000"]
    pruned = backup.prunable_backups(dirs, keep=1, verified={"2026-07-03_000000"})
    assert pruned == ["2026-07-01_000000", "2026-07-02_000000"]


# ---------------------------------------------------------------------------
# _prune_old_backups — actual directory removal
# ---------------------------------------------------------------------------

def _make_verified_backup(root, name):
    """A directory backup_is_verified() genuinely accepts: real manifest, real zip."""
    import json as _json, zipfile as _zipfile
    d = root / name
    (d / "qdrant").mkdir(parents=True, exist_ok=True)
    with _zipfile.ZipFile(d / "vault.zip", "w") as zf:
        zf.writestr("note.md", "hello")
    (d / backup._MANIFEST_NAME).write_text(_json.dumps({
        "version": 1, "created_at": name, "status": "complete",
        "vault_ok": True, "collections_ok": [], "collections_failed": [],
    }), encoding="utf-8")
    return d


def test_prune_old_backups_removes_oldest_dirs(tmp_path, monkeypatch):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    names = ["2026-07-01_000000", "2026-07-02_000000", "2026-07-03_000000", "2026-07-04_000000"]
    for n in names:
        _make_verified_backup(backup_root, n)

    monkeypatch.setattr(backup, "_BACKUP_ROOT", backup_root)
    pruned = backup._prune_old_backups(keep=2)

    assert pruned == 2
    remaining = sorted(p.name for p in backup_root.iterdir())
    assert remaining == names[-2:]


def test_prune_old_backups_keeps_everything_when_none_verified(tmp_path, monkeypatch):
    """Legacy directories with no manifest are never auto-deleted -- they predate the
    manifest and may be the only copy of that data."""
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    for n in ["2026-07-01_000000", "2026-07-02_000000", "2026-07-03_000000"]:
        (backup_root / n).mkdir()

    monkeypatch.setattr(backup, "_BACKUP_ROOT", backup_root)
    assert backup._prune_old_backups(keep=1) == 0
    assert len(list(backup_root.iterdir())) == 3


def test_backup_is_verified_rejects_truncated_vault_zip(tmp_path):
    """A manifest alone is not proof -- artifacts are checked against it."""
    root = tmp_path / "backups"; root.mkdir()
    d = _make_verified_backup(root, "2026-07-01_000000")
    assert backup.backup_is_verified(d) is True
    (d / "vault.zip").write_bytes(b"")
    assert backup.backup_is_verified(d) is False


def test_failed_backup_run_prunes_nothing(tmp_path, monkeypatch):
    """The ordering bug: pruning used to run before this run's outcome was known, so a
    totally failed backup could still evict good ones."""
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    for n in ["2026-07-01_000000", "2026-07-02_000000", "2026-07-03_000000"]:
        _make_verified_backup(backup_root, n)

    monkeypatch.setattr(backup, "_VAULT_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(backup, "_BACKUP_ROOT", backup_root)
    monkeypatch.setattr(backup, "_snapshot_collections", lambda dest: ([], ["<unreachable>"]))

    result = backup.create_backup(keep=1)
    assert result["vault_ok"] is False
    assert result["pruned"] == 0
    assert len([p for p in backup_root.iterdir() if p.is_dir()]) == 3


def test_prune_old_backups_missing_root_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_BACKUP_ROOT", tmp_path / "does-not-exist")
    assert backup._prune_old_backups(keep=2) == 0


# ---------------------------------------------------------------------------
# _zip_vault — real zipfile, no network
# ---------------------------------------------------------------------------

def test_zip_vault_produces_valid_archive(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")
    (vault / "sub").mkdir()
    (vault / "sub" / "nested.md").write_text("nested", encoding="utf-8")

    monkeypatch.setattr(backup, "_VAULT_DIR", vault)
    dest = tmp_path / "vault.zip"
    assert backup._zip_vault(dest) is True

    import zipfile
    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert any(n.endswith("note.md") for n in names)
        assert any(n.endswith("nested.md") for n in names)


def test_zip_vault_missing_directory_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_VAULT_DIR", tmp_path / "does-not-exist")
    assert backup._zip_vault(tmp_path / "vault.zip") is False


# ---------------------------------------------------------------------------
# create_backup — full orchestration, Qdrant snapshot step stubbed out
# ---------------------------------------------------------------------------

def test_create_backup_writes_vault_and_reports_collections(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(backup, "_VAULT_DIR", vault)
    monkeypatch.setattr(backup, "_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(backup, "_snapshot_collections", lambda dest: (["col_a", "col_b"], []))

    result = backup.create_backup(keep=7)

    assert result["vault_ok"] is True
    assert result["collections_ok"] == ["col_a", "col_b"]
    assert result["collections_failed"] == []
    dest = Path(result["dir"])
    assert dest.exists()
    assert (dest / "vault.zip").exists()


def test_create_backup_partial_collection_failure_still_keeps_backup(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(backup, "_VAULT_DIR", vault)
    monkeypatch.setattr(backup, "_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(backup, "_snapshot_collections", lambda dest: (["col_a"], ["col_b"]))

    result = backup.create_backup(keep=7)
    assert result["vault_ok"] is True
    assert result["collections_ok"] == ["col_a"]
    assert result["collections_failed"] == ["col_b"]
    assert Path(result["dir"]).exists()   # partial success is still worth keeping


def test_create_backup_total_failure_cleans_up_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_VAULT_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(backup, "_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(backup, "_snapshot_collections", lambda dest: ([], ["<qdrant unreachable>"]))

    result = backup.create_backup(keep=7)
    assert result["vault_ok"] is False
    assert result["collections_ok"] == []
    assert not Path(result["dir"]).exists()   # no clutter left behind


def test_create_backup_prunes_via_the_real_pipeline(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    # Pre-seed 3 fake older backups so this run (keep=2) must prune down to 2.
    for n in ["2020-01-01_000000", "2020-01-02_000000", "2020-01-03_000000"]:
        (backup_root / n).mkdir()

    monkeypatch.setattr(backup, "_VAULT_DIR", vault)
    monkeypatch.setattr(backup, "_BACKUP_ROOT", backup_root)
    monkeypatch.setattr(backup, "_snapshot_collections", lambda dest: ([], []))

    result = backup.create_backup(keep=2)
    # 3 pre-seeded bare dirs (legacy/unverified) + this run's verified one = 4 total.
    # keep=2 trims the TOTAL down to 2 by dropping the two oldest unverified ones; the
    # remaining legacy directory ages out on a later run rather than all at once.
    assert backup.backup_is_verified(Path(result["dir"])) is True
    assert result["pruned"] == 2
    remaining = sorted(p.name for p in backup_root.iterdir() if p.is_dir())
    assert len(remaining) == 2
    assert Path(result["dir"]).name in remaining
    assert "2020-01-03_000000" in remaining      # newest legacy survives


# ---------------------------------------------------------------------------
# P0: the SQLite runtime database is part of every backup
# ---------------------------------------------------------------------------

def _seed_database(rows: int = 5) -> None:
    """Real schema, real rows, at the path conftest gave this test."""
    from backend.storage import db
    conn = db.connect()
    assert conn is not None
    for i in range(rows):
        conn.execute(
            "INSERT INTO ingress (message_id, channel, received_at) VALUES (?, ?, ?)",
            (f"m{i}", "discord", float(i)),
        )
    conn.commit()


def _table_names(path) -> set:
    import sqlite3
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _backup_env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(backup, "_VAULT_DIR", vault)
    monkeypatch.setattr(backup, "_BACKUP_ROOT", tmp_path / "backups")

    def snapshots(dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "col_a.snapshot").write_bytes(b"snapshot")     # verification checks the file
        return ["col_a"], []

    monkeypatch.setattr(backup, "_snapshot_collections", snapshots)


def test_backup_contains_a_copy_of_the_runtime_database(tmp_path, monkeypatch):
    """The finding: a backup folder held vault.zip and Qdrant snapshots and nothing of
    .jarvis_runtime.db -- exchanges, runs, receipts, forecasts, watches, rules, cost."""
    _backup_env(tmp_path, monkeypatch)
    _seed_database(rows=5)

    result = backup.create_backup(keep=7)

    dest = Path(result["dir"])
    assert result["database"] == "ok"
    assert (dest / "runtime.db").is_file()
    assert backup.backup_is_verified(dest) is True


def test_restored_copy_opens_with_every_table_and_every_row(tmp_path, monkeypatch):
    """Acceptance: a restored backup opens with every table intact."""
    import json
    import sqlite3
    from backend.storage import db
    _backup_env(tmp_path, monkeypatch)
    _seed_database(rows=7)
    live_tables = _table_names(db.db_path())
    assert {"exchanges", "sessions", "ingress"} <= live_tables

    dest = Path(backup.create_backup(keep=7)["dir"])

    assert _table_names(dest / "runtime.db") == live_tables
    conn = sqlite3.connect(f"file:{(dest / 'runtime.db').as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ingress").fetchone()[0] == 7
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database"] == "ok"
    assert manifest["database_tables"]["ingress"] == 7


def test_copy_includes_rows_still_sitting_in_the_wal(tmp_path, monkeypatch):
    """Why the online backup API and not a file copy: the database runs in WAL mode, so
    recent commits live in the -wal file until a checkpoint. Copying only the main file
    silently drops them."""
    import shutil
    import sqlite3
    from backend.storage import db
    _backup_env(tmp_path, monkeypatch)
    _seed_database(rows=9)                 # the connection stays open: nothing checkpointed
    naive = tmp_path / "naive.db"
    shutil.copyfile(db.db_path(), naive)
    try:
        c = sqlite3.connect(str(naive))
        naive_rows = c.execute("SELECT COUNT(*) FROM ingress").fetchone()[0]
        c.close()
    except sqlite3.DatabaseError:
        naive_rows = 0
    assert naive_rows < 9, "test is vacuous: the rows were already checkpointed"

    dest = Path(backup.create_backup(keep=7)["dir"])
    conn = sqlite3.connect(f"file:{(dest / 'runtime.db').as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ingress").fetchone()[0] == 9
    finally:
        conn.close()


def test_copy_is_consistent_while_the_backend_keeps_writing(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from backend.storage import db
    _backup_env(tmp_path, monkeypatch)
    _seed_database(rows=1)
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(str(db.db_path()), timeout=30.0)
        i = 1000
        while not stop.is_set():
            try:
                conn.execute("INSERT INTO ingress (message_id, channel, received_at) "
                             "VALUES (?, 'discord', 0)", (f"w{i}",))
                conn.commit()
            except sqlite3.OperationalError:
                pass
            i += 1
        conn.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        results = [backup._backup_database(tmp_path / f"copy{n}.db") for n in range(5)]
    finally:
        stop.set()
        t.join()
    assert all(r["status"] == "ok" for r in results)
    for n in range(5):
        assert backup._database_copy_intact(tmp_path / f"copy{n}.db", results[n]["tables"])


def test_verifying_a_copy_leaves_no_wal_files_beside_it(tmp_path, monkeypatch):
    """The copy is converted out of WAL mode; otherwise merely opening it to verify would
    create -wal/-shm files in the backup folder (and fail on read-only media)."""
    _backup_env(tmp_path, monkeypatch)
    _seed_database()
    dest = Path(backup.create_backup(keep=7)["dir"])
    assert backup.backup_is_verified(dest) is True
    assert sorted(p.name for p in dest.iterdir() if p.name.startswith("runtime.db")) == ["runtime.db"]


def test_no_database_yet_is_not_a_failed_backup(tmp_path, monkeypatch):
    """A fresh install has nothing to lose; that must not demote the whole backup."""
    _backup_env(tmp_path, monkeypatch)
    from backend.storage import db
    assert not db.db_path().exists()
    result = backup.create_backup(keep=7)
    assert result["database"] == "absent"
    assert backup.backup_is_verified(Path(result["dir"])) is True
    assert not (Path(result["dir"]) / "runtime.db").exists()


def test_failed_database_copy_makes_the_backup_partial_and_unverified(tmp_path, monkeypatch):
    """Vault and Qdrant saved but the database not: not a good copy, and it must say so
    instead of looking complete."""
    import json
    _backup_env(tmp_path, monkeypatch)
    _seed_database()
    monkeypatch.setattr(backup, "_backup_database",
                        lambda dest: {"status": "failed", "tables": {}})
    result = backup.create_backup(keep=7)
    dest = Path(result["dir"])
    assert result["database"] == "failed"
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["status"] == "partial"
    assert backup.backup_is_verified(dest) is False


def test_unreadable_source_database_fails_cleanly_and_leaves_nothing_behind(tmp_path, monkeypatch):
    from backend.storage import db
    _backup_env(tmp_path, monkeypatch)
    db.db_path().write_bytes(b"this is not a sqlite database" * 50)
    dest = tmp_path / "out" / "runtime.db"
    dest.parent.mkdir()
    out = backup._backup_database(dest)
    assert out["status"] == "failed"
    assert list(dest.parent.iterdir()) == []       # no half-written copy, no .tmp


def test_damaged_or_incomplete_copy_is_not_verified(tmp_path, monkeypatch):
    """A manifest is not proof: the copy must still open, pass integrity_check and hold
    the tables the manifest lists."""
    import sqlite3
    _backup_env(tmp_path, monkeypatch)
    _seed_database()
    dest = Path(backup.create_backup(keep=7)["dir"])
    assert backup.backup_is_verified(dest) is True

    conn = sqlite3.connect(str(dest / "runtime.db"))
    conn.execute("DROP TABLE ingress")
    conn.commit()
    conn.close()
    assert backup.backup_is_verified(dest) is False          # a table went missing

    (dest / "runtime.db").write_bytes(b"garbage" * 100)
    assert backup.backup_is_verified(dest) is False          # not a database at all

    (dest / "runtime.db").unlink()
    assert backup.backup_is_verified(dest) is False          # gone


def test_manifests_from_before_the_database_copy_stay_verified(tmp_path):
    """Demoting every existing backup would leave nothing verified, which stops pruning
    entirely (rule 1 of prunable_backups)."""
    root = tmp_path / "backups"
    root.mkdir()
    d = _make_verified_backup(root, "2026-07-01_000000")      # no "database" key
    assert backup.backup_is_verified(d) is True


def test_database_alone_is_worth_keeping(tmp_path, monkeypatch):
    """The vault missing and Qdrant down, but the database saved: keep that folder rather
    than deleting the only copy of the plan log."""
    monkeypatch.setattr(backup, "_VAULT_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(backup, "_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(backup, "_snapshot_collections", lambda dest: ([], ["<qdrant unreachable>"]))
    _seed_database()
    result = backup.create_backup(keep=7)
    assert result["database"] == "ok"
    assert Path(result["dir"]).exists()
    assert (Path(result["dir"]) / "runtime.db").is_file()


def test_copy_with_a_corrupted_data_page_is_not_verified(tmp_path, monkeypatch):
    """The schema page is fine, the table list is intact, and the file opens -- only
    integrity_check can see that a data page is damaged. A copy like this must not count
    as a good backup."""
    _backup_env(tmp_path, monkeypatch)
    _seed_database(rows=4000)
    dest = Path(backup.create_backup(keep=7)["dir"])
    assert backup.backup_is_verified(dest) is True

    copy = dest / "runtime.db"
    raw = bytearray(copy.read_bytes())
    page = 4096
    assert len(raw) > page * 12, "need a multi-page database for this test"
    start = page * 8                                   # a data page, not page 1
    raw[start:start + page] = b"\xff" * page
    copy.write_bytes(bytes(raw))

    import sqlite3
    conn = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ingress" in tables                     # the schema still reads: only integrity_check sees it
    finally:
        conn.close()
    assert backup.backup_is_verified(dest) is False
