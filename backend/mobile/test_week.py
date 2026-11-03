"""The draggable phone week: real planner/storage, fake Google calendar, no live services."""
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.brain import planner as P, weekplan as W
from backend.brain.test_planday import NOW, WEEK, TUE, block, paris, planned, svc  # noqa: F401
from backend.mobile import routes, today, week
from backend.mobile.test_today import token  # noqa: F401
from backend.storage import plans
from backend.tools import calendar as cal, policy


@pytest.fixture(autouse=True)
def clean_cache():
    today.clear_calendar_cache()
    yield
    today.clear_calendar_cache()


def context():
    return {"view_start": WEEK, "days": 3, "plan_id": plans.applied(WEEK)["id"]}


def move(uid, **over):
    return week.action("move", {**context(), "day": "2026-10-06", "start": "19:00", **over}, uid, NOW)


def test_three_days_share_calendar_cache_but_keep_non_plan_owned_meetings(svc, token):
    planned(svc)
    svc.seed("Lunch", paris(2026, 10, 6, 12), paris(2026, 10, 6, 13),
             extendedProperties={"private": {cal.MARKER_KEY: "1"}})
    v = week.view(WEEK, now=NOW)
    assert len(v["days"]) == 3 and v["days"][1]["blocks"]
    assert [e["title"] for e in v["days"][1]["events"]] == ["Lunch"]
    assert v["days"][1]["events"][0]["id"]
    assert today._calendar(TUE)[0] == []
    before = len(svc.calls)
    week.view(WEEK, now=NOW)
    assert len(svc.calls) == before


def test_move_applies_and_undo_restores_only_its_version(svc, token):
    planned(svc)
    gym = block("gym")
    old = W._eff(gym)
    r = move(gym["uid"])
    row = next(r for r in plans.applied(WEEK)["blocks"] if r["uid"] == gym["uid"])
    assert W._eff(row)[0] == P.at(TUE, "19:00").timestamp()
    assert r["week"]["plan_id"] != gym["plan_id"]
    week.action("undo", context(), now=NOW)
    assert W._eff(next(r for r in plans.applied(WEEK)["blocks"] if r["uid"] == gym["uid"])) == old


def test_untracked_old_plan_event_is_not_lost_from_the_calendar(svc, token):
    planned(svc)
    svc.seed("Old block (untracked)", paris(2026, 10, 6, 21), paris(2026, 10, 6, 22),
             extendedProperties={"private": {cal.MARKER_KEY: "1", cal.PLAN_KEY: WEEK, cal.BLOCK_KEY: "orphan"}})
    assert [e["title"] for e in week.view(WEEK, now=NOW)["days"][1]["events"]] == ["Old block (untracked)"]


def test_read_window_crossing_sunday_uses_each_days_applied_plan(svc, token):
    planned(svc)
    W.run(W.Ask(action="propose", week="2026-10-12", rules={"blocks": [{"kind": "reading", "count": 1, "minutes": 30}]}), NOW)
    W.run(W.Ask(action="apply", week="2026-10-12"), NOW)
    v = week.view("2026-10-11", 3, NOW)
    assert [d["date"] for d in v["days"]] == ["2026-10-11", "2026-10-12", "2026-10-13"]
    assert [b["kind"] for b in v["days"][1]["blocks"]] == ["reading"]


@pytest.mark.parametrize("over,reason", [({"start": "10:00"}, "overlap"),
    ({"start": "19:07"}, "quarter"), ({"day": "2026-10-13"}, "selected week"),
    ({"plan_id": -1}, "changed")])
def test_refused_move_preserves_event(svc, token, over, reason):
    planned(svc)
    gym = block("gym")
    before = svc.live()
    with pytest.raises((week.Refused, ValueError), match=reason):
        move(gym["uid"], **over)
    assert svc.live() == before


def test_work_cannot_escape_its_hours(svc, token):
    planned(svc)
    with pytest.raises(week.Refused, match="outside"):
        move(block("work")["uid"], start="20:00")


def test_personal_event_cannot_be_claimed_by_a_block_uid_or_an_undo(svc, token):
    planned(svc)
    gym = block("gym")
    svc.items[gym["event_id"]]["extendedProperties"] = {}
    before = svc.live()
    with pytest.raises(week.Refused, match="no longer"):
        move(gym["uid"])
    with pytest.raises(week.Refused, match="no longer"):
        week.action("undo", context(), now=NOW)
    assert svc.live() == before


def test_calendar_write_failure_returns_the_actual_old_position_and_reason(svc, token):
    planned(svc)
    gym = block("gym")
    svc.fail("patch", RuntimeError("calendar offline"))
    r = move(gym["uid"])
    shown = next(b for d in r["week"]["days"] for b in d["blocks"] if b["uid"] == gym["uid"])
    assert shown["start"] == W._eff(gym)[0]
    assert "calendar offline" in r["warning"] and shown["status"] == plans.FAILED


def test_outcomes_and_unreadable_calendar_leave_the_rest_of_the_week(svc, token, monkeypatch):
    planned(svc)
    gym = block("gym")
    plans.record_outcome(gym["uid"], plans.applied(WEEK)["id"], "done", "owner", now=NOW.timestamp())
    monkeypatch.setattr(cal, "TOKEN_PATH", token.parent / "missing.json")
    v = week.view(WEEK, now=NOW)
    assert v["days"][1]["counts"]["done"] == 1
    assert v["days"][1]["blocks"] and "not connected" in v["days"][1]["events_note"]


