"""A02: the SQLite runtime store — connection, schema, migrations.

Small, durable operational records. The roadmap is explicit about scope: "Add SQLite for
small, durable operational records. Do not introduce microservices, a generic multi-agent
framework, a new vector database, or a wholesale rewrite." Qdrant keeps retrieval,
Markdown keeps narrative memory; this holds the facts that must survive a crash and be
queryable by index rather than by scanning.

**Additive by design.** Nothing reads from here to serve a turn yet. Exchanges are written
alongside the existing `jarvis_threads` path, which stays authoritative until the SQLite
copy has proven itself against real traffic. A bug in this file should mean "an unused
table is wrong", never "a memory went missing" — so every entry point swallows its own
errors and returns a falsey value, matching the silent-degradation contract the rest of
the backend follows.

Threading: `sqlite3` connections cannot be shared across threads, and this codebase calls
into storage from the event loop, from `run_in_executor` workers, and from daemon threads.
Each thread therefore gets its own connection via `threading.local()`. WAL mode lets a
reader proceed while a writer holds the write lock, which is what makes that safe.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Project root, matching the .jarvis_*.json operational-state family that already lives
# there (.jarvis_pace.json, .jarvis_chronos.json, .self_model_cache.json).
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / ".jarvis_runtime.db"

_local = threading.local()
_init_lock = threading.Lock()
_initialised_paths: set[str] = set()
# Every connection handed out, across every thread. threading.local() alone cannot be
# closed from outside its owning thread, which left executor-thread connections holding
# the database file open -- on Windows that is a hard PermissionError when a test tries
# to delete its temporary database, and in production it means shutdown does not
# actually release the file.
_all_conns: list[sqlite3.Connection] = []

SCHEMA_VERSION = 8

# Ordered migrations. Index i upgrades user_version i -> i+1, so a deployment that is
# behind replays only what it is missing. Never edit a shipped migration in place: add a
# new one, or an existing database will silently disagree with a fresh one.
_MIGRATIONS: list[str] = [
    # 0 -> 1: sessions, canonical exchanges, ingress deduplication.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id        TEXT PRIMARY KEY,
        channel           TEXT NOT NULL,
        started_at        REAL NOT NULL,
        last_activity_at  REAL NOT NULL,
        ended_at          REAL
    );
    -- The index that removes "scan all history to find the current session".
    CREATE INDEX IF NOT EXISTS idx_sessions_channel_activity
        ON sessions (channel, last_activity_at DESC);

    CREATE TABLE IF NOT EXISTS exchanges (
        id             TEXT PRIMARY KEY,   -- the owning Thread's uuid, so the two stores
                                            -- correlate without a second identifier
        session_id     TEXT NOT NULL,
        channel        TEXT NOT NULL,
        timestamp      REAL NOT NULL,
        user_text      TEXT NOT NULL,      -- ALWAYS the full text, see module note below
        assistant_text TEXT NOT NULL,      -- ALWAYS the full text
        significance   REAL NOT NULL,
        high           INTEGER NOT NULL,
        summary        TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_exchanges_session
        ON exchanges (session_id, timestamp);
    CREATE INDEX IF NOT EXISTS idx_exchanges_channel_ts
        ON exchanges (channel, timestamp DESC);

    CREATE TABLE IF NOT EXISTS ingress (
        message_id  TEXT PRIMARY KEY,      -- Discord message id: the deduplication key
        channel     TEXT NOT NULL,
        received_at REAL NOT NULL,
        session_id  TEXT
    );
    """,
    # 1 -> 2: Discord message identity and user corrections. These are source
    # records, not copies of Qdrant or the vault.
    """
    CREATE TABLE IF NOT EXISTS discord_outgoing (
        message_id TEXT PRIMARY KEY,
        owner_id   TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        kind       TEXT NOT NULL,
        body       TEXT NOT NULL,
        sent_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_discord_outgoing_owner_time
        ON discord_outgoing (owner_id, sent_at DESC);

    CREATE TABLE IF NOT EXISTS fact_corrections (
        id              TEXT PRIMARY KEY,
        user_id         TEXT NOT NULL,
        claim           TEXT NOT NULL,
        correction      TEXT NOT NULL,
        terms           TEXT NOT NULL DEFAULT '[]',
        created_at      REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_fact_corrections_user_time
        ON fact_corrections (user_id, created_at DESC);
    """,
    # 2 -> 3: goals (docs/archive/GOALS-ROADMAP.md M1, G1a). Canonical goal records: the mandate and
    # its criteria, numbered plans, milestones, bounded sessions, events, notes, captured
    # evidence, versioned files, budget charges, and effects (first writer in G2).
    """
    CREATE TABLE IF NOT EXISTS goals (
      goal_id TEXT PRIMARY KEY, creation_key TEXT NOT NULL UNIQUE,
      title TEXT NOT NULL, authorized_words TEXT NOT NULL, conversation_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN
        ('proposed','active','waiting','paused','done','partial','cancelled','failed')),
      mandate_rev INTEGER NOT NULL DEFAULT 1, plan_rev INTEGER NOT NULL DEFAULT 0,
      deadline_at REAL, budget_microusd INTEGER NOT NULL CHECK (budget_microusd >= 0),
      folder TEXT NOT NULL UNIQUE, active_session_id TEXT, card_message_id TEXT,
      created_at REAL NOT NULL, local_date TEXT NOT NULL, updated_at REAL NOT NULL,
      ended_at REAL, outcome_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS goal_criteria (
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), criterion_id TEXT NOT NULL,
      text TEXT NOT NULL, actor TEXT NOT NULL, source_event_id TEXT,
      mandate_rev INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      acceptance_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
      PRIMARY KEY (goal_id, criterion_id, mandate_rev)
    );
    CREATE TABLE IF NOT EXISTS goal_plans (
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), revision INTEGER NOT NULL,
      actor TEXT NOT NULL, reason TEXT NOT NULL, plan_json TEXT NOT NULL,
      created_at REAL NOT NULL, local_date TEXT NOT NULL,
      PRIMARY KEY (goal_id, revision)
    );
    CREATE TABLE IF NOT EXISTS goal_milestones (
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), milestone_id TEXT NOT NULL,
      plan_rev INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
      title TEXT NOT NULL, expected TEXT NOT NULL, checks_json TEXT NOT NULL,
      criteria_json TEXT NOT NULL, dependencies_json TEXT NOT NULL DEFAULT '[]',
      state TEXT NOT NULL, active_session_id TEXT, no_progress INTEGER NOT NULL DEFAULT 0,
      accepted_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL,
      PRIMARY KEY (goal_id, milestone_id)
    );
    CREATE TABLE IF NOT EXISTS goal_sessions (
      session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), milestone_id TEXT NOT NULL,
      generation INTEGER NOT NULL, plan_rev INTEGER NOT NULL, state TEXT NOT NULL,
      started_at REAL NOT NULL, local_date TEXT NOT NULL, ended_at REAL,
      ending_json TEXT, checkpoint_json TEXT NOT NULL DEFAULT '{}',
      progress_fingerprint TEXT, output_tokens INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS goal_one_working_session ON goal_sessions(goal_id)
      WHERE state IN ('claimed','running');
    CREATE TABLE IF NOT EXISTS goal_events (
      event_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(goal_id),
      event_key TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
      payload_json TEXT NOT NULL, source_message_id TEXT, conversation_id TEXT,
      created_at REAL NOT NULL, local_date TEXT NOT NULL, applied_at REAL,
      UNIQUE (goal_id, event_key)
    );
    CREATE INDEX IF NOT EXISTS goal_events_pending ON goal_events(goal_id, applied_at, created_at);
    CREATE TABLE IF NOT EXISTS goal_notes (
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), entry_id TEXT NOT NULL,
      revision INTEGER NOT NULL, section TEXT NOT NULL, body TEXT NOT NULL,
      source_refs_json TEXT NOT NULL DEFAULT '[]', removed INTEGER NOT NULL DEFAULT 0,
      op_key TEXT NOT NULL, created_at REAL NOT NULL, local_date TEXT NOT NULL,
      PRIMARY KEY (goal_id, entry_id, revision), UNIQUE (goal_id, op_key)
    );
    CREATE TABLE IF NOT EXISTS goal_evidence (
      evidence_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(goal_id),
      session_id TEXT, run_id TEXT, tool TEXT NOT NULL, arguments_json TEXT NOT NULL,
      body TEXT NOT NULL, body_hash TEXT NOT NULL, metadata_json TEXT NOT NULL,
      captured_at REAL NOT NULL, local_date TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS goal_files (
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), name TEXT NOT NULL,
      revision INTEGER NOT NULL, relative_path TEXT NOT NULL, mime_type TEXT NOT NULL,
      body TEXT, byte_length INTEGER NOT NULL, content_hash TEXT NOT NULL,
      source_refs_json TEXT NOT NULL DEFAULT '[]', op_key TEXT NOT NULL,
      mirrored_hash TEXT, created_at REAL NOT NULL, local_date TEXT NOT NULL,
      PRIMARY KEY (goal_id, name, revision), UNIQUE (goal_id, op_key)
    );
    CREATE TABLE IF NOT EXISTS goal_charges (
      charge_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(goal_id),
      session_id TEXT, purpose TEXT NOT NULL,
      reserved_microusd INTEGER NOT NULL CHECK (reserved_microusd >= 0),
      settled_microusd INTEGER CHECK (settled_microusd >= 0),
      state TEXT NOT NULL,
      created_at REAL NOT NULL, local_date TEXT NOT NULL, settled_at REAL
    );
    CREATE INDEX IF NOT EXISTS goal_charges_day ON goal_charges(local_date, state);
    CREATE INDEX IF NOT EXISTS goal_charges_goal ON goal_charges(goal_id, state);
    CREATE TABLE IF NOT EXISTS goal_effects (
      effect_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(goal_id),
      milestone_id TEXT, generation INTEGER,
      kind TEXT NOT NULL,
      logical_key TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL,
      target_id TEXT, result_json TEXT, quota_meter TEXT, quota_units INTEGER DEFAULT 0,
      created_at REAL NOT NULL, local_date TEXT NOT NULL, updated_at REAL NOT NULL,
      UNIQUE (goal_id, logical_key)
    );
    """,
    # 3 -> 4: goal waits, links to other subsystems' rows, and goal deliveries
    # (docs/archive/GOALS-ROADMAP.md M2, G2). Delivery state stays in delivery_intents/delivery_parts.
    """
    CREATE TABLE IF NOT EXISTS goal_waits (
      wait_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(goal_id),
      milestone_id TEXT NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL,
      correlation_key TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL,
      due_at REAL, expires_at REAL, created_at REAL NOT NULL, local_date TEXT NOT NULL,
      resolved_at REAL, outcome_json TEXT,
      UNIQUE (goal_id, correlation_key)
    );
    CREATE INDEX IF NOT EXISTS goal_waits_due ON goal_waits(state, due_at);
    CREATE TABLE IF NOT EXISTS goal_bindings (
      kind TEXT NOT NULL, target_id TEXT NOT NULL,
      goal_id TEXT NOT NULL REFERENCES goals(goal_id),
      milestone_id TEXT NOT NULL, generation INTEGER NOT NULL,
      effect_id TEXT NOT NULL REFERENCES goal_effects(effect_id), wait_id TEXT,
      active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
      PRIMARY KEY (kind, target_id), UNIQUE (effect_id, kind)
    );
    CREATE TABLE IF NOT EXISTS goal_deliveries (
      goal_id TEXT NOT NULL REFERENCES goals(goal_id), event_id TEXT NOT NULL,
      purpose TEXT NOT NULL, delivery_id TEXT NOT NULL UNIQUE,
      item_key TEXT, item_version TEXT, message_id TEXT, created_at REAL NOT NULL,
      PRIMARY KEY (goal_id, event_id, purpose)
    );
    """,
    # 4 -> 5 (M3, docs/archive/GOALS-ROADMAP.md G3): durable conversation ingress. A message that
    # arrives while JARVIS is busy is inserted here BEFORE it is acknowledged; it either
    # joins the running turn of its own conversation (target_run_id) or becomes its own
    # turn when idle, with its own reply. Plus the shadow promise check's record (B1).
    """
    CREATE TABLE IF NOT EXISTS conversation_inbox (
      message_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
      source_message_id TEXT, target_run_id TEXT, target_goal_id TEXT,
      reply_to_message_id TEXT, text TEXT NOT NULL, context_json TEXT NOT NULL,
      state TEXT NOT NULL, received_at REAL NOT NULL, local_date TEXT NOT NULL,
      claimed_at REAL, processed_at REAL
    );
    CREATE INDEX IF NOT EXISTS conversation_inbox_pending
      ON conversation_inbox(conversation_id, state, received_at);
    CREATE TABLE IF NOT EXISTS promise_checks (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, channel TEXT,
      kind TEXT NOT NULL, verdict TEXT NOT NULL, backing TEXT, sentence TEXT NOT NULL,
      checked_at REAL NOT NULL, local_date TEXT NOT NULL
    );
    """,
    # 5 -> 6 (lab C3, SCIENCE-LAB-WORK-PLAN.md): research threads. A thread is a goal that
    # stays open -- sessions on its own cadence while the lab is open, a monthly budget --
    # so it reuses every goal table; `kind` tells the two apart and existing goal code asks
    # for kind 'goal' only. thread_json holds the thread's subject, focus and cadence.
    """
    ALTER TABLE goals ADD COLUMN kind TEXT NOT NULL DEFAULT 'goal'
      CHECK (kind IN ('goal', 'thread'));
    ALTER TABLE goals ADD COLUMN thread_json TEXT NOT NULL DEFAULT '{}';
    CREATE INDEX IF NOT EXISTS goals_kind_state ON goals(kind, state);
    """,
    # 6 -> 7 (lab C4): Python jobs for the PC's sandbox. The server queues the exact
    # request (code + the data it reads); the PC claims it while the lab is open, under a
    # lease, and posts back what it produced. op_key makes a retried call the same job.
    """
    CREATE TABLE IF NOT EXISTS lab_jobs (
      job_id TEXT PRIMARY KEY, op_key TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL,
      origin TEXT NOT NULL, title TEXT NOT NULL, request_json TEXT NOT NULL,
      request_sha TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
      lease_until REAL, result_json TEXT, error TEXT,
      created_at REAL NOT NULL, local_date TEXT NOT NULL, updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS lab_jobs_state ON lab_jobs(state, created_at);
    CREATE INDEX IF NOT EXISTS lab_jobs_thread ON lab_jobs(thread_id, created_at);
    """,
    # 7 -> 8 (24 October 2026): the lab was removed. Its research threads (goals of kind
    # 'thread') go with every row that belongs to them, the goal row last so a rerun after
    # a crash still finds them, and the Python job table is dropped. The two columns
    # migration 6 added stay: every row is now kind 'goal', and dropping a column would mean
    # rebuilding the goals table. Messages in conversation_inbox are his history and stay.
    """
    DELETE FROM goal_criteria WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_plans WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_milestones WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_sessions WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_events WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_notes WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_evidence WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_files WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_charges WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_effects WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_waits WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_bindings WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goal_deliveries WHERE goal_id IN (SELECT goal_id FROM goals WHERE kind='thread');
    DELETE FROM goals WHERE kind='thread';
    DROP INDEX IF EXISTS lab_jobs_state;
    DROP INDEX IF EXISTS lab_jobs_thread;
    DROP TABLE IF EXISTS lab_jobs;
    """,
]


