"""G1a: one durable goal -- plan, sessions, checks, charges, restart.

The real runner, storage, budget, gateway, policy and loop; only the provider client and the
web read are faked. Each test has its own database and vault (backend/conftest.py).
"""
import asyncio
import json
import re
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from backend.brain import background as B
from backend.brain import engine as C
from backend.brain import goal_budget as budget
from backend.brain import goal_runner as R
from backend.brain import goals as GT
from backend.brain import router
from backend.storage import db
from backend.storage import goals as S
from backend.tools import gateway, policy

PAGE = ("iShares CSPX factsheet, 30 Sep 2026: total expense ratio 0.07%, domiciled in Ireland, "
        "physical replication.")


def run(coro):
    return asyncio.run(coro)


def _usage(out=50, cost=0.0004):
    return NS(prompt_tokens=1000, completion_tokens=out, cost=cost,
              prompt_tokens_details=None, completion_tokens_details=None)


def _text(content, cost=0.0004, out=50):
    msg = NS(content=content, tool_calls=None, reasoning_content=None, reasoning=None)
    return NS(choices=[NS(message=msg, finish_reason="stop")], usage=_usage(out, cost), model="m")


def _calls(*calls, cost=0.0004):
    tcs = [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls)]
    msg = NS(content=None, tool_calls=tcs, reasoning_content=None, reasoning=None)
    return NS(choices=[NS(message=msg, finish_reason="tool_calls")], usage=_usage(cost=cost), model="m")


class Fake:
    """A provider that answers through a script function: script(kwargs, state) -> response."""

    def __init__(self, script):
        self.script, self.sent, self.state = script, [], {}
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.sent.append(kw)
        return self.script(kw, self.state)


def _last_evidence(kw):
    for m in reversed(kw["messages"]):
        if m.get("role") == "tool":
            found = re.search(r"\[saved as evidence (ev[0-9a-f]{10})\]", m["content"] or "")
            if found:
                return found.group(1)
    return None


PLAN = {"milestones": [{"id": "M1", "title": "Find CSPX's fee and write the report",
                        "expected": "result.md with the fee, domicile and replication, sourced",
                        "checks": ["fee quoted from a source"], "criteria": ["C1"], "depends": []}]}


def happy_script(kw, st):
    """Planner -> plan; session: browse, write result.md quoting the page, finish; checker ok."""
    first = kw["messages"][0]["content"]
    if "You plan one goal" in first:
        return _text(json.dumps(PLAN))
    if "Check one finished goal" in first:
        return _text(json.dumps({"met": ["C1"], "unmet": []}))
    st["n"] = st.get("n", 0) + 1
    if st["n"] == 1:
        return _calls(("web_browse", {"task": "CSPX TER"}))
    if st["n"] == 2:
        ev = _last_evidence(kw)
        report = (f'CSPX costs "total expense ratio 0.07%" [{ev}], it is Irish-domiciled and '
                  f"physically replicated [{ev}].")
        return _calls(("goal_note", {"op": "add", "op_key": "fee", "section": "Findings",
                                     "text": "CSPX TER 0.07%", "evidence_ids": [ev]}),
                      ("goal_file", {"op": "write", "name": "result.md", "content": report,
                                     "expected_revision": None, "op_key": "report"}))
    return _calls(("goal_finish", {"ending": "milestone_done", "summary": "report written",
                                   "artifact_refs": [{"name": "result.md", "revision": 1}]}))


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_GOALS", "1")
    from backend.tools import vault
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    assert R.acquire_lock(tmp_path / "goals.lock")
    sent = []

    async def notify(text, key):
        sent.append((key, text))
        return True
    R.set_notifier(notify)
    reads = []

    async def browse(**kw):
        reads.append(kw)
        return PAGE
    monkeypatch.setitem(C.TOOL_DISPATCH, "web_browse", browse)
    fake = Fake(happy_script)
    monkeypatch.setattr(R, "_client", lambda: fake)
    monkeypatch.setattr(B, "_stop_if_paused", lambda: None)
    monkeypatch.setattr(R, "_task", None)
    monkeypatch.setattr(R, "_parked", [])
    yield NS(fake=fake, sent=sent, reads=reads)
    R.set_notifier(None)
    R.release_lock()


