"""G2: waits, questions, approvals and child jobs (docs/archive/GOALS-ROADMAP.md section 13, G2).

The real runner, goal storage, approvals, watches, jobs, delivery receipts, gateway and policy;
the provider client, the research launch, the email tool and the clock are faked. No real email,
calendar event or research run is ever made. Each test has its own database
(backend/conftest.py).
"""
import asyncio
import json
import re
from types import SimpleNamespace as NS

import pytest

from backend.brain import background as B
from backend.brain import engine as C
from backend.brain import goal_budget as budget
from backend.brain import goal_runner as R
from backend.brain import goals as GT
from backend.brain import router
from backend.brain import watches as W
from backend.storage import approvals as A
from backend.storage import db, jobs
from backend.storage import goals as S
from backend.storage import watches as WS
from backend.tools import approval_flow as AF
from backend.tools import policy

from backend.brain.test_goal_runner import Fake, _calls, _text, run

T0 = 1_792_000_000.0                        # Wed 2026-10-14 19:46 Paris
TWO_MS = {"milestones": [
    {"id": "M1", "title": "Ask the agency", "expected": "their answer, sourced",
     "checks": [], "criteria": ["C1"], "depends": []},
    {"id": "M2", "title": "Compare the listings", "expected": "compare.md",
     "checks": [], "criteria": ["C1"], "depends": []}]}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_GOALS", "1")
    from backend.tools import vault
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    assert R.acquire_lock(tmp_path / "goals.lock")
    clock = NS(t=T0)
    monkeypatch.setattr(R, "_now", lambda: clock.t)
    sent = []

    async def notify(text, key):
        sent.append((key, text))
        return True
    R.set_notifier(notify)
    emails = []

    async def gmail(**kw):                  # never a real email
        emails.append(kw)
        return "Email sent."
    monkeypatch.setitem(C.TOOL_DISPATCH, "gmail_action", gmail)
    launches = []

    async def launch(question, depth, goal_id):
        jid = jobs.enqueue({"kind": "web_research", "description": question, "goal_id": goal_id})
        jobs.claim(jid)
        launches.append(jid)
        return NS(job_id=jid, _handle=None), ""
    monkeypatch.setattr(R._bg, "start_goal_research", launch)
    monkeypatch.setattr(R, "_client", lambda: Fake(lambda kw, st: _text("{}")))
    monkeypatch.setattr(B, "_stop_if_paused", lambda: None)
    monkeypatch.setattr(R, "_task", None)
    monkeypatch.setattr(R, "_parked", [])
    AF.set_notifier(None)
    AF._undelivered.clear()
    tokens = []

    def say(words, quote=None):
        """His words this turn and the quote a call carries (what the gateway would set)."""
        words = [words] if isinstance(words, str) else list(words)
        tokens.append((policy.turn_words, policy.turn_words.set(words)))
        tokens.append((policy.call_quote, policy.call_quote.set(quote if quote is not None else words[-1])))
    yield NS(clock=clock, sent=sent, emails=emails, launches=launches, say=say)
    for var, tok in reversed(tokens):
        var.reset(tok)
    R.set_notifier(None)
    R.release_lock()


def _goal(state=S.ACTIVE, plan=TWO_MS, deadline=None, words="find me a flat, write to the agency"):
    g = S.create(title="Flat hunt", words=words, conversation_id="discord:dm",
                 criteria=["a shortlist of flats"], state=state,
                 budget_microusd=budget.micro(1.0), deadline_at=deadline, creation_key=words)
    S.save_plan(g["goal_id"], plan["milestones"], actor="jarvis", reason="test")
    return S.get(g["goal_id"])


def _session(g, mid="M1"):
    ms = next(m for m in S.plan(g["goal_id"]) if m["milestone_id"] == mid)
    return R.Session(S.get(g["goal_id"]), ms, "sess-test", "run-test")


def _ms(g, mid):
    return next(m for m in S.plan(g["goal_id"]) if m["milestone_id"] == mid)


def _email(to="agency@flats.example", body="Is the Rue Oberkampf flat still free?"):
    return {"action": "send_email", "to": to, "subject": "Flat", "body": body}


# ------------------------------------------------------------------ time waits

