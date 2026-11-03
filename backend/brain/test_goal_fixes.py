"""Goal g99640a5696 (18 October 2026): a finished goal that ended "partial".

Each test replays one shape from that night's records on the server: file references sent as
plain names (35 refusals), a quote joined with an ellipsis (19), a milestone whose output was
a note in self/ (the goal's end), note rewording that kept 21 sessions running on one
milestone, a milestone accepted without its file, and the Discord card cut mid-sentence.
"""
import json

import pytest

from backend.brain import goal_runner as R
from backend.brain import goals as GT
from backend.brain.test_goal_runner import PLAN, _calls, _cycles, _start, _text, run, world  # noqa: F401
from backend.storage import goals as S

PAGE = ("Nvidia now trades at under 17 times forward earnings, near its lowest valuation in more "
        "than a decade. The multiple has been cut in half compared with 2025, when it sat above 25.")

VERDICT_PLAN = [{"id": "M1", "title": "Draft verdicts", "expected": "A goal file verdicts.md with a verdict per position",
                 "checks": [], "criteria": ["C1"], "depends": []},
                {"id": "M2", "title": "Write the result", "expected": "result.md with every verdict",
                 "checks": [], "criteria": ["C1"], "depends": ["M1"]}]


def _session(g):
    m = S.ready_milestone(g["goal_id"])
    sid = S.claim_session(g["goal_id"], m["milestone_id"], m["generation"], "r-" + m["milestone_id"])
    return R.Session(S.get(g["goal_id"]), m, sid, "r-" + m["milestone_id"])


def _with_file(world, body=None):
    g = _start()
    S.save_plan(g["goal_id"], VERDICT_PLAN, actor="jarvis", reason="first plan")
    ev = S.save_evidence(g["goal_id"], "M1", 1, session_id="s0", run_id=None, tool="web_browse",
                         args={}, body=PAGE, metadata={})
    s = _session(g)
    text = body or f'NVDA: RENEWED. "under 17 times forward earnings" [{ev}].'
    assert run(s.execute("goal_file", {"op": "write", "name": "verdicts.md", "content": text,
                                       "expected_revision": None, "op_key": "v1"})).startswith("Wrote")
    return g, s, ev


# --- file references ------------------------------------------------------------------

@pytest.mark.parametrize("refs", [
    ["verdicts.md"],                                   # a plain name
    ["verdicts.md r1"],                                # name and revision in one string
    '[{"name": "verdicts.md", "revision": 1}]',        # the array sent as a JSON string
    [{"name": "verdicts.md", "revision": "1"}],        # the revision as text
    [{"file": "verdicts.md"}],                         # another key for the name
])
def test_a_file_reference_in_another_shape_is_read_not_refused(world, refs):
    g, s, _ = _with_file(world)
    out = run(s.execute("goal_finish", {"ending": "milestone_done", "summary": "verdicts written",
                                        "artifact_refs": refs}))
    assert out.startswith("Session ending recorded"), out
    assert s.ending["artifact_refs"] == [{"name": "verdicts.md", "revision": 1}]


def test_an_unreadable_reference_says_the_shape_to_use(world):
    g, s, _ = _with_file(world)
    out = run(s.execute("goal_finish", {"ending": "milestone_done", "summary": "x",
                                        "artifact_refs": [42]}))
    assert out.startswith("Error") and '{"name": "verdicts.md", "revision": 1}' in out


def test_a_reference_to_a_missing_file_is_still_refused(world):
    g, s, _ = _with_file(world)
    out = run(s.execute("goal_finish", {"ending": "milestone_done", "summary": "x",
                                        "artifact_refs": ["nonexistent.md"]}))
    assert out.startswith("Error") and "nonexistent.md" in out
    assert "files: verdicts.md r1" in out        # names what does exist


# --- quotes joined with an ellipsis ---------------------------------------------------

def test_a_quote_joining_two_real_passages_with_an_ellipsis_is_accepted(world):
    g, _, ev = _with_file(world)
    for dots in ("…", "...", " … "):
        text = f'"under 17 times forward earnings{dots}cut in half compared with 2025" [{ev}]'
        assert R.check_quotes(g["goal_id"], text) == "", dots


def test_every_part_of_an_ellipsis_quote_must_be_real(world):
    g, _, ev = _with_file(world)
    text = f'"under 17 times forward earnings … tripled compared with 2025" [{ev}]'
    assert "not in" in R.check_quotes(g["goal_id"], text)


# --- a milestone's own file ------------------------------------------------------------

