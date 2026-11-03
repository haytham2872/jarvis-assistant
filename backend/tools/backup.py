"""backend/tools/backup.py — Durability for the vault + Qdrant memory.

Everything JARVIS is — letters, the the owner portrait, skills, mood, threads,
consolidated facts — lives in three places: the jarvis-vault/ folder, the
qdrant_data Docker volume and the SQLite runtime database (.jarvis_runtime.db:
exchanges, runs, receipts, forecasts, watches, standing rules, approvals and the
plan life log). A corrupted volume or an accidental `docker compose down -v`
erases all of it permanently. This module is the fix: zip the vault, snapshot
every live Qdrant collection and copy the database into a dated backups/ folder,
pruning old ones beyond a keep count.

The database copy uses SQLite's online backup API, which is consistent while the
backend keeps writing (copying the file directly, or `cp` on a WAL database, can
capture a torn state). It is converted to a single self-contained file, checked
with PRAGMA integrity_check before it counts, and its tables are recorded in the
manifest so a restore can be compared with what was backed up. It was missing
until the control-and-planning roadmap (P0): two months of plan data must not be
the next thing to depend on an unbacked file.

Two callers, same core `create_backup()`:
  - Standalone script (`python -m backend.tools.backup`), invoked by
    teardown.ps1 on every graceful stop, while Qdrant is still up and before
    `docker compose stop`. Deliberately NOT folded into the /shutdown HTTP
    endpoint -- that call already runs consolidation (which can itself take
    a while) under teardown.ps1's 20s timeout budget; stacking a multi-
    collection snapshot backup onto the same request risked the client
    timing out and teardown.ps1 taskkill-ing the process mid-backup.
    Running as its own process decouples the two failure modes entirely.
  - `backend.brain.backup_loop`, a periodic in-process safety net for a
    session that runs for days without ever being gracefully stopped.

Fully synchronous (zipfile, qdrant_client, httpx.Client are all blocking) --
callers that need this off the event loop wrap it in run_in_executor, same
pattern as every other blocking Qdrant call in this codebase. Same silent-
degradation contract: any single collection or the vault zip can fail
without aborting the rest of the backup.
"""

import os
import json
import shutil
import sqlite3
import zipfile
import logging
import datetime
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_VAULT_DIR    = _PROJECT_ROOT / "jarvis-vault"
_BACKUP_ROOT  = _PROJECT_ROOT / "backups"

_KEEP_LAST   = 7    # dated backup folders retained; oldest pruned first
# `localhost` pinned to the IPv4 loopback Qdrant is bound on -- see storage/qdrant.py.
from backend.storage.qdrant import host as _qdrant_host
_QDRANT_HOST = _qdrant_host()
_QDRANT_PORT = 6333
_SNAPSHOT_TIMEOUT = 60.0   # seconds — snapshot creation + download per collection
_MANIFEST_NAME = "manifest.json"   # A00: proof a backup actually completed
_DB_NAME = "runtime.db"            # P0: the SQLite runtime database copy


def _zip_vault(dest_zip: Path) -> bool:
    """Zip jarvis-vault/ whole. False (not raised) on any failure."""
    if not _VAULT_DIR.exists():
        log.warning("backup: jarvis-vault/ does not exist — nothing to zip")
        return False
    try:
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in _VAULT_DIR.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(_VAULT_DIR.parent))
        return True
    except Exception as e:
        log.warning("backup: vault zip failed: %s", e)
        return False


def _snapshot_collections(dest_dir: Path) -> tuple[list[str], list[str]]:
    """Snapshot every live Qdrant collection to dest_dir/<name>.snapshot.

    Returns (succeeded, failed) collection-name lists. A single collection's
    snapshot/download failure doesn't stop the others. The server-side
    snapshot is deleted after a successful download so Qdrant doesn't
    silently accumulate them release over release — the downloaded copy IS
    the backup.
    """
    ok: list[str] = []
    failed: list[str] = []
    try:
        client = QdrantClient(host=_QDRANT_HOST, port=_QDRANT_PORT, timeout=10)
        names = [c.name for c in client.get_collections().collections]
    except Exception as e:
        log.warning("backup: cannot reach Qdrant (%s) — skipping all collection snapshots", e)
        return ok, ["<qdrant unreachable>"]

    if not names:
        return ok, failed

    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=_SNAPSHOT_TIMEOUT) as http:
        for name in names:
            try:
                snap = client.create_snapshot(collection_name=name)
                url = f"http://{_QDRANT_HOST}:{_QDRANT_PORT}/collections/{name}/snapshots/{snap.name}"
                resp = http.get(url)
                resp.raise_for_status()
                (dest_dir / f"{name}.snapshot").write_bytes(resp.content)
                try:
                    client.delete_snapshot(collection_name=name, snapshot_name=snap.name)
                except Exception as e:
                    log.debug("backup: server-side snapshot cleanup failed for %s: %s", name, e)
                ok.append(name)
            except Exception as e:
                log.warning("backup: snapshot failed for collection %r: %s", name, e)
                failed.append(name)
    return ok, failed


