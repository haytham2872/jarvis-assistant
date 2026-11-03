"""The lab was removed on 24 October 2026. Migration 8 deletes its research threads (goals of
kind 'thread') with every row that belongs to them and drops the Python job table, and
leaves ordinary goals exactly as they were. On the server: 2 threads, about 90 rows across
the goal tables, 3 finished jobs."""
import sqlite3

import pytest

from backend.storage import db

GOAL_TABLES = ("goal_criteria", "goal_plans", "goal_milestones", "goal_sessions",
               "goal_events", "goal_notes", "goal_evidence", "goal_files", "goal_charges",
               "goal_effects", "goal_waits", "goal_bindings", "goal_deliveries")


def _at_version_7(path) -> sqlite3.Connection:
    """A database as the server has it before this change: schema 7, with one goal and one
    lab thread, each with a row in every goal table, and a finished lab job."""
    conn = sqlite3.connect(path)
    for script in db._MIGRATIONS[:7]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 7")
    for gid, kind in (("gkeep", "goal"), ("gthread", "thread")):
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(goals)")}
        conn.execute(
            "INSERT INTO goals (goal_id, creation_key, title, authorized_words, conversation_id,"
            " state, budget_microusd, folder, created_at, local_date, updated_at, kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, gid, gid, "w", "c", "active", 1, gid, 1.0, "2026-10-24", 1.0, kind))
        assert "kind" in cols
        for t in GOAL_TABLES:
            info = list(conn.execute(f"PRAGMA table_info({t})"))
            values = {}
            for _, name, typ, notnull, default, _pk in info:
                if name == "goal_id":
                    values[name] = gid
                elif notnull and default is None:
                    values[name] = f"{t}-{gid}-{name}" if "TEXT" in typ.upper() or not typ else 1
            conn.execute(f"INSERT INTO {t} ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
                         list(values.values()))
    conn.execute("INSERT INTO lab_jobs (job_id, op_key, thread_id, origin, title, request_json,"
                 " request_sha, state, created_at, local_date, updated_at)"
                 " VALUES ('j1','k1','gthread','session','t','{}','s','done',1,'2026-10-24',1)")
    conn.commit()
    return conn


def _count(conn, table, gid):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE goal_id=?", (gid,)).fetchone()[0]


def test_migration_removes_threads_and_keeps_goals(tmp_path):
    conn = _at_version_7(tmp_path / "old.db")
    db._apply_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db._MIGRATIONS) == 8
    for t in ("goals",) + GOAL_TABLES:
        assert _count(conn, t, "gthread") == 0, t
        assert _count(conn, t, "gkeep") == 1, t
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'lab_jobs%'").fetchall() == []


def test_migration_can_run_again(tmp_path):
    """executescript commits before user_version is bumped, so a crash between the two
    reruns it; nothing may fail or change the second time."""
    conn = _at_version_7(tmp_path / "old.db")
    conn.executescript(db._MIGRATIONS[7])
    conn.executescript(db._MIGRATIONS[7])
    assert _count(conn, "goals", "gkeep") == 1 and _count(conn, "goals", "gthread") == 0


def test_a_fresh_database_has_no_lab_table(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "fresh.db"))
    db.close_all()
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='lab_jobs'").fetchone() is None
    db.close_all()


def test_the_lab_is_gone_and_goals_remain():
    from backend.brain import engine
    from backend.tools.registry import TOOLS
    names = {t["name"] for t in TOOLS}
    assert not names & {"thread", "lab_show", "lab_python"}
    assert "goal" in names and "goal" in engine.TOOL_DISPATCH
    assert not set(engine.TOOL_DISPATCH) & {"thread", "lab_show", "lab_python"}
    for module in ("backend.lab_link", "backend.brain.lab_threads", "backend.brain.lab_context",
                   "backend.brain.lab_python", "backend.storage.lab_jobs"):
        with pytest.raises(ImportError):
            __import__(module)


def test_the_app_has_no_lab_routes():
    import backend.main as M
    assert not [r.path for r in M.app.routes if getattr(r, "path", "").startswith("/lab")]
    assert [r.path for r in M.app.routes if getattr(r, "path", "").startswith("/mobile")]