def test_a_milestone_whose_output_is_a_file_is_not_done_without_it(world):
    g = _start()
    S.save_plan(g["goal_id"], VERDICT_PLAN, actor="jarvis", reason="first plan")
    ev = S.save_evidence(g["goal_id"], "M1", 1, session_id="s0", run_id=None, tool="web_browse",
                         args={}, body=PAGE, metadata={})
    s = _session(g)
    out = run(s.execute("goal_finish", {"ending": "milestone_done", "summary": "verdicts drafted",
                                        "evidence_refs": [ev]}))
    assert out.startswith("Error") and "verdicts.md" in out


def test_naming_only_evidence_still_checks_the_milestones_file(world):
    g, s, ev = _with_file(world)
    out = run(s.execute("goal_finish", {"ending": "milestone_done", "summary": "done",
                                        "evidence_refs": [ev]}))
    assert out.startswith("Session ending recorded"), out
    assert {"name": "verdicts.md", "revision": 1} in s.ending["artifact_refs"]


# --- the vault ------------------------------------------------------------------------

def test_a_plan_that_writes_outside_the_goal_is_refused_with_the_reason():
    plan = [{"id": "M1", "title": "Final note",
             "expected": "The final note saved to the vault as self/market-positions-review.md",
             "criteria": ["C1"], "depends": []},
            {"id": "M2", "title": "Result", "expected": "result.md", "criteria": ["C1"], "depends": ["M1"]}]
    ms, why = R.validate_plan(plan, ["C1"])
    assert ms is None and "goals" in why and "self/" in why


def test_the_planner_and_sessions_are_told_goal_files_are_in_the_vault():
    assert "jarvis-vault/goals/" in R.PLANNER_PROMPT
    assert "jarvis-vault/goals/" in R.SESSION_SYSTEM
    desc = next(t for t in R.SESSION_TOOLS if t["name"] == "goal_file")["description"]
    assert "jarvis-vault/goals/" in desc


def test_a_written_file_says_where_it_is_in_the_vault(world):
    g, s, _ = _with_file(world)
    out = run(s.execute("goal_file", {"op": "write", "name": "note.md", "content": "a note",
                                      "expected_revision": None, "op_key": "n"}))
    assert f"jarvis-vault/goals/{S.get(g['goal_id'])['folder']}/note.md" in out


# --- stopping ---------------------------------------------------------------------------

def test_rewording_the_same_notes_is_not_progress(world):
    def reword(kw, st):
        if "You plan one goal" in kw["messages"][0]["content"]:
            return _text(json.dumps(PLAN))
        st["n"] = st.get("n", 0) + 1
        if st["n"] == 1:
            return _calls(("goal_note", {"op": "add", "op_key": "next", "section": "Next",
                                         "text": "Search for the fee, attempt 0"}))
        if st["n"] % 2 == 0:
            return _calls(("goal_finish", {"ending": "continue", "summary": "searching"}))
        entry = S.notes(st["gid"])[0]
        return _calls(("goal_note", {"op": "revise", "op_key": f"r{st['n']}", "section": "Next",
                                     "entry_id": entry["entry_id"], "expected_revision": entry["revision"],
                                     "text": f"Search for the fee, attempt {st['n']}"}))
    world.fake.script = reword
    g = _start()
    world.fake.state["gid"] = g["goal_id"]
    run(_cycles(10))
    g = S.get(g["goal_id"])
    assert g["state"] == S.PARTIAL and "no progress" in g["outcome"]["reason"]


def test_one_milestone_gets_a_bounded_number_of_sessions(world, monkeypatch):
    """Real progress every session (a new evidence page each time) but never finishing."""
    def busy(kw, st):
        if "You plan one goal" in kw["messages"][0]["content"]:
            return _text(json.dumps(PLAN))
        st["n"] = st.get("n", 0) + 1
        if st["n"] % 2:
            return _calls(("web_browse", {"task": f"CSPX TER page {st['n']}"}))
        return _calls(("goal_finish", {"ending": "continue", "summary": "still reading"}))

    async def browse(**kw):
        return f"page {kw['task']}"
    from backend.brain import engine as C
    world.fake.script = busy
    monkeypatch.setitem(C.TOOL_DISPATCH, "web_browse", browse)
    g = _start()
    run(_cycles(R.MAX_MILESTONE_SESSIONS * 2 + 6))
    g = S.get(g["goal_id"])
    assert g["state"] in (S.PARTIAL, S.ACTIVE)
    per_gen = {}
    rows = S._conn().execute("SELECT milestone_id, generation FROM goal_sessions WHERE goal_id=?",
                             (g["goal_id"],)).fetchall()
    for mid, gen in rows:
        per_gen[(mid, gen)] = per_gen.get((mid, gen), 0) + 1
    assert max(per_gen.values()) <= R.MAX_MILESTONE_SESSIONS
    assert g["state"] == S.PARTIAL