def test_a_time_wait_resolves_on_its_time_and_not_before(world):
    g = _goal()
    s = _session(g)
    assert "recorded" in run(s.goal_finish("wait_until", "agency answers Mondays", op_key="mon",
                                           when="2026-10-19T09:00:00+02:00"))
    S.set_milestone(g["goal_id"], "M1", 1, S.WAITING_MS, expect=(S.READY,))
    world.clock.t = T0 + 3600
    assert run(R.reconcile_waits()) == 0
    assert _ms(g, "M1")["state"] == S.WAITING_MS
    world.clock.t = T0 + 5 * 86400                      # Mon 19 Oct 19:46, past 09:00
    assert run(R.reconcile_waits()) == 1
    assert _ms(g, "M1")["state"] == S.READY
    assert run(R.reconcile_waits()) == 0               # exactly one continuation


def test_a_time_wait_must_be_in_the_future_and_carry_its_offset(world):
    s = _session(_goal())
    assert "Error" in run(s.goal_finish("wait_until", "x", op_key="a", when="2026-10-19 09:00"))
    assert "passed" in run(s.goal_finish("wait_until", "x", op_key="b",
                                         when="2026-10-01T09:00:00+02:00"))
    assert "op_key" in run(s.goal_finish("wait_until", "x", when="2026-10-19T09:00:00+02:00"))


# ------------------------------------------------------------------ child jobs

def test_a_job_finished_before_its_wait_is_registered_still_wakes_the_goal(world):
    g = _goal()
    s = _session(g)
    out = run(s.goal_job("start", op_key="agencies", task="Paris 11e rental agencies"))
    jid = world.launches[0]
    assert jid in out
    jobs.finish(jid, "done", {"kind": "web_research", "summary": "found 4",
                              "result": "Agence Bastille, rue de la Roquette ...", "goal_id": g["goal_id"]})
    assert "recorded" in run(s.goal_finish("wait_for_job", "waiting on research", op_key="w",
                                           job_id=jid))
    S.set_milestone(g["goal_id"], "M1", 1, S.WAITING_MS, expect=(S.READY,))
    assert run(R.reconcile_waits()) == 1
    assert _ms(g, "M1")["state"] == S.READY
    # the job's report is saved as the goal's evidence; the effect records what it became
    assert any(e["tool"] == "web_research" for e in S.evidence_index(g["goal_id"]))
    [e] = S.effects(g["goal_id"], "job")
    assert e["state"] == "done"


def test_the_same_job_key_never_launches_twice(world):
    g = _goal()
    s = _session(g)
    run(s.goal_job("start", op_key="k", task="Paris agencies"))
    again = run(_session(g).goal_job("start", op_key="k", task="Paris agencies"))
    assert len(world.launches) == 1 and "Already started" in again
    changed = run(_session(g).goal_job("start", op_key="k", task="Lyon agencies"))
    assert "different" in changed and len(world.launches) == 1


def test_a_lost_acceptance_is_never_launched_again(world, monkeypatch):
    g = _goal()

    async def dies(question, depth, goal_id):
        jid = jobs.enqueue({"kind": "web_research", "goal_id": goal_id})
        world.launches.append(jid)
        raise RuntimeError("process died before the acceptance was recorded")
    monkeypatch.setattr(R._bg, "start_goal_research", dies)
    with pytest.raises(RuntimeError):
        run(_session(g).goal_job("start", op_key="k", task="Paris agencies"))
    [e] = S.effects(g["goal_id"], "job")
    assert e["state"] == "starting"                    # unknown, not failed
    out = run(_session(g).goal_job("start", op_key="k", task="Paris agencies"))
    assert "no confirmed outcome" in out and len(world.launches) == 1


def test_the_research_quota_counts_across_a_restart_and_unknown_is_not_free(world):
    g = _goal()
    for i in range(4):
        run(_session(g).goal_job("start", op_key=f"k{i}", task=f"question {i}"))
    # one launch whose outcome is unknown (effect begun, never accepted)
    S.effect_begin(g["goal_id"], "M1", 1, kind="job", logical_key="M1:1:lost",
                   request={"x": 1}, quota_meter="tinyfish_research", quota_units=1)
    db.close_all()                                     # a restart: nothing in memory survives
    out = run(_session(g).goal_job("start", op_key="k9", task="one more"))
    assert "five research runs are used" in out and len(world.launches) == 4


def test_a_goal_job_is_counted_once_not_twice(world):
    g = _goal()
    run(_session(g).goal_job("start", op_key="k", task="q"))
    assert S.quota_used_today("tinyfish_research") == 1
    assert B.research_launched_today() == 0            # the job row is the goal's, counted above


def test_an_unavailable_adapter_fails_cleanly_and_spends_no_quota(world, monkeypatch):
    async def offline(question, depth, goal_id):
        return None, "web research is unavailable (the PC is offline)"
    monkeypatch.setattr(R._bg, "start_goal_research", offline)
    g = _goal()
    out = run(_session(g).goal_job("start", op_key="k", task="q"))
    assert "did not start" in out and "offline" in out
    assert S.quota_used_today("tinyfish_research") == 0
    assert "not a job" in run(_session(g).goal_finish("wait_for_job", "x", op_key="w", job_id="nope"))


