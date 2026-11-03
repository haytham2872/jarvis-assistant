"""Phone goals against isolated canonical storage; workers and transcription are faked."""
import asyncio
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.brain import goal_budget as money, goal_runner as runner, goals
from backend.mobile import goals_view as V, routes
from backend.mobile.devserver import create_app
from backend.storage import goals as S, operations
from backend.tools import policy


@pytest.fixture(autouse=True)
def available(monkeypatch):
    monkeypatch.setattr(goals, "_unavailable", lambda: None)
    for name in ("touched", "cancel_running", "reconcile_waits"):
        monkeypatch.setattr(runner, name, AsyncMock())
    for name in ("withdraw_approvals", "repropose_withdrawn"):
        monkeypatch.setattr(runner, name, AsyncMock(return_value=0))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", "test-token")
    with TestClient(create_app()) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


def goal(state=S.ACTIVE, *, title="Compare funds"):
    g = S.create(title=title, words="Compare these funds", conversation_id="mobile", criteria=["fees"],
                 state=S.ACTIVE, budget_microusd=1_000_000, deadline_at=time.time() + 86400,
                 creation_key=uuid.uuid4().hex)
    assert g
    assert S.save_plan(g["goal_id"], [{"id": "M1", "title": "Compare fees", "expected": "result.md",
                                      "depends": [], "checks": [], "criteria": ["C1"]}], actor="fixture", reason="test")
    if state != S.ACTIVE:
        assert S.transition(g["goal_id"], state, expect=(S.ACTIVE,))
    return S.get(g["goal_id"])


def question(g, text="Which funds?"):
    row = S.add_wait(g["goal_id"], "M1", 1, kind="question", key=uuid.uuid4().hex, payload={"question": text})
    assert "wait_id" in row
    return row


def test_list_orders_live_states_and_limits_ended(client):
    for n in range(23):
        goal(S.DONE, title=f"Ended {n}")
    paused, waiting, active = goal(S.PAUSED), goal(S.WAITING), goal()
    proposal = goal(S.PROPOSED)
    question(waiting)
    view = client.get("/mobile/v1/goals").json()
    assert [g["state"] for g in view["goals"][:3]] == [S.ACTIVE, S.WAITING, S.PAUSED]
    assert len(view["goals"]) == 23
    assert view["proposals"][0]["goal_id"] == proposal["goal_id"]
    hub = client.get("/mobile/v1/hub")
    assert hub.json()["goals"] == {"active": 2, "questions": 1, "proposals": 1, "enabled": True}
    assert client.get("/mobile/v1/hub", headers={"If-None-Match": hub.headers["etag"]}).status_code == 304


def test_detail_reads_running_session_milestones_charges_files_and_cited_evidence(client):
    g = goal()
    gid = g["goal_id"]
    session = S.claim_session(gid, "M1", 1, "goal-run")
    S.mark_running(session)
    ev = S.save_evidence(gid, "M1", 1, session_id=session, run_id="goal-run", tool="web_browse", args={},
                         body="The fee is 0.07%.", metadata={"title": "Factsheet", "url": "https://example.com/facts"})
    S.save_file(gid, "M1", 1, name="result.md", content=f"Fee 0.07% [{ev}].", expected_revision=None, op_key="report")
    w = question(g)
    for n in range(10):
        operations.record_operation(tool=f"read-{n}", outcome="ok", started_at=time.time(), run_id="goal-run")
    charge, reason = money.reserve(gid, "session", 200_000)
    assert charge and not reason
    view = client.get(f"/mobile/v1/goals/{gid}").json()
    assert view["now"] == "Working on M1: Compare fees"
    assert view["milestones"][0]["id"] == "M1" and view["milestones"][0]["state"] == "working"
    assert view["session"]["state"] == "running" and len(view["session"]["steps"]) == 8
    assert view["session"]["steps"][0] == "read-2 · ok"
    assert view["questions"] == [{"wait_id": w["wait_id"], "text": "Which funds?"}]
    assert view["charges"] == {"spent_usd": 0, "reserved_usd": 0.2}
    assert view["spent_usd"] == 0.2 and view["files"][0]["revision"] == 1
    assert view["evidence"][0]["id"] == ev and view["evidence"][0]["url"] == "https://example.com/facts"
    assert client.get(f"/mobile/v1/goals/{gid}/evidence/{ev}").json()["excerpt"] == "The fee is 0.07%."