def db_path() -> Path:
    """Where the runtime database lives. `JARVIS_DB_PATH` overrides, and tests point it
    at a tmp_path so they never touch the real file."""
    override = os.getenv("JARVIS_DB_PATH", "")
    return Path(override) if override else _DEFAULT_PATH


def _apply_migrations(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(_MIGRATIONS):
        return
    for index in range(version, len(_MIGRATIONS)):
        conn.executescript(_MIGRATIONS[index])
        # PRAGMA does not accept bound parameters; index+1 is an int we control.
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()
    log.info("storage: schema migrated %d -> %d", version, len(_MIGRATIONS))


def connect() -> sqlite3.Connection | None:
    """This thread's connection, migrated and ready. None when SQLite is unusable.

    Returning None rather than raising keeps a storage failure from taking down a turn:
    callers treat it exactly like Qdrant being unreachable.
    """
    path = str(db_path())
    existing = getattr(_local, "conns", None)
    if existing is None:
        existing = _local.conns = {}
    conn = existing.get(path)
    if conn is not None:
        return conn
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL: a reader never blocks on the writer, which is what makes one connection
        # per thread safe here. FULL sync since G2 (2026-10-06, docs/archive/GOALS-ROADMAP.md N12): NORMAL
        # survives a process crash but a host power loss could lose the last commits -- an
        # approval or goal effect recorded just before a send. Measured on the GCP server
        # (scripts/measure_sqlite_commit.py, 2 x 500 commits): FULL p50 2.03 ms, p95 2.41 ms,
        # max 5.8 ms; NORMAL p95 0.08 ms but max 52 ms. Under the roadmap's ~5 ms p95 bar.
        conn.execute("PRAGMA journal_mode=WAL")
        # JARVIS_DB_SYNCHRONOUS: the test suite runs NORMAL (backend/conftest.py) -- it makes
        # thousands of commits and does not test power-loss durability.
        sync = "NORMAL" if os.getenv("JARVIS_DB_SYNCHRONOUS", "").upper() == "NORMAL" else "FULL"
        conn.execute(f"PRAGMA synchronous={sync}")
        conn.execute("PRAGMA foreign_keys=ON")
        with _init_lock:
            if path not in _initialised_paths:
                _apply_migrations(conn)
                _initialised_paths.add(path)
        existing[path] = conn
        with _init_lock:
            _all_conns.append(conn)
        return conn
    except Exception as exc:
        log.warning("storage: could not open %s (%s) -- durable records disabled", path, exc)
        return None


def close_all() -> None:
    """Close EVERY connection this process opened, on any thread.

    sqlite3 objects may be closed from a thread other than the one that created them
    (only *using* them cross-thread is unsafe), so this is legitimate -- and necessary,
    since connections created inside run_in_executor workers would otherwise keep the
    file open for the life of the process.
    """
    with _init_lock:
        conns = list(_all_conns)
        _all_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass
    _local.conns = {}


def _reset_for_tests() -> None:
    close_all()
    with _init_lock:
        _initialised_paths.clear()


def healthy() -> bool:
    """Cheap liveness probe for status reporting."""
    conn = connect()
    if conn is None:
        return False
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False