# ------------------------------------------------------------------ questions

def test_an_independent_milestone_runs_while_a_question_waits(world):
    g = _goal()
    s = _session(g, "M1")
    assert "recorded" in run(s.goal_finish("ask", "need his budget", op_key="q",
                                           question="What is your monthly budget?",
                                           blocked_milestone_ids=["M1"]))
    S.set_milestone(g["goal_id"], "M1", 1, S.WAITING_MS, expect=(S.READY,))
    run(R.reconcile_waits())
    assert S.ready_milestone(g["goal_id"])["milestone_id"] == "M2"
    assert S.get(g["goal_id"])["state"] == S.ACTIVE
    assert any("question" in k and "monthly budget" in t for k, t in world.sent)


def test_a_question_reaches_the_turn_and_his_answer_unblocks_once(world):
    g = _goal()
    run(_session(g).goal_finish("ask", "x", op_key="q", question="Max rent?",
                                blocked_milestone_ids=["M1", "M2"]))
    for mid in ("M1",):
        S.set_milestone(g["goal_id"], mid, 1, S.WAITING_MS, expect=(S.READY,))
    run(R.reconcile_waits())
    assert S.get(g["goal_id"])["state"] == S.WAITING
    [w] = S.waits(g["goal_id"])
    assert w["wait_id"] in GT.prompt_line() and "Max rent?" in GT.prompt_line()
    world.say("1400 a month max")
    out = run(GT._answer(goal_id="", wait_id=w["wait_id"], text="€1,400 a month"))
    assert "recorded" in out
    again = run(GT._answer(goal_id="", wait_id=w["wait_id"], text="€1,400"))
    assert "already settled" in again                    # a duplicate answer continues nothing
    assert {m["state"] for m in S.plan(g["goal_id"])} == {S.READY}
    assert S.get(g["goal_id"])["state"] == S.ACTIVE
    assert GT.prompt_line().count("Max rent?") == 0


def test_an_answer_must_be_his_words(world):
    g = _goal()
    run(_session(g).goal_finish("ask", "x", op_key="q", question="Max rent?",
                                blocked_milestone_ids=["M1"]))
    [w] = S.waits(g["goal_id"])
    world.say("what's the weather", quote="1400 a month max")
    assert "his own words" in run(GT._answer(goal_id="", wait_id=w["wait_id"], text="1400"))
    assert S.get_wait(w["wait_id"])["state"] == S.OPEN


def test_an_unanswered_question_resolves_after_a_week(world):
    g = _goal()
    run(_session(g).goal_finish("ask", "x", op_key="q", question="Max rent?",
                                blocked_milestone_ids=["M1"]))
    S.set_milestone(g["goal_id"], "M1", 1, S.WAITING_MS, expect=(S.READY,))
    world.clock.t = T0 + 6 * 86400
    run(R.reconcile_waits())
    assert _ms(g, "M1")["state"] == S.WAITING_MS         # six days: still asked
    world.clock.t = T0 + 8 * 86400
    run(R.reconcile_waits())
    assert _ms(g, "M1")["state"] == S.READY
    assert S.resolved_waits(g["goal_id"], "M1")[0]["outcome"] == {"expired": True}


def test_pause_needs_his_words(world):
    g = _goal()
    world.say("what's the weather", quote="pause the flat hunt")
    assert "needs his words" in run(GT._pause(goal_id=g["goal_id"]))
    assert S.get(g["goal_id"])["state"] == S.ACTIVE


def test_an_old_question_is_not_put_on_every_turn(world, monkeypatch):
    g = _goal()
    run(_session(g).goal_finish("ask", "x", op_key="q", question="Max rent?",
                                blocked_milestone_ids=["M1"]))
    conn = db.connect()
    with conn:
        conn.execute("UPDATE goal_waits SET created_at=created_at-2*86400")
    assert "Max rent?" not in GT.prompt_line()
    assert "Max rent?" in GT.show_text(g["goal_id"])


# ------------------------------------------------------------------ approvals

def _propose(world, g, key="mail", args=None):
    return run(_session(g).goal_action(key, "gmail_action", args or _email(),
                                       "ask the agency whether the flat is free"))