def test_ended_session_metadata_and_completed_outputs_are_still_readable():
    g = goal()
    gid = g["goal_id"]
    sid = S.claim_session(gid, "M1", 1, "ended-run")
    assert S.complete_milestone(gid, "M1", 1, sid, {"summary": "Found the fee", "artifact_refs": [{"name": "result.md", "revision": 1}]})
    assert S.end_session(sid, S.COMPLETE, ending={}, fingerprint="x", release=False)
    view = V.detail(gid)
    assert view["session"]["state"] == S.COMPLETE and view["session"]["started_at"]
    assert view["milestones"][0]["summary"] == "Found the fee" and view["milestones"][0]["outputs"][0]["name"] == "result.md"


def test_file_limit_is_bytes_and_missing_or_foreign_records_are_404(client):
    g = goal()
    gid = g["goal_id"]
    S.save_file(gid, "M1", 1, name="result.md", content="€" * 60_000, expected_revision=None, op_key="long")
    f = client.get(f"/mobile/v1/goals/{gid}/files/result.md")
    assert f.status_code == 200 and f.json()["truncated"]
    assert len(f.json()["text"].encode("utf-8")) <= 100_000
    for path in ("missing", f"{gid}/files/missing.md", f"{gid}/evidence/evmissing"):
        assert client.get(f"/mobile/v1/goals/{path}").status_code == 404
    assert client.post("/mobile/v1/goals/gmissing/control", json={"action": "cancel"}).status_code == 404


@pytest.mark.parametrize("action,start,end", [("pause", S.ACTIVE, S.PAUSED), ("resume", S.PAUSED, S.ACTIVE), ("cancel", S.ACTIVE, S.CANCELLED)])
def test_controls_reuse_approved_goal_tool_and_restore_authority(client, monkeypatch, action, start, end):
    g = goal(start)
    actual = goals.goal
    seen = []
    async def invoke(**kwargs):
        seen.append((policy.current_caller.get(), policy.call_quote.get(), kwargs))
        return await actual(**kwargs)
    monkeypatch.setattr(goals, "goal", invoke)
    r = client.post(f"/mobile/v1/goals/{g['goal_id']}/control", json={"action": action})
    assert r.status_code == 200 and S.get(g["goal_id"])["state"] == end
    assert "goal resume" not in r.json()["text"]
    assert seen[0][0] == policy.APPROVED and action in seen[0][1] and seen[0][2]["action"] == action
    assert policy.current_caller.get() != policy.APPROVED


def test_raise_budget_start_and_decline_proposals(client):
    g = goal(S.PAUSED)
    r = client.post(f"/mobile/v1/goals/{g['goal_id']}/budget", json={"usd": 2})
    assert r.status_code == 200 and r.json()["goal"]["budget_usd"] == 2 and r.json()["goal"]["state"] == S.ACTIVE
    p = goal(S.PROPOSED)
    assert client.post(f"/mobile/v1/goals/proposals/{p['goal_id']}/start").status_code == 200
    assert S.get(p["goal_id"])["state"] == S.ACTIVE
    declined = goal(S.PROPOSED)
    assert client.post(f"/mobile/v1/goals/proposals/{declined['goal_id']}/decline").status_code == 200
    assert S.get(declined["goal_id"])["state"] == S.CANCELLED
    assert client.post(f"/mobile/v1/goals/proposals/{g['goal_id']}/decline").status_code == 409


def test_answer_records_his_exact_words_once(client):
    g = goal()
    w = question(g)
    payload = {"wait_id": w["wait_id"], "text": "  CSPX and VUAA, please. "}
    first = client.post("/mobile/v1/goals/answer", json=payload)
    assert first.status_code == 200
    assert S.events(g["goal_id"], "answer")[0]["payload"]["answer"] == payload["text"]
    second = client.post("/mobile/v1/goals/answer", json=payload)
    assert second.status_code == 200 and "settled" in second.json()["text"]
    assert len(S.events(g["goal_id"], "answer")) == 1


def test_voice_only_transcribes_and_requires_explicit_send(client, monkeypatch):
    g = goal()
    w = question(g)
    transcribe = AsyncMock(return_value=("CSPX and VUAA", {}))
    monkeypatch.setattr(routes, "_transcribe_body", transcribe)
    response = client.post(f"/mobile/v1/goals/answer/voice?wait_id={w['wait_id']}", content=b"audio", headers={"Content-Type": "audio/mp4"})
    assert response.status_code == 200 and response.json()["transcript"] == "CSPX and VUAA"
    assert S.get_wait(w["wait_id"])["state"] == S.OPEN and S.events(g["goal_id"], "answer") == []
    S.resolve_wait(w["wait_id"], {})
    assert client.post(f"/mobile/v1/goals/answer/voice?wait_id={w['wait_id']}", content=b"audio").status_code == 409
    assert transcribe.await_count == 1