def test_add_is_one_slot_preserves_standing_rules_and_other_blocks_then_undo(svc, token):
    planned(svc)
    old = plans.applied(WEEK)
    r = week.action("add", {**context(), "day": "2026-10-10", "start": "18:00", "kind": "gym", "minutes": 45}, now=NOW)
    new = plans.applied(WEEK)
    assert len(new["blocks"]) == len(old["blocks"]) + 1
    assert [W._carry(b) for b in new["blocks"] if b["uid"] in {b["uid"] for b in old["blocks"]}] == [W._carry(b) for b in old["blocks"]]
    assert new["rules"]["work"] == old["rules"]["work"]
    assert new["rules"]["blocks"][0]["not_before"] == old["rules"]["blocks"][0]["not_before"]
    assert new["rules"]["blocks"][0]["count"] == old["rules"]["blocks"][0]["count"] + 1
    added = next(b for b in new["blocks"] if b["uid"] not in {b["uid"] for b in old["blocks"]})
    assert W._eff(added) == (P.at(date(2026, 10, 10), "18:00").timestamp(), P.at(date(2026, 10, 10), "18:45").timestamp())
    assert not r["warning"]
    week.action("undo", context(), now=NOW)
    assert len(plans.applied(WEEK)["blocks"]) == len(old["blocks"])


def test_proposal_is_ghosted_pinned_and_blocks_edits_until_applied(svc, token):
    planned(svc)
    gym = block("gym")
    assert W.run(W.Ask(action="add", rules={"blocks": [{"kind": "reading", "count": 1, "minutes": 30}]}), NOW).startswith("Added")
    v = week.view(WEEK, now=NOW)
    assert v["draft"]["blocks"] and v["draft"]["plan_id"] != v["plan_id"]
    with pytest.raises(week.Refused, match="proposal"):
        move(gym["uid"])
    with pytest.raises(week.Refused, match="changed"):
        week.action("apply", {**context(), "week": WEEK, "plan_id": -1}, now=NOW)
    r = week.action("apply", {**context(), "week": WEEK, "plan_id": v["draft"]["plan_id"]}, now=NOW)
    assert r["week"]["draft"] is None


def test_stale_undo_does_not_reverse_a_discord_change(svc, token):
    planned(svc)
    move(block("gym")["uid"])
    expected = context()
    move(block("gym")["uid"], start="18:45")
    with pytest.raises(week.Refused, match="changed"):
        week.action("undo", expected, now=NOW)


def test_authority_and_cache_are_restored_even_on_refusal(svc, token, monkeypatch):
    planned(svc)
    today._calendar(TUE)
    seen = []
    original = W.run
    def run(ask, now):
        seen.append((policy.current_caller.get(), policy.call_quote.get()))
        return original(ask, now)
    monkeypatch.setattr(W, "run", run)
    caller, quote = policy.current_caller.get(), policy.call_quote.get()
    move(block("gym")["uid"])
    assert seen and all(c == policy.APPROVED and "phone:" in q for c, q in seen)
    assert (policy.current_caller.get(), policy.call_quote.get()) == (caller, quote)
    assert not today._calendar_cache
    with pytest.raises(week.Refused):
        move(block("gym")["uid"], start="10:00")
    assert (policy.current_caller.get(), policy.call_quote.get()) == (caller, quote)


def test_approved_caller_is_reset_in_the_direct_calling_context(svc, token):
    planned(svc)
    c = policy.current_caller.set("unattended")
    q = policy.call_quote.set("outside this tap")
    try:
        move(block("gym")["uid"])
        assert policy.current_caller.get() == "unattended"
        assert policy.call_quote.get() == "outside this tap"
    finally:
        policy.call_quote.reset(q)
        policy.current_caller.reset(c)


def test_routes_auth_conditional_and_refusal(monkeypatch, svc, token):
    monkeypatch.setenv("MOBILE_TOKEN", "week-token")
    monkeypatch.setattr(week, "action", lambda *a: (_ for _ in ()).throw(week.Refused("Occupied.")))
    app = FastAPI(); app.include_router(routes.router)
    client = TestClient(app)
    paths = [("get", "/mobile/v1/week?start=" + WEEK), ("post", "/mobile/v1/week/blocks/x/move"),
             ("post", "/mobile/v1/week/blocks"), ("post", "/mobile/v1/week/undo"), ("post", "/mobile/v1/week/apply")]
    for method, path in paths:
        assert getattr(client, method)(path).status_code == 401
    assert client.get("/mobile/v1/week?days=bad").status_code == 401
    auth = {"Authorization": "Bearer week-token"}
    r = client.get(paths[0][1], headers=auth)
    assert r.status_code == 200
    assert client.get(paths[0][1], headers={**auth, "If-None-Match": r.headers["etag"]}).status_code == 304
    r = client.post(paths[1][1], headers=auth, json={})
    assert r.status_code == 409 and r.json()["detail"] == "Occupied."
    assert client.get("/mobile/v1/week?days=bad", headers=auth).status_code == 400