# --- the card --------------------------------------------------------------------------

def test_the_card_never_cuts_a_sentence(world):
    g = _start()
    reason = ("stuck on M4: M4 requires a file in the vault folder self/, but this session exposes "
              "no vault write tool. vault_read and vault_list are read-only, and goal_file writes only "
              "to the goal-file store. The vault is writable in general (a prior audit landed a note "
              "and read it back), so this is a tool-availability gap in this session, not a vault fault.")
    assert S.transition(g["goal_id"], S.PARTIAL, expect=(S.ACTIVE,), outcome={"reason": reason})
    g = S.get(g["goal_id"])
    line = next(l for l in GT.card_text(g).splitlines() if l.startswith("Ended:"))
    assert line.endswith((".", "…")) and not line.endswith(" The")
    assert len(line) <= 300


# --- a re-plan keeps finished work ------------------------------------------------------

def test_a_replan_that_rewords_a_done_milestone_does_not_redo_it(world):
    g = _start()
    S.save_plan(g["goal_id"], VERDICT_PLAN, actor="jarvis", reason="first plan")
    s = _session(g)
    run(s.execute("goal_file", {"op": "write", "name": "verdicts.md", "content": "NVDA: RENEWED.",
                                "expected_revision": None, "op_key": "v1"}))
    assert S.complete_milestone(g["goal_id"], "M1", s.gen, s.session_id,
                                {"artifact_refs": [{"name": "verdicts.md", "revision": 1}],
                                 "evidence_refs": [], "summary": "done"})
    # The planner rewords M1, as it did on 18 October (positions.md became facts.md).
    reworded = {"milestones": [
        {"id": "M1", "title": "Draft the verdicts again", "expected": "A goal file facts.md",
         "checks": [], "criteria": ["C1"], "depends": []},
        {"id": "M2", "title": "Write the result", "expected": "result.md with every verdict",
         "checks": [], "criteria": ["C1"], "depends": ["M1"]}]}
    world.fake.script = lambda kw, st: _text(json.dumps(reworded))
    assert run(R.plan_goal(S.get(g["goal_id"]), reason="stuck", blocker="M2 could not finish"))
    m1 = next(m for m in S.plan(g["goal_id"]) if m["milestone_id"] == "M1")
    assert m1["state"] == S.MS_DONE and m1["generation"] == 1
    assert m1["expected"] == VERDICT_PLAN[0]["expected"]


# --- 19 October: an audit of his letters could never be planned ---------------------------

@pytest.mark.parametrize("expected", [
    "A goal file corpus.md summarising all 144 letters in the vault letters/ folder, with dates",
    "checks.md comparing what the letters/ claim with queue.md and evolution.md in the vault",
    "result.md: the audit note (the note in self/ he asked for), saved in the vault under goals/",
    "Notes listing every contradiction between letters/ and the queue, backed by evidence",
    "facts.md quoting letters/2026-10-01_8f3b162a.md and the queue",
])
def test_reading_another_folder_is_not_writing_there(expected):
    plan = [{"id": "M1", "title": "Read", "expected": expected, "criteria": ["C1"], "depends": []},
            {"id": "M2", "title": "Result", "expected": "result.md", "criteria": ["C1"], "depends": ["M1"]}]
    ms, why = R.validate_plan(plan, ["C1"])
    assert ms is not None, why


@pytest.mark.parametrize("expected", [
    "The final note saved to the vault as self/market-positions-review.md",
    "The audit note saved in self/ for future sessions",
    "A note written into topics/ describing the corpus",
    "self/letters-audit.md with the findings",
])
def test_an_output_saved_in_another_folder_is_still_refused(expected):
    plan = [{"id": "M1", "title": "Note", "expected": expected, "criteria": ["C1"], "depends": []},
            {"id": "M2", "title": "Result", "expected": "result.md", "criteria": ["C1"], "depends": ["M1"]}]
    ms, why = R.validate_plan(plan, ["C1"])
    assert ms is None and "goal file" in why


def test_the_planner_is_told_reading_is_fine_and_a_note_in_self_is_result_md():
    assert "read anywhere in the vault" in R.PLANNER_PROMPT
    assert "met by result.md" in R.PLANNER_PROMPT