def _start(title="CSPX fee report", criteria=("CSPX's fee, domicile and replication, sourced",)):
    return S.create(title=title, words="research CSPX's fee for me and keep at it",
                    conversation_id="discord:dm", criteria=list(criteria), state=S.ACTIVE,
                    budget_microusd=budget.micro(1.0), deadline_at=None, creation_key=title)


async def _cycles(n=6):
    for _ in range(n):
        await R._cycle()
        if R._task is not None:
            await R._task
            R._task = None


# --- the whole goal -------------------------------------------------------------------

def test_a_read_goal_plans_works_checks_and_is_delivered_once(world):
    g = _start()
    run(_cycles())
    g = S.get(g["goal_id"])
    assert g["state"] == S.DONE
    assert S.read_file(g["goal_id"], "result.md")["revision"] == 1
    ends = [k for k, _ in world.sent if k.endswith("end:done")]
    assert len(ends) == 1 and "0.07%" in world.sent[-1][1]
    # every call was charged and settled; the goal paid what the provider reported
    charges = S.charges(g["goal_id"])
    assert {c["purpose"] for c in charges} == {"planner", "session", "checker"}
    assert all(c["state"] == "settled" for c in charges)
    run(_cycles(2))                                    # nothing runs, nothing is sent again
    assert len([k for k, _ in world.sent if k.endswith("end:done")]) == 1


def test_an_unaccepted_proposal_dispatches_nothing(world):
    S.create(title="Flat hunt", words="maybe look for flats", conversation_id="discord:dm",
             criteria=["listings under 1000"], state=S.PROPOSED, budget_microusd=10**6,
             deadline_at=None, creation_key="p1")
    run(_cycles())
    assert world.fake.sent == [] and world.reads == []


def test_a_session_crash_holds_its_charge_and_the_redo_reuses_saved_evidence(world, monkeypatch):
    g = _start()
    run(_cycles(1))                                    # plan
    real = world.fake.script

    def crash_after_read(kw, st):
        if "goal_session" in st or "You plan one goal" in kw["messages"][0]["content"]:
            return real(kw, st)
        st["n"] = st.get("n", 0) + 1
        if st["n"] == 1:
            return _calls(("web_browse", {"task": "CSPX TER"}))
        raise RuntimeError("process died")              # after the evidence commit
    world.fake.script = crash_after_read
    run(_cycles(1))
    [c] = [c for c in S.charges(g["goal_id"]) if c["purpose"] == "session"]
    assert c["state"] == "held" and c["settled_microusd"] == c["reserved_microusd"]
    assert len(S.evidence_index(g["goal_id"])) == 1    # the read survived
    assert S.ready_milestone(g["goal_id"])["milestone_id"] == "M1"
    world.fake.state.clear()
    world.fake.script = real
    run(_cycles())
    assert S.get(g["goal_id"])["state"] == S.DONE
    # the redo's brief listed the evidence the crashed session had saved
    briefs = [kw["messages"][1]["content"] for kw in world.fake.sent
              if kw["messages"][0]["content"].startswith("You are JARVIS, working one milestone")]
    assert "Saved evidence" in briefs[-1] and "ev" in briefs[-1].split("Saved evidence")[1]


def test_recovery_after_a_crash_mid_session(world):
    g = _start()
    run(_cycles(1))
    m = S.ready_milestone(g["goal_id"])
    sid = S.claim_session(g["goal_id"], "M1", m["generation"], "r-crash")
    charge, _ = budget.reserve(g["goal_id"], "session", R._session_ceiling(), session_id=sid)
    [s] = S.recover()                                   # the restarted process
    assert s["session_id"] == sid
    assert [c["state"] for c in S.charges(g["goal_id"]) if c["charge_id"] == charge] == ["held"]
    run(_cycles())
    assert S.get(g["goal_id"])["state"] == S.DONE


