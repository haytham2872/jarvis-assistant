"""G1a: goal storage (M1) and the goal budget's charges.

Every test runs against its own temporary database (backend/conftest.py).
"""
import sqlite3

import pytest

from backend.brain import goal_budget as B
from backend.storage import db, lifelog
from backend.storage import goals as G


def _goal(state=G.ACTIVE, key="k1", budget=1_000_000, criteria=("a sourced table",)):
    return G.create(title="Compare ETFs", words="compare the three ETFs, keep at it",
                    conversation_id="discord:dm", criteria=list(criteria), state=state,
                    budget_microusd=budget, deadline_at=None, creation_key=key)


def _plan(goal_id, ids=("M1", "M2")):
    ms = [{"id": m, "title": f"step {m}", "expected": f"{m}.md", "checks": [],
           "criteria": ["C1"], "depends": ([ids[i - 1]] if i else [])}
          for i, m in enumerate(ids)]
    return G.save_plan(goal_id, ms, actor="jarvis", reason="first plan")


# --- migration -------------------------------------------------------------------------

def test_a_fresh_database_is_at_v3_with_the_goal_tables():
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 8
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"goals", "goal_criteria", "goal_plans", "goal_milestones", "goal_sessions",
            "goal_events", "goal_notes", "goal_evidence", "goal_files", "goal_charges",
            "goal_effects"} <= names