def _database_source() -> Path:
    from backend.storage import db
    return db.db_path()


def _table_counts(conn: sqlite3.Connection) -> dict:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _backup_database(dest_file: Path) -> dict:
    """Copy the runtime database with SQLite's online backup API.

    Returns {"status": "ok" | "absent" | "failed", "tables": {name: rows}}. "absent" is a
    fresh install with no database yet -- nothing to lose, not a failed backup. Never
    raises. The copy is written under a temporary name and moved into place only after
    PRAGMA integrity_check passes, so a half-written or damaged copy is never left
    looking like a backup.
    """
    src_path = _database_source()
    if not src_path.is_file():
        return {"status": "absent", "tables": {}}
    tmp = dest_file.with_name(dest_file.name + ".tmp")
    src = dst = None
    try:
        tmp.unlink(missing_ok=True)
        src = sqlite3.connect(str(src_path), timeout=30.0)
        dst = sqlite3.connect(str(tmp))
        src.backup(dst)
        # The source is in WAL mode and the copy inherits that. Convert it to a single
        # self-contained file, so opening it later never creates -wal/-shm files beside
        # the backup (which would also fail on read-only media).
        dst.execute("PRAGMA journal_mode=DELETE").fetchone()
        verdict = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise sqlite3.DatabaseError(f"integrity_check: {verdict}")
        tables = _table_counts(dst)
        dst.commit()
        dst.close(); dst = None
        src.close(); src = None
        os.replace(tmp, dest_file)
        return {"status": "ok", "tables": tables}
    except Exception as e:
        log.warning("backup: database copy failed: %s", e)
        return {"status": "failed", "tables": {}}
    finally:
        for c in (dst, src):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _database_copy_intact(path: Path, tables: dict) -> bool:
    """True when the copy opens, passes integrity_check and still has every table the
    manifest says it was made with."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return all(name in have for name in tables)
    except Exception:
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _write_manifest(dest: Path, *, stamp: str, vault_ok: bool,
                    collections_ok: list, collections_failed: list,
                    database: dict | None = None) -> None:
    """Record what this backup actually contains, so retention can tell a complete
    backup from a failed one instead of trusting the directory name."""
    database = database or {"status": "absent", "tables": {}}
    payload = {
        "version": 2,
        "created_at": stamp,
        "status": ("complete" if (vault_ok and not collections_failed
                                  and database["status"] != "failed") else "partial"),
        "vault_ok": vault_ok,
        "collections_ok": sorted(collections_ok),
        "collections_failed": sorted(collections_failed),
        "database": database["status"],
        "database_tables": database["tables"],
    }
    tmp = dest / (_MANIFEST_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, dest / _MANIFEST_NAME)   # atomic: never a half-written manifest
    except Exception as e:
        log.warning("backup: could not write manifest for %s: %s", dest.name, e)


def backup_is_verified(path: Path) -> bool:
    """True only when the manifest says complete AND its artifacts are really there.

    A manifest alone is not enough -- it is checked against the files on disk, so a
    truncated vault.zip or a missing snapshot demotes the backup to unverified rather
    than letting it count as a good copy.
    """
    try:
        manifest = json.loads((path / _MANIFEST_NAME).read_text(encoding="utf-8"))
        if manifest.get("status") != "complete" or manifest.get("vault_ok") is not True:
            return False
        if manifest.get("collections_failed"):
            return False
        vault_zip = path / "vault.zip"
        if not vault_zip.is_file() or vault_zip.stat().st_size == 0:
            return False
        with zipfile.ZipFile(vault_zip) as zf:
            if zf.testzip() is not None:
                return False
        for name in manifest.get("collections_ok", []):
            snap = path / "qdrant" / f"{name}.snapshot"
            if not snap.is_file() or snap.stat().st_size == 0:
                return False
        # Manifests written before P0 have no "database" key; they stay valid as they were
        # (demoting them would leave nothing verified and stop all pruning). From version 2
        # on, a database that was backed up must still open and hold its tables.
        state = manifest.get("database")
        if state == "failed":
            return False
        if state == "ok" and not _database_copy_intact(
                path / _DB_NAME, manifest.get("database_tables") or {}):
            return False
        return True
    except (OSError, ValueError, TypeError, zipfile.BadZipFile):
        return False


def prunable_backups(dirs: list, keep: int, verified: set | None = None) -> list:
    """Pure: which dated backup dir names should be pruned, oldest first.

    Names sort lexicographically, which is chronological for YYYY-MM-DD_HHMMSS.

    **A00 fix -- a failed backup must not be able to evict a verified one.** Before the
    manifest existed, every directory counted toward `keep` regardless of whether the
    backup inside had succeeded, so a run of failures could push out the last known-good
    copy. `verified` is the set of names confirmed complete by backup_is_verified().

    Rules, in order:
      1. If nothing is verified, prune NOTHING. An unverified directory may hold the only
         surviving copy, and with no good backup to fall back on this is the worst
         possible moment to delete.
      2. Verified backups are pruned only when there are MORE than `keep` of them, oldest
         first. A verified backup is never sacrificed to make room for an unverified one.
      3. Unverified/legacy directories are pruned oldest-first, only as far as needed to
         bring the TOTAL directory count down to `keep`.

    Rule 3 is deliberately about the total, not "anything older than the newest verified
    backup". **That first version deleted 7 real legacy backups on its first live run**:
    with a single verified backup present, every manifest-less directory counted as
    superseded at once. Legacy directories are historical restore points, and the arrival
    of one good backup does not make seven older ones worthless -- they age out normally
    instead, which is also what the pre-manifest behavior did.

    `verified=None` means "caller did not classify" and is treated as nothing verified, so
    rule 1 applies rather than the old count-everything behavior.
    """
    keep = max(0, keep)
    names = sorted(dirs)
    verified = verified or set()
    good = [n for n in names if n in verified]
    if not good:
        return []
    if keep == 0:
        return names          # explicit request to retain nothing

    drop = set(good[:max(0, len(good) - keep)])          # rule 2
    survivors = [n for n in names if n not in drop]
    excess = len(survivors) - keep                        # rule 3
    if excess > 0:
        for name in survivors:                            # oldest first
            if excess == 0:
                break
            if name not in verified:
                drop.add(name)
                excess -= 1
    return sorted(drop)


def _prune_old_backups(keep: int) -> int:
    if not _BACKUP_ROOT.exists():
        return 0
    dirs = [p for p in _BACKUP_ROOT.iterdir() if p.is_dir()]
    names = [p.name for p in dirs]
    verified = {p.name for p in dirs if backup_is_verified(p)}
    if not verified:
        log.warning(
            "backup: no verified backup present -- pruning nothing (%d director%s kept)",
            len(names), "y" if len(names) == 1 else "ies",
        )
        return 0
    pruned = 0
    for name in prunable_backups(names, keep, verified):
        try:
            shutil.rmtree(_BACKUP_ROOT / name)
            pruned += 1
        except Exception as e:
            log.warning("backup: failed to prune old backup %s: %s", name, e)
    return pruned


def create_backup(keep: int = _KEEP_LAST) -> dict:
    """Zip the vault + snapshot every Qdrant collection into a fresh dated
    backups/<timestamp>/ folder, then prune old backups beyond `keep`.

    Never raises. Returns:
      {"dir": str, "vault_ok": bool, "collections_ok": [...],
       "collections_failed": [...], "database": "ok"|"absent"|"failed", "pruned": int}
    A totally empty result (nothing backed up at all) removes its own empty
    dated folder rather than leaving clutter behind.
    """
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = _BACKUP_ROOT / stamp
    dest.mkdir(parents=True, exist_ok=True)

    vault_ok = _zip_vault(dest / "vault.zip")
    collections_ok, collections_failed = _snapshot_collections(dest / "qdrant")
    database = _backup_database(dest / _DB_NAME)

    if not vault_ok and not collections_ok and database["status"] != "ok":
        try:
            shutil.rmtree(dest)
        except Exception:
            pass
        pruned = 0   # nothing was produced -- this run has not earned the right to delete
    else:
        # Manifest first, THEN prune: retention reads manifests, so the backup just made
        # must be classifiable before it can influence (or survive) pruning. Pruning used
        # to run before this outcome was known, letting a failed run evict good backups.
        _write_manifest(dest, stamp=stamp, vault_ok=vault_ok,
                        collections_ok=collections_ok, collections_failed=collections_failed,
                        database=database)
        pruned = _prune_old_backups(keep)

    result = {
        "dir": str(dest),
        "vault_ok": vault_ok,
        "collections_ok": collections_ok,
        "collections_failed": collections_failed,
        "database": database["status"],
        "pruned": pruned,
    }
    log.info(
        "backup: vault=%s, %d/%d collections backed up, database=%s, pruned %d old backup(s) -> %s",
        "ok" if vault_ok else "FAILED",
        len(collections_ok), len(collections_ok) + len(collections_failed),
        database["status"], pruned, dest,
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    result = create_backup()
    print(
        f"backup: vault={'ok' if result['vault_ok'] else 'FAILED'}, "
        f"{len(result['collections_ok'])} collection(s) ok, "
        f"{len(result['collections_failed'])} failed, "
        f"database {result['database']}, "
        f"pruned {result['pruned']} old backup(s) -> {result['dir']}"
    )
    sys.exit(0 if (result["vault_ok"] and not result["collections_failed"]
                   and result["database"] != "failed") else 1)
