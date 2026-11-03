"""Commit latency of the runtime database under synchronous=NORMAL and FULL (G0, N12).

    .venv/bin/python scripts/measure_sqlite_commit.py [--commits 500] [--root /opt/jarvis]

docs/archive/GOALS-ROADMAP.md: if the p95 commit under FULL is under about 5 ms, the whole database
moves to FULL before critical goal actions are enabled (G2); otherwise the
process-crash-only guarantee is documented and Sam decides.

Never touches the live database: it is copied with SQLite's online backup API (read-only
source connection) into a temporary directory beside it -- the same filesystem, so the
fsync cost is the real one -- measured there, then deleted. Each commit writes one small
row shaped like a goal event (about 300 bytes of JSON), in WAL mode as the runtime uses.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def db_path(root: Path = ROOT) -> Path:
    override = os.getenv("JARVIS_DB_PATH", "")
    return Path(override) if override else root / ".jarvis_runtime.db"


def pct(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def measure(path: Path, mode: str, commits: int) -> list[float]:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={mode}")
    conn.execute("CREATE TABLE IF NOT EXISTS g0_commit_probe (id TEXT PRIMARY KEY, "
                 "payload TEXT NOT NULL, created_at REAL NOT NULL)")
    payload = json.dumps({"kind": "answer", "text": "x" * 220, "goal": "g-probe"})
    out = []
    for _ in range(commits):
        t0 = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO g0_commit_probe VALUES (?, ?, ?)",
                     (str(uuid.uuid4()), payload, time.time()))
        conn.execute("COMMIT")
        out.append((time.perf_counter() - t0) * 1000)
    conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", type=int, default=500)
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    src_path = db_path(Path(a.root).resolve() if a.root else ROOT)
    if not src_path.exists():
        raise SystemExit(f"No runtime database at {src_path}")
    tmp = Path(tempfile.mkdtemp(prefix=".g0-commit-probe-", dir=src_path.parent))
    try:
        copy = tmp / "copy.db"
        src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        dst.close()
        size = copy.stat().st_size
        print(f"Copied {src_path.name} ({size / 1e6:.1f} MB) to {tmp} (same filesystem); "
              f"live database untouched")
        for mode in ("NORMAL", "FULL", "NORMAL", "FULL"):     # interleaved, twice
            ms = measure(copy, mode, a.commits)
            print(f"  synchronous={mode:6} commits={len(ms)} p50={statistics.median(ms):.3f}ms "
                  f"p95={pct(ms, .95):.3f}ms p99={pct(ms, .99):.3f}ms max={max(ms):.3f}ms")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"Removed {tmp}")


if __name__ == "__main__":
    main()
