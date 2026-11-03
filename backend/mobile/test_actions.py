"""M4: approval requests and background work on the phone.

The gate: a double tap executes at most once; a decision made on Discord shows on the phone
and the reverse; work that finished while the app was closed is there on reopen. The tool is
a fake (no email is sent); storage is the per-test database."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile.test_conversation import AUTH, _isolation, _main, run  # noqa: F401  (fixture)

ARGS = {"action": "send_email", "to": "x@example.com", "subject": "Hi", "body": "Body"}


@pytest.fixture
def mail(monkeypatch):
    from backend.brain import engine
    sent = []

    async def gmail(**kw):
        await asyncio.sleep(0.01)
        sent.append(kw)
        return "Email sent to x@example.com."
    monkeypatch.setitem(engine.TOOL_DISPATCH, "gmail_action", gmail)
    return sent


def _client(monkeypatch):
    M, _ = _main(monkeypatch)                      # configures the phone, which starts listening
    from backend.mobile import routes
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), M


def _request(context="background"):
    from backend.tools import approval_flow
    return run(approval_flow.request(context=context, tool="gmail_action", args=ARGS,
                                     risk="outward", why="a background job wants to reply"))


def _events():
    from backend.mobile import events
    return [e for e in events.HUB.since(events.HUB.epoch, 0) or []]


# ------------------------------------------------------------------ approvals

def test_a_waiting_request_is_on_the_phone_and_in_the_snapshot(monkeypatch, mail):
    c, M = _client(monkeypatch)
    row = _request()
    pending = c.get("/mobile/v1/approvals", headers=AUTH).json()["pending"]
    assert [p["id"] for p in pending] == [row["id"]]
    assert "arguments" not in pending[0] and pending[0]["why"] == "a background job wants to reply"
    snap = c.get("/mobile/v1/snapshot", headers=AUTH).json()
    assert [a["id"] for a in snap["approvals"]] == [row["id"]]
    assert any(e["type"] == "approval" and e["approval"]["id"] == row["id"] for e in _events())


def test_a_double_tap_on_the_phone_sends_once(monkeypatch, mail):
    c, M = _client(monkeypatch)
    row = _request()
    first = c.post(f"/mobile/v1/approvals/{row['id']}", json={"decision": "approve"}, headers=AUTH).json()
    second = c.post(f"/mobile/v1/approvals/{row['id']}", json={"decision": "approve"}, headers=AUTH).json()
    assert mail == [ARGS]
    assert "Email sent" in first["text"] and first["approval"]["state"] == "executed"
    assert "already approved" in second["text"]
    assert c.get("/mobile/v1/approvals", headers=AUTH).json()["pending"] == []


def test_concurrent_taps_from_the_phone_and_discord_run_once(monkeypatch, mail):
    _client(monkeypatch)
    from backend.mobile import conversation
    from backend.tools import approval_flow
    row = _request()

    async def both():
        return await asyncio.gather(conversation.decide(row["id"], True),
                                    approval_flow.approve(row["id"], "discord button"))
    phone, discord = run(both())
    assert mail == [ARGS]
    assert sum("already approved" in t for t in (phone["text"], discord)) == 1


def test_a_decision_on_discord_shows_on_the_phone(monkeypatch, mail):
    _client(monkeypatch)
    from backend.tools import approval_flow
    row = _request()
    run(approval_flow.deny(row["id"], "discord button"))
    states = [e["approval"]["state"] for e in _events() if e["type"] == "approval"]
    assert states == ["pending", "denied"]
    assert mail == []


def test_a_decision_on_the_phone_updates_the_discord_message(monkeypatch, mail):
    c, M = _client(monkeypatch)
    from backend.tools import approval_flow
    told = []

    async def resolution(row, text):
        told.append((row["id"], row["state"], text))
        return True
    monkeypatch.setattr(approval_flow, "_resolution_notifier", resolution)
    row = _request()
    c.post(f"/mobile/v1/approvals/{row['id']}", json={"decision": "deny"}, headers=AUTH)
    assert told == [(row["id"], "denied", f"Denied #{row['id']}. Nothing was done.")]


def test_a_bad_decision_is_refused(monkeypatch, mail):
    c, M = _client(monkeypatch)
    row = _request()
    assert c.post(f"/mobile/v1/approvals/{row['id']}", json={"decision": "maybe"}, headers=AUTH).status_code == 400
    assert c.post(f"/mobile/v1/approvals/{row['id']}", json={"decision": "approve"}).status_code == 401
    assert mail == []


# ------------------------------------------------------------------ background work

def _phone_turn_context():
    from backend.context import origin
    return origin.current_origin.set(origin.Origin(user_text="look into it", channel="mobile"))


def test_work_started_from_the_phone_is_shown_there_and_messaged_on_discord(monkeypatch):
    _client(monkeypatch)
    from backend.brain import background
    from backend.context import origin
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)   # nothing actually runs
    token = _phone_turn_context()
    try:
        reply = run(background.run_in_background("check the notary's email"))
    finally:
        origin.current_origin.reset(token)
    assert "in the app and on Discord" in reply
    t = max(background._tasks.values(), key=lambda x: x.id)
    assert t.origin == "mobile" and t.notify
    jobs = [e["job"] for e in _events() if e["type"] == "job"]
    assert jobs and jobs[-1]["description"] == "check the notary's email" and "brief" not in jobs[-1]


def test_work_finished_while_the_app_was_closed_is_there_on_reopen(monkeypatch):
    c, M = _client(monkeypatch)
    from backend.brain import background
    from backend.context import origin
    from backend.storage import jobs
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)
    token = _phone_turn_context()
    try:
        run(background.run_in_background("summarise the lease"))
    finally:
        origin.current_origin.reset(token)
    t = max(background._tasks.values(), key=lambda x: x.id)
    t.status, t.result, t.summary, t.finished = "done", "The lease ends in June.", "Lease ends in June.", 1e10
    assert jobs.claim(t.job_id) is True                       # the worker claims, then finishes
    jobs.finish(t.job_id, "done", background._job_payload(t))
    background._tasks.clear()                                 # a restart: only the table is left
    snap = c.get("/mobile/v1/snapshot", headers=AUTH).json()
    job = next(j for j in snap["jobs"] if j["description"] == "summarise the lease")
    assert job["status"] == "done" and job["result"] == "The lease ends in June."
    assert c.get("/mobile/v1/jobs", headers=AUTH).json()["jobs"][0]["description"] == "summarise the lease"


def test_work_started_elsewhere_is_not_listed_on_the_phone(monkeypatch):
    c, M = _client(monkeypatch)
    from backend.brain import background
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)
    run(background.run_in_background("a desk task"))                   # no phone origin
    assert c.get("/mobile/v1/jobs", headers=AUTH).json()["jobs"] == []
    assert not [e for e in _events() if e["type"] == "job"]


def test_an_approval_on_discord_shows_running_then_done_on_the_phone(monkeypatch, mail):
    _client(monkeypatch)
    from backend.tools import approval_flow
    row = _request()
    run(approval_flow.approve(row["id"], "discord button"))
    states = [e["approval"]["state"] for e in _events() if e["type"] == "approval"]
    assert states == ["pending", "executing", "executed"]
    assert mail == [ARGS]


def test_a_phone_job_that_finishes_is_announced_to_the_phone(monkeypatch):
    _client(monkeypatch)
    from backend.brain import background
    from backend.context import origin
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)

    async def runner(task, **kw):
        return "The notary replied: Thursday at 2."
    monkeypatch.setattr(background, "_run_compat_loop", runner)
    token = _phone_turn_context()
    try:
        run(background.run_in_background("check the notary's email"))
    finally:
        origin.current_origin.reset(token)
    t = max(background._tasks.values(), key=lambda x: x.id)
    run(background._worker(t))
    last = [e["job"] for e in _events() if e["type"] == "job"][-1]
    assert last["status"] == "done" and last["result"] == "The notary replied: Thursday at 2."


def test_a_phone_job_hands_the_phone_its_conclusion_live_and_after_a_restart(monkeypatch):
    # Sam, 15 October: the whole result stays in the job card; only the conclusion is a
    # message. It reaches the phone live and from the durable job row (a reopened app).
    c, _ = _client(monkeypatch)
    from backend.brain import background, conclusion
    from backend.context import origin
    monkeypatch.setattr(background, "_schedule_queued", lambda: None)
    report = ("## Findings\n" + "The notary's office reopened after the holidays. " * 20
              + "\n\n## Bottom line\nThursday at 2 is confirmed.")

    async def runner(task, **kw):
        return report
    monkeypatch.setattr(background, "_run_compat_loop", runner)

    async def no_model(result):
        raise AssertionError("the report has its own conclusion; no model call")
    monkeypatch.setattr(conclusion, "_ask_model", no_model)
    token = _phone_turn_context()
    try:
        run(background.run_in_background("check the notary's email"))
    finally:
        origin.current_origin.reset(token)
    t = max(background._tasks.values(), key=lambda x: x.id)
    run(background._worker(t))
    live = [e["job"] for e in _events() if e["type"] == "job"][-1]
    assert live["conclusion"] == "Thursday at 2 is confirmed." and live["result"] == report
    stored = c.get("/mobile/v1/jobs", headers=AUTH).json()["jobs"][0]
    assert stored["conclusion"] == "Thursday at 2 is confirmed." and stored["result"] == report