def test_a_critical_action_only_becomes_one_approval_request(world):
    g = _goal()
    out = _propose(world, g)
    assert "approval as request #" in out and world.emails == []
    [row] = A.pending()
    assert row["context"] == "goal" and "Flat hunt" in row["preview"] + row["why"]
    again = _propose(world, g)
    assert "Already proposed" in again and len(A.pending()) == 1
    changed = _propose(world, g, args=_email(body="something else"))
    assert "different action" in changed and len(A.pending()) == 1


def test_only_email_and_calendar_writes_can_be_proposed(world):
    g = _goal()
    out = run(_session(g).goal_action("k", "gmail_action", {"action": "list_inbox"}, "x"))
    assert "proposes only" in out and A.pending() == []
    bad = run(_session(g).goal_action("k2", "gmail_action",
                                      {**_email(), "cc_everyone": True}, "x"))
    assert "unknown argument" in bad and A.pending() == []


def test_the_tap_alone_does_not_wake_the_goal_the_outcome_does(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    assert A.claim(row["id"], "button") is not None     # executing: a tap, no outcome yet
    run(R.reconcile_waits())
    assert S.waits(g["goal_id"])[0]["state"] == S.OPEN
    A.finish(row["id"], A.EXECUTED, "Email sent.")
    run(R.reconcile_waits())
    [w] = S.resolved_waits(g["goal_id"], "M1")
    assert w["outcome"]["approval"] == A.EXECUTED
    [e] = S.effects(g["goal_id"], "approval")
    assert e["state"] == A.EXECUTED


@pytest.mark.parametrize("final, expect", [
    (A.DENIED, "denied"), (A.EXPIRED, "expired"), (A.FAILED, "failed"),
    (A.UNCERTAIN, "may have been sent: do not repeat it")])
def test_denied_expired_failed_and_uncertain_outcomes_come_back(world, final, expect):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    conn = db.connect()
    with conn:
        conn.execute("UPDATE approvals SET state=? WHERE id=?", (final, row["id"]))
    run(R.reconcile_waits())
    [w] = S.resolved_waits(g["goal_id"], "M1")
    assert expect in json.dumps(w["outcome"])
    assert _ms(g, "M1")["state"] == S.READY


def test_a_late_tap_after_expiry_runs_nothing(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    conn = db.connect()
    with conn:
        conn.execute("UPDATE approvals SET expires_at=? WHERE id=?", (1.0, row["id"]))
    run(AF.approve(row["id"], "button"))
    assert world.emails == []


def test_pause_withdraws_first_and_a_tap_after_it_runs_nothing(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    world.say("pause the flat hunt")
    out = run(GT._pause(goal_id=g["goal_id"]))
    assert "paused" in out and "withdrawn" in out
    assert A.get(row["id"])["state"] == A.DENIED
    run(AF.approve(row["id"], "button"))
    assert world.emails == []


def test_a_tap_that_lands_before_the_pause_is_recorded_in_flight(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    A.claim(row["id"], "button")                         # the tap wins the race
    world.say("pause the flat hunt")
    run(GT._pause(goal_id=g["goal_id"]))
    assert A.get(row["id"])["state"] == A.EXECUTING
    [e] = S.effects(g["goal_id"], "approval")
    assert e["state"] == "in_flight"
    A.finish(row["id"], A.EXECUTED, "Email sent.")
    run(R.reconcile_waits())                             # recorded even while paused
    assert S.effects(g["goal_id"], "approval")[0]["state"] == A.EXECUTED


def test_resume_proposes_a_withdrawn_action_again_as_a_new_request(world):
    g = _goal()
    _propose(world, g)
    [first] = A.pending()
    world.say("pause it")
    run(GT._pause(goal_id=g["goal_id"]))
    world.say("resume the flat hunt")
    out = run(GT._resume(goal_id=g["goal_id"]))
    assert "proposed again" in out
    [second] = A.pending()
    assert second["id"] != first["id"] and second["arguments"] == first["arguments"]
    assert world.emails == []


def test_cancel_withdraws_before_the_goal_ends(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    world.say("cancel the flat hunt")
    run(GT._cancel(goal_id=g["goal_id"]))
    assert A.get(row["id"])["state"] == A.DENIED
    assert S.waits(g["goal_id"]) == []
    run(AF.approve(row["id"], "text"))
    assert world.emails == []


def test_a_replan_withdraws_the_requests_of_milestones_it_replaces(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    run(R.withdraw_approvals(g["goal_id"], "re-planned", ["M1"]))
    assert A.get(row["id"])["state"] == A.DENIED


def test_a_deadline_closes_a_waiting_goal_and_withdraws_its_request(world):
    g = _goal(deadline=T0 + 86400, plan={"milestones": TWO_MS["milestones"][:1]})
    _propose(world, g)
    S.set_milestone(g["goal_id"], "M1", 1, S.WAITING_MS, expect=(S.READY,))
    run(R.reconcile_waits())
    assert S.get(g["goal_id"])["state"] == S.WAITING
    world.clock.t = T0 + 2 * 86400
    run(R._cycle())
    assert S.get(g["goal_id"])["state"] == S.PARTIAL
    assert A.pending() == []


def test_startup_denies_goal_requests_with_no_live_goal(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    conn = db.connect()
    with conn:                                           # the goal ended without withdrawing
        conn.execute("UPDATE goals SET state='cancelled' WHERE goal_id=?", (g["goal_id"],))
    R.release_lock()
    R.startup()
    assert A.get(row["id"])["state"] == A.DENIED


def test_a_restore_pauses_every_goal_and_denies_its_requests(world):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    marker = db.db_path().with_name(db.db_path().name + R.RESTORE_MARKER_SUFFIX)
    marker.write_text("restored")
    R.release_lock()
    R.startup()
    assert S.get(g["goal_id"])["state"] == S.PAUSED
    assert A.get(row["id"])["state"] == A.DENIED
    assert not marker.exists()


def test_an_approval_shows_a_guessed_recipient(world):
    g = _goal()
    _propose(world, g, args=_email(to="anna@gmail.com"))
    [row] = A.pending()
    assert "anna@gmail.com" in row["why"] and "Check first" in row["why"]


# ------------------------------------------------------------------ watches

def _goal_watch(world, g):
    s = _session(g)
    out = run(s.goal_finish("wait_for_event", "until a listing appears", op_key="ev",
                            condition={"method": "web", "query": "Oberkampf flat",
                                       "predicate": "a 2-room flat under 1400 is listed"}))
    assert "recorded" in out
    [w] = S.waits(g["goal_id"])
    return WS.get(w["payload"]["watch_id"])


def test_the_same_event_key_creates_one_watch(world):
    g = _goal()
    a = _goal_watch(world, g)
    run(_session(g).goal_finish("wait_for_event", "again", op_key="ev",
                                condition={"method": "web", "query": "Oberkampf flat",
                                           "predicate": "a 2-room flat under 1400 is listed"}))
    assert len(S.effects(g["goal_id"], "watch")) == 1 and WS.get(a["watch_id"]) is not None


def test_a_goal_watch_fires_for_the_goal_and_sends_no_dm(world, monkeypatch):
    g = _goal()
    w = _goal_watch(world, g)
    dms = []

    async def dm(text):
        dms.append(text)
        return True
    W.set_notifier(dm)
    monkeypatch.setattr(W, "_gather", lambda watch: _done((True, "Listed: 2-room flat, 1350 a month")))
    monkeypatch.setattr(W, "_judge", lambda watch, ev: _done(json.dumps(
        {"met": True, "quote": "2-room flat, 1350 a month", "confidence": 0.9, "summary": "listed"})))
    try:
        assert run(W.evaluate(WS.get(w["watch_id"]))) == "fired"
    finally:
        W.set_notifier(None)
    assert dms == []
    assert any(c["purpose"] == "watch_judge" for c in S.charges(g["goal_id"]))
    run(R.reconcile_waits())
    [r] = S.resolved_waits(g["goal_id"], "M1")
    assert r["outcome"]["watch"] == "fired"


def test_a_crash_while_a_goal_watch_fires_is_not_reported_as_a_dm(world):
    g = _goal()
    w = _goal_watch(world, g)
    assert WS.begin_alert(w["watch_id"], "listed", next_state=WS.FIRED, next_check_at=T0, verdict="met")
    WS.reconcile_inflight()                              # the restart
    assert [p for p in WS.pop_parked() if p["watch_id"] == w["watch_id"]] == []
    run(R.reconcile_waits())
    assert S.resolved_waits(g["goal_id"], "M1")[0]["outcome"]["watch"] == "fired"


def test_an_ordinary_watch_still_dms(world, monkeypatch):
    wid = WS.add(subject="NVDA", condition="closes below 180", method="web", query="NVDA",
                 every_s=3600, expires_at=T0 + 86400)
    dms = []

    async def dm(text):
        dms.append(text)
        return True
    W.set_notifier(dm)
    monkeypatch.setattr(W, "_gather", lambda watch: _done((True, "NVDA closed at 175.10")))
    monkeypatch.setattr(W, "_judge", lambda watch, ev: _done(json.dumps(
        {"met": True, "quote": "NVDA closed at 175.10", "confidence": 0.9, "summary": "below"})))
    try:
        assert run(W.evaluate(WS.get(wid))) == "fired"
    finally:
        W.set_notifier(None)
    assert len(dms) == 1


# ------------------------------------------------------------------ delivery

def test_a_confirmed_goal_message_is_never_sent_again_and_an_uncertain_part_is_not_resent(world):
    from backend.brain import discord_bridge as D
    calls = []

    async def ok(body):
        calls.append(body)
        return NS(id=len(calls), channel=None)
    text = "Goal result " + ("x" * 2500)                 # two parts
    assert run(D._send_durable(ok, text, intent_id="goal:g1:end", route="owner"))
    assert run(D._send_durable(ok, text, intent_id="goal:g1:end", route="owner"))
    assert len(calls) == 2                               # not sent twice

    async def dies(body):
        calls.append(body)
        if len(calls) == 4:
            raise ConnectionError("accepted, but the answer was lost")
        return NS(id=len(calls), channel=None)
    assert not run(D._send_durable(dies, text, intent_id="goal:g1:question:w1", route="owner"))
    assert not run(D._send_durable(dies, text, intent_id="goal:g1:question:w1", route="owner"))
    assert len(calls) == 4                               # the uncertain part is never resent


def test_the_brief_lists_only_confirmed_sends(world):
    g = _goal()
    run(R.deliver(g["goal_id"], "question:w1", "Max rent?"))
    from backend.storage import delivery
    assert S.sent(g["goal_id"]) == []                     # recorded, but no receipt yet
    delivery.prepare("goal:%s:question:w1" % g["goal_id"], "owner", "Max rent?", ["Max rent?"])
    delivery.claim_part("goal:%s:question:w1" % g["goal_id"], 0)
    delivery.mark_sent("goal:%s:question:w1" % g["goal_id"], 0, "m1")
    assert [s["purpose"] for s in S.sent(g["goal_id"])] == ["question:w1"]


# ------------------------------------------------------------------ two weeks

def test_two_simulated_weeks(world, monkeypatch):
    """A flat hunt over two weeks: a plan, a research job, a question answered on day 2, an
    email proposed and tapped, a timer, a goal watch that fires on day 9, and the end."""
    calls = NS(sessions=0)

    def script(kw, st):
        first = kw["messages"][0]["content"]
        if "You plan one goal" in first:
            return _text(json.dumps(SIM_PLAN))
        if "Check one finished goal" in first:
            return _text(json.dumps({"met": ["C1"], "unmet": []}))
        brief = kw["messages"][1]["content"]
        mid = re.search(r"THIS SESSION'S MILESTONE: (M\d)", brief).group(1)
        step = sum(1 for m in kw["messages"] if m.get("role") == "assistant")
        return _flat_session(mid, brief, step, world)

    monkeypatch.setattr(R, "_client", lambda: Fake(script))
    g = S.create(title="Flat hunt", words="find me a flat and write to the agency",
                 conversation_id="discord:dm", criteria=["a shortlist of flats"], state=S.ACTIVE,
                 budget_microusd=budget.micro(1.0), deadline_at=T0 + 14 * 86400, creation_key="sim")
    gid = g["goal_id"]
    for day in range(15):
        world.clock.t = T0 + day * 86400
        run(_day())
        if day == 1:                                     # he answers the question
            [q] = [w for w in S.waits(gid) if w["kind"] == "question"]
            world.say("max 1400 a month")
            run(GT._answer(goal_id=gid, wait_id=q["wait_id"], text="1400"))
        if day == 2:                                     # the job finishes
            for jid in world.launches:
                jobs.finish(jid, "done", {"kind": "web_research", "result": "Agence Bastille lists 3 flats",
                                          "goal_id": gid})
        if day == 3:                                     # he taps Approve
            for row in A.pending():
                run(AF.approve(row["id"], "button"))
        if day == 9:                                     # the listing appears
            for w in S.waits(gid):
                if w["kind"] == "watch":
                    conn = db.connect()
                    with conn:
                        conn.execute("UPDATE watches SET fire_count=1, state='fired' WHERE watch_id=?",
                                     (w["payload"]["watch_id"],))
        if S.get(gid)["state"] in S.ENDED:
            break
    g = S.get(gid)
    assert g["state"] == S.DONE, (g["state"], g["outcome"])
    assert len(world.launches) == 1 and len(world.emails) == 1
    assert len([k for k, _ in world.sent if k.endswith("end:done")]) == 1
    assert all(w["state"] != S.OPEN for w in S.waits(gid, state=None))
    kinds = {w["kind"] for w in S.waits(gid, state=None)}
    assert kinds >= {"question", "job", "approval", "watch", "time"}


SIM_PLAN = {"milestones": [
    {"id": "M2", "title": "Compare the listings", "expected": "compare.md",
     "checks": [], "criteria": ["C1"], "depends": []},
    {"id": "M1", "title": "Ask the agency and write the shortlist",
     "expected": "result.md: the shortlist and the agency's answer",
     "checks": [], "criteria": ["C1"], "depends": []}]}


def _flat_session(mid, brief, step, world):
    if mid == "M2":
        if "Came back (job" not in brief:
            if step == 0:
                return _calls(("goal_job", {"action": "start", "op_key": "agencies",
                                            "task": "Paris 11e flats under 1400"}))
            jid = world.launches[0]
            return _calls(("goal_finish", {"ending": "wait_for_job", "summary": "research running",
                                           "op_key": "wj", "job_id": jid}))
        if "Came back (watch" not in brief:
            if step == 0:
                return _calls(("goal_file", {"op": "write", "name": "compare.md",
                                             "content": "Three flats from Agence Bastille.",
                                             "expected_revision": None, "op_key": "c1"}))
            return _calls(("goal_finish", {"ending": "wait_for_event", "summary": "waiting for a listing",
                                           "op_key": "ev", "condition": {
                                               "method": "web", "query": "Oberkampf 2 rooms",
                                               "predicate": "a 2-room flat under 1400 is listed"}}))
        return _calls(("goal_finish", {"ending": "milestone_done", "summary": "compared",
                                       "artifact_refs": [{"name": "compare.md", "revision": 1}]}))
    if "Came back (question" not in brief:
        return _calls(("goal_finish", {"ending": "ask", "summary": "need his budget", "op_key": "q",
                                       "question": "What is your maximum monthly rent?",
                                       "blocked_milestone_ids": ["M1"]}))
    if "Came back (approval" not in brief:
        if step == 0:
            return _calls(("goal_action", {"op_key": "mail", "target_tool": "gmail_action",
                                           "arguments": _email(), "explanation": "ask about the flat"}))
        return _calls(("goal_finish", {"ending": "continue", "summary": "proposed the email"}))
    if "Came back (time" not in brief:
        return _calls(("goal_finish", {"ending": "wait_until", "summary": "agency answers in 2 days",
                                       "op_key": "t", "when": _iso(world.clock.t + 2 * 86400)}))
    if step == 0:
        return _calls(("goal_file", {"op": "write", "name": "result.md",
                                     "content": "Shortlist: three flats from Agence Bastille; the agency was emailed.",
                                     "expected_revision": None, "op_key": "r"}))
    return _calls(("goal_finish", {"ending": "milestone_done", "summary": "done",
                                   "artifact_refs": [{"name": "result.md", "revision": 1}]}))


# ------------------------------------------------------------------ helpers

def _iso(ts):
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.fromtimestamp(ts, ZoneInfo("Europe/Paris")).isoformat()


async def _day(cycles=4):
    """One simulated day: the loop's cycles, each session run to its end (one event loop)."""
    for _ in range(cycles):
        await R._cycle()
        if R._task is not None:
            await R._task
            R._task = None


def _done(value):
    async def coro():
        return value
    return coro()


# ------------------------------------------------------------------ guards found missing

def test_a_replan_withdraws_before_it_replaces_the_milestones(world, monkeypatch):
    g = _goal()
    _propose(world, g)
    [row] = A.pending()
    plan = {"milestones": [dict(TWO_MS["milestones"][1], expected="result.md, the comparison")]}
    monkeypatch.setattr(R, "_client", lambda: Fake(lambda kw, st: _text(json.dumps(plan))))
    assert run(R.plan_goal(S.get(g["goal_id"]), reason="stuck", blocker="the agency is gone"))
    assert A.get(row["id"])["state"] == A.DENIED
    run(AF.approve(row["id"], "button"))
    assert world.emails == []


def test_a_wait_from_a_replaced_generation_is_cancelled_not_resolved(world, monkeypatch):
    g = _goal()
    run(_session(g).goal_finish("wait_until", "x", op_key="t", when="2026-10-15T09:00:00+02:00"))
    plan = {"milestones": [dict(m, expected=m["expected"] + " (revised)") for m in TWO_MS["milestones"]]}
    S.save_plan(g["goal_id"], plan["milestones"], actor="jarvis", reason="re-plan")
    world.clock.t = T0 + 2 * 86400
    assert run(R.reconcile_waits()) == 0
    assert S.waits(g["goal_id"], state=S.W_CANCELLED)[0]["kind"] == "time"


def test_a_milestone_left_waiting_with_nothing_to_wait_for_is_ready_again(world):
    g = _goal()
    S.set_milestone(g["goal_id"], "M2", 1, S.WAITING_MS, expect=(S.READY,))
    run(R.reconcile_waits())
    assert _ms(g, "M2")["state"] == S.READY


def test_resume_is_refused_when_the_budget_is_spent(world):
    g = _goal()
    world.say("pause it")
    run(GT._pause(goal_id=g["goal_id"]))
    charge, _ = budget.reserve(g["goal_id"], "session", budget.micro(0.5))
    budget.settle(charge, budget.micro(1.0))
    world.say("resume it")
    assert "raise its budget" in run(GT._resume(goal_id=g["goal_id"]))
    assert S.get(g["goal_id"])["state"] == S.PAUSED


def test_a_question_holds_only_the_milestones_it_names(world):
    g = _goal()
    run(_session(g, "M1").goal_finish("ask", "x", op_key="q", question="Max rent?",
                                      blocked_milestone_ids=["M1"]))
    S.set_milestone(g["goal_id"], "M2", 1, S.WAITING_MS, expect=(S.READY,))
    run(R.reconcile_waits())
    assert _ms(g, "M2")["state"] == S.READY              # the question does not hold M2


# ------------------------------------------------------------------ found live (run 2)

def test_a_question_always_holds_the_milestone_that_asked(world):
    g = _goal()
    run(_session(g, "M1").goal_finish("ask", "x", op_key="q", question="Max rent?",
                                      blocked_milestone_ids=["M2"]))
    [w] = S.waits(g["goal_id"])
    assert sorted(w["payload"]["blocked"]) == ["M1", "M2"]


def test_a_job_started_without_a_wait_is_waited_for_and_listed(world, monkeypatch):
    g = _goal(plan={"milestones": TWO_MS["milestones"][:1]})

    def script(kw, st):
        st["n"] = st.get("n", 0) + 1
        if st["n"] == 1:
            return _calls(("goal_job", {"action": "start", "op_key": "listings",
                                        "task": "2-room flats on rue Oberkampf under 1400"}))
        return _calls(("goal_finish", {"ending": "continue", "summary": "research started"}))
    monkeypatch.setattr(R, "_client", lambda: Fake(script))

    async def go():
        await R._cycle()
        if R._task is not None:
            await R._task
            R._task = None
    run(go())
    [w] = S.waits(g["goal_id"])
    assert w["kind"] == "job" and w["payload"]["job_id"] == world.launches[0]
    assert _ms(g, "M1")["state"] == S.WAITING_MS
    ms = _ms(g, "M1")
    assert world.launches[0] in R.brief(S.get(g["goal_id"]), ms, "s")


def test_goal_show_lists_unconfirmed_actions_and_unsettled_charges(world):
    g = _goal()
    S.effect_begin(g["goal_id"], "M1", 1, kind="job", logical_key="M1:1:lost", request={"x": 1})
    budget.reserve(g["goal_id"], "session", budget.micro(0.01))
    text = GT.show_text(g["goal_id"])
    assert "not confirmed: job" in text and "check before it is repeated" in text
    assert "Charges not yet settled: session" in text


def test_a_session_stopped_by_pause_is_redone_after_resume(world, monkeypatch):
    """D9: pause cancels the running session (its charge held), the milestone goes back to
    ready, and after resume the next cycle runs it again."""
    g = _goal(plan={"milestones": TWO_MS["milestones"][:1]})
    gate = {}

    async def slow_create(**kw):
        gate.setdefault("n", 0)
        gate["n"] += 1
        if gate["n"] == 1:
            await asyncio.sleep(3600)                  # cut off by the pause
        return _calls(("goal_finish", {"ending": "continue", "summary": "redone"}))
    monkeypatch.setattr(R, "_client", lambda: NS(chat=NS(completions=NS(create=slow_create))))

    async def go():
        await R._cycle()
        await asyncio.sleep(0.05)
        out = await GT._pause(goal_id=g["goal_id"])
        assert "paused" in out
        R._task = None
        assert _ms(g, "M1")["state"] == S.READY
        await GT._resume(goal_id=g["goal_id"])
        await R._cycle()
        if R._task is not None:
            await R._task
            R._task = None
    world.say("pause the flat hunt")                 # set before the loop copies the context
    run(go())
    states = [r["state"] for r in db.connect().execute(
        "SELECT state FROM goal_sessions WHERE goal_id=? ORDER BY started_at", (g["goal_id"],))]
    assert states[0] == S.INTERRUPTED and len(states) == 2
    assert [c["state"] for c in S.charges(g["goal_id"])][0] == "held"