def test_a_v2_database_with_legacy_rows_upgrades_and_keeps_them(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    for script in db._MIGRATIONS[:2]:
        raw.executescript(script)
    raw.execute("PRAGMA user_version = 2")
    raw.execute("INSERT INTO sessions VALUES ('s1','discord',1.0,2.0,NULL)")
    raw.commit()
    raw.close()
    monkeypatch.setenv("JARVIS_DB_PATH", str(path))
    db._reset_for_tests()
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM goals").fetchone()[0] == 0
    db._reset_for_tests()
    conn = db.connect()                                  # a second startup changes nothing
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# --- goals ------------------------------------------------------------------------------

def test_create_is_idempotent_on_its_key():
    a, b = _goal(), _goal()
    assert a["goal_id"] == b["goal_id"]
    assert [c["criterion_id"] for c in a["criteria"]] == ["C1"]
    assert len(G.list_goals()) == 1


def test_one_working_session_per_goal():
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    m = G.ready_milestone(g["goal_id"])
    assert G.claim_session(g["goal_id"], "M1", m["generation"], "r1")
    assert G.claim_session(g["goal_id"], "M1", m["generation"], "r2") is None


def test_a_proposed_goal_cannot_start_a_session():
    g = _goal(state=G.PROPOSED)
    _plan(g["goal_id"], ("M1",))
    assert G.claim_session(g["goal_id"], "M1", 1, "r1") is None


def test_a_stale_session_writes_nothing_after_cancel():
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    sid = G.claim_session(g["goal_id"], "M1", 1, "r1")
    assert G.transition(g["goal_id"], G.CANCELLED, expect=(G.ACTIVE,))
    r = G.save_note(g["goal_id"], "M1", 1, op="add", op_key="M1:1:a", section="Findings",
                    text="late finding")
    assert "refused" in r and G.notes(g["goal_id"]) == []
    f = G.save_file(g["goal_id"], "M1", 1, name="result.md", content="x",
                    expected_revision=None, op_key="M1:1:f")
    assert "refused" in f and G.files(g["goal_id"]) == []
    assert G.save_evidence(g["goal_id"], "M1", 1, session_id=sid, run_id=None, tool="web_browse",
                           args={}, body="page", metadata={}) is None
    assert not G.complete_milestone(g["goal_id"], "M1", 1, sid, {})


def test_a_replan_makes_an_old_generation_stale():
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    G.claim_session(g["goal_id"], "M1", 1, "r1")
    ms = [{"id": "M1", "title": "changed", "expected": "x.md", "checks": [], "criteria": ["C1"],
           "depends": []}]
    G.save_plan(g["goal_id"], ms, actor="jarvis", reason="replan")
    assert not G.guard_ok(g["goal_id"], "M1", 1)
    assert G.guard_ok(g["goal_id"], "M1", 2)


def test_notes_replay_by_op_key_and_refuse_a_changed_payload():
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    a = G.save_note(g["goal_id"], "M1", 1, op="add", op_key="k", section="Findings", text="TER 0.07%")
    b = G.save_note(g["goal_id"], "M1", 1, op="add", op_key="k", section="Findings", text="TER 0.07%")
    assert b.get("replayed") and b["entry_id"] == a["entry_id"]
    c = G.save_note(g["goal_id"], "M1", 1, op="add", op_key="k", section="Findings", text="other")
    assert "refused" in c
    assert len(G.notes(g["goal_id"])) == 1


def test_a_note_cannot_cite_evidence_of_another_goal():
    g1, g2 = _goal(key="a"), _goal(key="b")
    for g in (g1, g2):
        _plan(g["goal_id"], ("M1",))
    ev = G.save_evidence(g2["goal_id"], "M1", 1, session_id="s", run_id=None, tool="web_browse",
                         args={}, body="page", metadata={})
    r = G.save_note(g1["goal_id"], "M1", 1, op="add", op_key="k", section="Findings", text="x",
                    evidence_ids=[ev])
    assert "refused" in r


def test_files_are_versioned_and_a_same_content_write_is_a_no_op():
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    gid = g["goal_id"]
    assert G.save_file(gid, "M1", 1, name="result.md", content="v1", expected_revision=None,
                       op_key="w1") == {"revision": 1}
    assert "refused" in G.save_file(gid, "M1", 1, name="result.md", content="v2",
                                    expected_revision=None, op_key="w2")
    assert G.save_file(gid, "M1", 1, name="result.md", content="v1", expected_revision=1,
                       op_key="w3").get("unchanged")
    assert G.save_file(gid, "M1", 1, name="result.md", content="v2", expected_revision=1,
                       op_key="w4") == {"revision": 2}
    assert G.save_file(gid, "M1", 1, name="result.md", content="v2", expected_revision=1,
                       op_key="w4").get("replayed")
    assert G.read_file(gid, "result.md")["body"] == "v2"
    assert G.read_file(gid, "result.md", 1)["body"] == "v1"
    assert "refused" in G.save_file(gid, "M1", 1, name="../x.md", content="x",
                                    expected_revision=None, op_key="w5")


def test_recovery_interrupts_sessions_releases_milestones_and_holds_charges():
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    sid = G.claim_session(g["goal_id"], "M1", 1, "r1")
    charge, why = B.reserve(g["goal_id"], "session", 100_000, session_id=sid)
    assert charge and not why
    [s] = G.recover()
    assert s["session_id"] == sid
    assert G.session(sid)["state"] == G.INTERRUPTED
    assert G.ready_milestone(g["goal_id"])["milestone_id"] == "M1"
    [c] = G.charges(g["goal_id"])
    assert (c["state"], c["settled_microusd"]) == ("held", 100_000)
    assert G.recover() == []


def test_milestones_wait_for_their_dependencies():
    g = _goal()
    _plan(g["goal_id"], ("M1", "M2"))
    assert G.ready_milestone(g["goal_id"])["milestone_id"] == "M1"
    sid = G.claim_session(g["goal_id"], "M1", 1, "r1")
    assert G.complete_milestone(g["goal_id"], "M1", 1, sid, {"files": []})
    G.end_session(sid, G.COMPLETE, ending={}, fingerprint=None, release=False)
    assert G.ready_milestone(g["goal_id"])["milestone_id"] == "M2"


# --- budget -----------------------------------------------------------------------------

def test_a_reservation_past_the_goal_cap_is_refused():
    g = _goal(budget=200_000)
    assert B.reserve(g["goal_id"], "session", 150_000)[0]
    charge, why = B.reserve(g["goal_id"], "session", 60_000)
    assert charge is None and "budget" in why


def test_the_daily_cap_counts_every_goal_and_earlier_open_reservations(monkeypatch):
    monkeypatch.setenv("GOALS_DAILY_BUDGET_USD", "0.30")
    g1, g2 = _goal(key="a"), _goal(key="b")
    c, _ = B.reserve(g1["goal_id"], "session", 150_000)
    conn = db.connect()
    with conn:                                       # an open reservation from yesterday
        conn.execute("UPDATE goal_charges SET local_date='2000-01-01' WHERE charge_id=?", (c,))
    assert B.reserve(g2["goal_id"], "session", 100_000)[0]
    charge, why = B.reserve(g2["goal_id"], "session", 100_000)
    assert charge is None and "today" in why


def test_settled_charges_count_and_a_null_cost_is_priced_from_the_table():
    usage = [{"cost": 0.001, "input_tokens": 9, "output_tokens": 9},
             {"cost": None, "input_tokens": 10_000, "output_tokens": 1_000}]
    assert B.cost_of(usage) == 1000 + int(round((10_000 * 0.30 + 1_000 * 1.20)))
    g = _goal()
    c, _ = B.reserve(g["goal_id"], "session", 100_000)
    assert B.settle(c, 4_200)
    assert B.goal_spent(g["goal_id"]) == 4_200


def test_a_restart_does_not_reset_the_cap():
    g = _goal(budget=100_000)
    c, _ = B.reserve(g["goal_id"], "session", 90_000)
    B.hold(c)
    db._reset_for_tests()                            # a new process, same database file
    assert B.goal_spent(g["goal_id"]) == 90_000
    assert B.reserve(g["goal_id"], "session", 20_000)[0] is None


def test_thresholds_are_reported_at_80_and_100_percent():
    g = _goal(budget=100_000)
    c, _ = B.reserve(g["goal_id"], "session", 85_000)
    assert B.thresholds_crossed(g["goal_id"]) == ["warn"]
    B.settle(c, 100_000)
    assert B.thresholds_crossed(g["goal_id"]) == ["warn", "cap"]


def test_one_working_session_even_across_two_ready_milestones():
    """The partial unique index, not the milestone's own state, is what stops a second
    session on another milestone of the same goal."""
    g = _goal()
    ms = [{"id": m, "title": m, "expected": f"{m}.md", "checks": [], "criteria": ["C1"],
           "depends": []} for m in ("M1", "M2")]
    G.save_plan(g["goal_id"], ms, actor="jarvis", reason="independent")
    assert G.claim_session(g["goal_id"], "M1", 1, "r1")
    assert G.claim_session(g["goal_id"], "M2", 1, "r2") is None


def test_nothing_is_written_to_a_goal_that_has_ended_even_on_a_done_milestone():
    """Ending a goal bumps the generation of unfinished milestones only: a done milestone
    keeps its generation, so the goal's own state is what refuses a late write there."""
    g = _goal()
    _plan(g["goal_id"], ("M1",))
    sid = G.claim_session(g["goal_id"], "M1", 1, "r1")
    assert G.complete_milestone(g["goal_id"], "M1", 1, sid, {})
    assert G.transition(g["goal_id"], G.DONE, expect=(G.ACTIVE,))
    r = G.save_note(g["goal_id"], "M1", 1, op="add", op_key="late", section="Findings", text="x")
    assert "refused" in r and G.notes(g["goal_id"]) == []


def test_production_syncs_full_and_only_an_explicit_setting_lowers_it(monkeypatch, tmp_path):
    """N12 (G2): the server measured FULL at p95 2.41 ms, under the roadmap's 5 ms bar."""
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "full.db"))
    monkeypatch.delenv("JARVIS_DB_SYNCHRONOUS", raising=False)
    db._reset_for_tests()
    assert db.connect().execute("PRAGMA synchronous").fetchone()[0] == 2      # FULL
    monkeypatch.setenv("JARVIS_DB_SYNCHRONOUS", "NORMAL")
    db._reset_for_tests()
    assert db.connect().execute("PRAGMA synchronous").fetchone()[0] == 1      # NORMAL