def test_a_second_coordinator_is_refused(world, tmp_path):
    code = ("import sys; sys.path.insert(0, '.'); from pathlib import Path;"
            "from backend.brain import goal_runner as R;"
            f"print(R.acquire_lock(Path(r'{tmp_path / 'goals.lock'}')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr


def test_a_cancelled_goals_running_session_writes_nothing(world):
    g = _start()
    run(_cycles(1))
    m = S.ready_milestone(g["goal_id"])
    sid = S.claim_session(g["goal_id"], "M1", m["generation"], "r1")
    sess = R.Session(S.get(g["goal_id"]), m, sid, "r1")
    S.transition(g["goal_id"], S.CANCELLED, expect=(S.ACTIVE,))
    out = run(sess.execute("goal_note", {"op": "add", "op_key": "x", "section": "Findings",
                                         "text": "late"}))
    assert "no longer current" in out and S.notes(g["goal_id"]) == []
    out = run(sess.execute("web_browse", {"task": "CSPX"}))
    assert "no longer current" in out and S.evidence_index(g["goal_id"]) == []


# --- money ------------------------------------------------------------------------------

def test_goal_calls_have_sdk_retries_off(monkeypatch):
    seen = {}

    class Pooled:
        def with_options(self, **kw):
            seen.update(kw)
            return self
    monkeypatch.setattr(C, "_get_compat_client", lambda: Pooled())
    R._client()
    assert seen == {"max_retries": 0}


def test_a_null_reported_cost_is_settled_from_the_table(world):
    def no_cost(kw, st):
        r = happy_script(kw, st)
        r.usage.cost = None
        return r
    world.fake.script = no_cost
    g = _start()
    run(_cycles())
    for c in S.charges(g["goal_id"]):
        assert c["settled_microusd"] > 0                # never zero for a call that ran


def test_no_session_is_admitted_past_the_cap_and_the_goal_pauses_once(world):
    g = S.create(title="Tiny", words="w", conversation_id="d", criteria=["x"], state=S.ACTIVE,
                 budget_microusd=50_000, deadline_at=None, creation_key="tiny")
    run(_cycles())
    assert world.fake.state.get("n") is None           # no session call was ever sent
    g = S.get(g["goal_id"])
    assert g["state"] == S.PAUSED
    assert len([k for k, _ in world.sent if ":cap:" in k]) == 1


def test_the_output_budget_bounds_every_call_including_recovery(monkeypatch):
    monkeypatch.setattr(B, "_with_skills", lambda s, t: (s, t))
    monkeypatch.setattr(B, "_stop_if_paused", lambda: None)
    sent = []

    def resp(kw):
        sent.append(kw["max_tokens"])
        msg = NS(content="", tool_calls=None, reasoning_content=None, reasoning=None)
        return NS(choices=[NS(message=msg, finish_reason="length")],
                  usage=NS(prompt_tokens=10, completion_tokens=kw["max_tokens"], cost=None,
                           prompt_tokens_details=None), model="m")

    async def create(**kw):
        return resp(kw)
    out = run(B._run_compat_loop("x", client=NS(chat=NS(completions=NS(create=create))),
                                 output_budget=5000))
    assert sum(sent) <= 5000 and sent[0] == 4096 and sent[1] == 5000 - 4096
    assert out.status == B.INCOMPLETE and out.output_tokens == sum(sent)


# --- progress and checks -----------------------------------------------------------------

def test_duplicate_notes_do_not_count_as_progress(world):
    def spin(kw, st):
        if "You plan one goal" in kw["messages"][0]["content"]:
            return _text(json.dumps(PLAN))
        st["n"] = st.get("n", 0) + 1
        if st["n"] % 2:
            return _calls(("goal_note", {"op": "add", "op_key": f"k{st['n']}",
                                         "section": "Next", "text": "Search for the fee"}))
        return _calls(("goal_finish", {"ending": "continue", "summary": "will search next"}))
    world.fake.script = spin
    g = _start()
    run(_cycles(8))
    g = S.get(g["goal_id"])
    assert g["state"] == S.PARTIAL and "no progress" in g["outcome"]["reason"]


def test_a_quote_not_in_its_evidence_is_refused_but_a_derived_number_is_not():
    g = _start()
    S.save_plan(g["goal_id"], [{"id": "M1", "title": "t", "expected": "result.md", "checks": [],
                                "criteria": ["C1"], "depends": []}], actor="j", reason="r")
    ev = S.save_evidence(g["goal_id"], "M1", 1, session_id="s", run_id=None, tool="web_browse",
                         args={}, body=PAGE, metadata={})
    assert R.check_quotes(g["goal_id"], f'The fee is "total expense ratio 0.07%" [{ev}].') == ""
    assert "not in" in R.check_quotes(g["goal_id"], f'The fee is "expense ratio of 0.05%" [{ev}].')
    # a derived figure (0.07% of 10,000 = 7) need not appear literally
    assert R.check_quotes(g["goal_id"], f"On 10,000 euros that is 7 euros a year [{ev}].") == ""
    assert "not evidence" in R.check_quotes(g["goal_id"], "made up [ev0123456789].")


def test_milestone_done_without_its_artifact_is_not_accepted(world):
    g = _start()
    run(_cycles(1))
    m = S.ready_milestone(g["goal_id"])
    sid = S.claim_session(g["goal_id"], "M1", m["generation"], "r1")
    sess = R.Session(S.get(g["goal_id"]), m, sid, "r1")
    out = run(sess.execute("goal_finish", {"ending": "milestone_done", "summary": "done",
                                           "artifact_refs": [{"name": "result.md", "revision": 1}]}))
    assert out.startswith("Error") and sess.ending is None


def test_done_is_refused_while_the_checker_finds_an_unmet_requirement(world):
    def strict(kw, st):
        if "Check one finished goal" in kw["messages"][0]["content"]:
            return _text(json.dumps({"met": [], "unmet": [{"id": "C1", "why": "no domicile"}]}))
        return happy_script(kw, st)
    world.fake.script = strict
    g = _start()
    run(_cycles(12))
    g = S.get(g["goal_id"])
    assert g["state"] == S.PARTIAL and "unmet" in g["outcome"]["reason"]
    assert any(m["milestone_id"] == "R1" for m in S.plan(g["goal_id"]))


def test_a_replan_cannot_drop_a_requirement():
    plan = [{"id": "M1", "title": "t", "expected": "result.md", "criteria": ["C1"], "depends": []}]
    assert R.validate_plan(plan, ["C1", "C2"])[0] is None
    assert "not covered" in R.validate_plan(plan, ["C1", "C2"])[1]
    assert R.validate_plan(plan, ["C1"])[0] is not None
    bad = [{"id": "M1", "title": "t", "expected": "x", "criteria": ["C1"], "depends": ["M2"]},
           {"id": "M2", "title": "t", "expected": "result.md", "criteria": ["C1"], "depends": []}]
    assert R.validate_plan(bad, ["C1"])[0] is None                 # a forward (cyclic) reference


def test_sessions_never_reach_mem0_ingestion(world, monkeypatch):
    from backend.brain import memory

    def boom(*a, **k):
        raise AssertionError("a goal session reached mem0 ingestion")
    for name in ("_do_ingest", "ingest"):
        if hasattr(memory, name):
            monkeypatch.setattr(memory, name, boom)
    _start()
    run(_cycles())


def test_only_reads_and_goal_tools_are_offered_in_a_session():
    names = {t["name"] for t in R._read_tool_schemas()} | set(R.SESSION_SCHEMAS)
    assert "vault_write" not in names and "run_in_background" not in names
    assert "goal" not in names and "schedule_task" not in names
    gmail = next(t for t in R._read_tool_schemas() if t["name"] == "gmail_action")
    assert "send_email" not in gmail["input_schema"]["properties"]["action"]["enum"]


def test_a_session_cannot_send_mail_even_by_name(world):
    g = _start()
    run(_cycles(1))
    m = S.ready_milestone(g["goal_id"])
    sid = S.claim_session(g["goal_id"], "M1", m["generation"], "r1")
    sess = R.Session(S.get(g["goal_id"]), m, sid, "r1")
    out = run(sess.execute("gmail_action", {"action": "send_email", "to": "x@y.z",
                                            "subject": "s", "body": "b"}))
    assert out.startswith("Error") and "not available" in out


# --- the goal tool -----------------------------------------------------------------------

def _as_live(words):
    policy.start_scope(words=[words])


def test_start_without_his_words_becomes_a_start_card_and_runs_nothing(world, monkeypatch):
    asked = []

    async def request(**kw):
        asked.append(kw)
        return {"id": 7, "state": "pending"}
    from backend.tools import approval_flow
    monkeypatch.setattr(approval_flow, "request", request)
    monkeypatch.setattr(approval_flow, "waiting_text", lambda row, ctx: "waiting #7")
    _as_live("compare the ETFs over the next days")
    p = run(GT.goal("propose", title="ETF comparison", criteria=["fees and domicile, sourced"]))
    pid = re.search(r"Proposal (g[0-9a-f]+)", p).group(1)
    res = run(gateway.execute("goal", {"action": "start", "proposal_id": pid, "quote": "yes"},
                              dispatch=C.TOOL_DISPATCH, caller=policy.LIVE))
    assert res.pending and asked and S.get(pid)["state"] == S.PROPOSED


def test_start_with_his_words_starts_the_proposal(world):
    _as_live("Yes, start that ETF goal please")
    p = run(GT.goal("propose", title="ETF comparison", criteria=["fees and domicile, sourced"]))
    pid = re.search(r"Proposal (g[0-9a-f]+)", p).group(1)
    res = run(gateway.execute("goal", {"action": "start", "proposal_id": pid,
                                       "quote": "start that ETF goal"},
                              dispatch=C.TOOL_DISPATCH, caller=policy.LIVE))
    assert res.ok and S.get(pid)["state"] == S.ACTIVE


def test_a_proposal_is_not_made_twice_for_the_same_request(world):
    _as_live("compare the ETFs")
    a = run(GT.goal("propose", title="ETF comparison"))
    b = run(GT.goal("propose", title="ETF  comparison"))
    assert a.startswith("Proposal") and len(S.list_goals((S.PROPOSED,))) == 1 and b


def test_cancel_needs_his_words(world):
    g = _start()
    _as_live("stop the CSPX goal")
    out = run(gateway.execute("goal", {"action": "cancel", "goal_id": g["goal_id"],
                                       "quote": "something else entirely"},
                              dispatch=C.TOOL_DISPATCH, caller=policy.LIVE))
    assert "needs his words" in str(out.result) and S.get(g["goal_id"])["state"] == S.ACTIVE
    out = run(gateway.execute("goal", {"action": "cancel", "goal_id": g["goal_id"],
                                       "quote": "stop the CSPX goal"},
                              dispatch=C.TOOL_DISPATCH, caller=policy.LIVE))
    assert out.ok and S.get(g["goal_id"])["state"] == S.CANCELLED


def test_background_jobs_cannot_reach_the_goal_tool(world):
    assert "goal" not in B._ALLOWED_TOOLS
    out = run(B._bg_exec("goal", {"action": "start", "title": "x"}))
    assert "not available" in out


def test_the_tool_is_hidden_while_goals_are_off(monkeypatch):
    monkeypatch.setenv("JARVIS_GOALS", "0")
    assert "goal" not in {t["name"] for t in C.tools_for_channel("discord")}
    monkeypatch.setenv("JARVIS_GOALS", "1")
    assert "goal" in {t["name"] for t in C.tools_for_channel("discord")}


def test_the_turn_line_names_running_goals_and_a_waiting_proposal(world):
    g = _start()
    S.create(title="Flat hunt", words="w", conversation_id="d", criteria=["x"],
             state=S.PROPOSED, budget_microusd=10**6, deadline_at=None, creation_key="fh")
    line = GT.prompt_line()
    assert g["goal_id"] in line and "Flat hunt" in line and "goal start" in line


def test_claiming_a_started_goal_needs_a_successful_start():
    problems = C._unbacked_claims("I've started a goal for the ETF comparison.", set(), 0,
                                  False, False, frozenset())
    assert problems
    assert not C._unbacked_claims("I've started a goal for the ETF comparison.", {"goal:start"},
                                  1, False, False, frozenset())


def test_no_session_tool_ever_needs_approval():
    """Session tools name their operation `op`; a per-action policy entry would read `action`,
    find nothing and fall to "unclassified: ask Sam" -- which is how goal_file writes first
    became approval requests in testing."""
    for name in R.SESSION_SCHEMAS:
        for op in ("add", "write", "read", "list", "revise", "remove", ""):
            assert policy.risk_of(name, {"op": op}) in (policy.READ, policy.OWN), (name, op)


def test_consolidation_keeps_goal_state_out_of_letters_portrait_and_queue():
    """Goal requirements, progress, spending and results live in the goal records; the
    session-end prompt says so in its tool-owned-state rule."""
    import inspect
    from backend.brain import consolidation
    src = inspect.getsource(consolidation)
    rule = src[src.index("What a tool keeps is not yours to record."):][:900]
    assert "«his» goals" in rule and "spending" in rule


def test_a_night_task_waits_while_the_three_slots_are_held(tmp_path, monkeypatch):
    import datetime
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "s.md")
    from backend.tools import vault
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")      # a successful run writes a draft
    chronos._write_schedule([chronos._format_night_line("2026-09-30", "", "research ECB rates in depth")])
    ran = []

    async def execute(prompt):
        ran.append(prompt)
        return "ok"
    monkeypatch.setattr(B, "execute_task", execute)
    now = datetime.datetime(2026, 10, 1, 3, 0, tzinfo=chronos._TZ)
    with B.occupy("goal"), B.occupy("goal"), B.occupy("night"):
        run(chronos._run_night_task(now))
    assert ran == [] and [n for n in map(chronos._parse_night_line, chronos._read_schedule()) if n]
    run(chronos._run_night_task(now))
    assert len(ran) == 1