def test_disabled_goals_auth_errors_and_partial_source_failure(client, monkeypatch):
    g = goal()
    wait = question(g)
    transcribe = AsyncMock()
    monkeypatch.setattr(routes, "_transcribe_body", transcribe)
    monkeypatch.setattr(goals, "_unavailable", lambda: "Not configured")
    view = client.get("/mobile/v1/goals").json()
    assert view["enabled"] is False and not view["goals"] and not view["proposals"]
    assert client.post(f"/mobile/v1/goals/{g['goal_id']}/control", json={"action": "pause"}).status_code == 409
    assert client.post(f"/mobile/v1/goals/answer/voice?wait_id={wait['wait_id']}", content=b"audio").status_code == 409
    transcribe.assert_not_awaited()
    assert client.get("/mobile/v1/hub", headers={"Authorization": "wrong"}).status_code == 401
    monkeypatch.setattr(goals, "_unavailable", lambda: None)
    monkeypatch.setattr(S, "files", lambda *_: (_ for _ in ()).throw(RuntimeError("unreadable")))
    view = V.detail(g["goal_id"])
    assert view["files"] == [] and view["questions"] and "Files could not be read just now." in view["notes"]


@pytest.mark.parametrize("value", [True, 0, -1, 21, 10**300, "2", None])
def test_budget_refuses_invalid_numbers(client, value):
    g = goal()
    assert client.post(f"/mobile/v1/goals/{g['goal_id']}/budget", json={"usd": value}).status_code == 400


def test_goal_tool_refusal_is_409_and_policy_is_restored_even_on_error(client, monkeypatch):
    g = goal()
    monkeypatch.setattr(goals, "goal", AsyncMock(return_value="Error: not enough budget"))
    response = client.post(f"/mobile/v1/goals/{g['goal_id']}/control", json={"action": "resume"})
    assert response.status_code == 409 and response.json()["detail"] == "not enough budget"
    assert policy.current_caller.get() != policy.APPROVED


def test_all_goal_routes_authorize_before_reading_or_acting(client, monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(routes, "_goal_read", reader)
    client.headers["Authorization"] = "wrong"
    for path in ("/hub", "/goals", "/goals/g1", "/goals/g1/files/result.md", "/goals/g1/evidence/ev1"):
        assert client.get("/mobile/v1" + path).status_code == 401
    for path in ("/goals/g1/control", "/goals/g1/budget", "/goals/answer", "/goals/answer/voice", "/goals/answer/voice?wait_id=w1",
                 "/goals/proposals/g1/start", "/goals/proposals/g1/decline"):
        assert client.post("/mobile/v1" + path, content=b"not JSON").status_code == 401
    reader.assert_not_awaited()


def test_decline_cannot_cancel_an_offer_started_between_read_and_transition(client, monkeypatch):
    g = goal(S.PROPOSED)
    transition = S.transition
    def started(*args, **kwargs):
        assert transition(g["goal_id"], S.ACTIVE, expect=(S.PROPOSED,))
        return transition(*args, **kwargs)
    monkeypatch.setattr(S, "transition", started)
    response = client.post(f"/mobile/v1/goals/proposals/{g['goal_id']}/decline")
    assert response.status_code == 409
    assert S.get(g["goal_id"])["state"] == S.ACTIVE


@pytest.mark.parametrize("raises", [False, True])
def test_authority_restores_in_the_same_task_after_refusal_or_exception(monkeypatch, raises):
    from fastapi import HTTPException
    async def invoke(**_):
        assert policy.current_caller.get() == policy.APPROVED
        assert policy.call_quote.get() == "his exact words"
        if raises:
            raise RuntimeError("fixture failure")
        return "Error: fixture refusal"
    monkeypatch.setattr(goals, "goal", invoke)
    async def probe():
        before = (policy.current_caller.get(), policy.call_quote.get())
        with pytest.raises(RuntimeError if raises else HTTPException):
            await routes._goal_invoke("pause", "g1", "his exact words")
        assert (policy.current_caller.get(), policy.call_quote.get()) == before
    asyncio.run(probe())