def test_a_session_that_answers_in_prose_is_told_to_finish_and_carries_on(world):
    """Live (2026-10-05): one session in five replied in prose with no goal_finish and was
    wasted. Before its last step it is now told to end with goal_finish."""
    def prose_then_finish(kw, st):
        first = kw["messages"][0]["content"]
        if "You plan one goal" in first:
            return _text(json.dumps(PLAN))
        if "Check one finished goal" in first:
            return _text(json.dumps({"met": ["C1"], "unmet": []}))
        st["n"] = st.get("n", 0) + 1
        if st["n"] == 1:
            return _text("I have looked into it and will write it up next time.")
        if st["n"] == 2:
            assert kw["messages"][-1]["content"] == R.FINAL_NUDGE
            return _calls(("goal_finish", {"ending": "continue", "summary": "nothing yet"}))
        return happy_script(kw, {"n": st["n"] - 2})
    world.fake.script = prose_then_finish
    g = _start()
    run(_cycles(1))
    run(_cycles(1))
    conn = db.connect()
    [s] = [dict(r) for r in conn.execute("SELECT state, ending_json FROM goal_sessions")]
    assert s["state"] == S.COMPLETE and json.loads(s["ending_json"])["ending"] == "continue"


def test_a_file_name_with_a_folder_is_refused_with_the_name_to_use():
    assert "use 'evidence.md'" in S.valid_name("notes/evidence.md")
