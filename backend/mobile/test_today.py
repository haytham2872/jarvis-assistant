"""The phone's Today tab (slice 2): the day view, and a tap recording one block's outcome the way
a check-in reply does. Against the calendar test double, with the planday test's standard week
(work weekdays 09:00-17:00, gym Mon/Tue/Thu/Fri at 17:30)."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.brain import planday as D
from backend.brain import weekplan as W
from backend.brain.test_planday import TUE, block, paris, planned, send_checkin, svc  # noqa: F401 (fixture)
from backend.mobile import today as T
from backend.storage import checkins, plans
from backend.tools import calendar as cal

TUE_1830 = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)      # Tuesday 18:30 Paris: gym started
TUE_1000 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)        # Tuesday 10:00 Paris: gym ahead
WED_0200 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)        # Wednesday 02:00 Paris
WED_0500 = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)        # Wednesday 05:00 Paris


@pytest.fixture
def token(monkeypatch, tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{}")
    monkeypatch.setattr(cal, "TOKEN_PATH", path)
    return path


@pytest.fixture
def agenda(monkeypatch):
    from backend.brain import chronos
    items = {"reminders": [], "tasks": []}
    monkeypatch.setattr(chronos, "agenda", lambda: {**items, "night": [], "done": [], "brief": {}, "parked": 0,
                                                     "night_results": 0})
    return items


def test_the_day_lists_the_plan_with_titles_times_and_whether_each_has_started(svc, token, agenda):
    planned(svc)
    v = T.view(TUE_1830)
    assert v["date"] == "2026-10-06" and v["label"] == "Tuesday 6 Oct"
    kinds = [(b["kind"], b["title"], b["started"]) for b in v["blocks"]]
    assert ("gym", "Gym", True) in kinds
    work = [b for b in v["blocks"] if b["kind"] == "work"]
    assert work and all(b["started"] for b in work) and all(b["outcome"] is None for b in work)
    assert v["checkin"] == {"enabled": True, "time": "21:30", "waiting": False, "sent": False}


def test_his_other_events_show_but_jarvis_blocks_are_not_shown_twice(svc, token, agenda):
    planned(svc)
    svc.seed("Lunch with Karim", paris(2026, 10, 6, 12, 30), paris(2026, 10, 6, 13, 30))
    v = T.view(TUE_1830)
    assert [e["title"] for e in v["events"]] == ["Lunch with Karim"]
    assert v["events_note"] == ""


def test_an_unreadable_calendar_leaves_the_rest_of_the_day(svc, agenda, monkeypatch, tmp_path):
    planned(svc)
    monkeypatch.setattr(cal, "TOKEN_PATH", tmp_path / "missing.json")
    v = T.view(TUE_1830)
    assert v["events"] is None and "not connected" in v["events_note"] and v["blocks"]


def test_todays_reminders_and_timed_checks_only(svc, token, agenda):
    planned(svc)
    agenda["reminders"] = [{"due": paris(2026, 10, 6, 19), "repeat": "once", "message": "call the notary"},
                           {"due": paris(2026, 10, 7, 9), "repeat": "once", "message": "tomorrow's"}]
    agenda["tasks"] = [{"due": paris(2026, 10, 6, 22), "repeat": "weekdays", "task": "NVDA close", "skill": ""}]
    v = T.view(TUE_1830)
    assert [r["text"] for r in v["reminders"]] == ["call the notary"]
    assert [t["text"] for t in v["tasks"]] == ["NVDA close"]


def test_a_tap_records_the_outcome_and_the_week_counts_it(svc, token, agenda):
    planned(svc)
    gym = block("gym")
    assert T.tap(gym["uid"], "done", TUE_1830) == "Gym 17:30-18:30: done."
    told = plans.outcomes([gym["uid"]])[gym["uid"]]
    assert (told["outcome"], told["actor"]) == ("done", "owner")
    v = T.view(TUE_1830)
    assert next(b for b in v["blocks"] if b["uid"] == gym["uid"])["outcome"]["outcome"] == "done"
    tue = next(d for d in v["week"] if d["date"] == "2026-10-06")
    assert tue["done"] == 1 and tue["planned"] >= 2
    assert "already recorded" in T.tap(gym["uid"], "done", TUE_1830)
    assert T.tap(gym["uid"], "partly", TUE_1830).endswith("partly.")      # a change of mind is recorded


def test_a_block_that_has_not_started_can_only_be_skipped(svc, token, agenda):
    planned(svc)
    gym = block("gym")
    with pytest.raises(W._Fail, match="has not started yet"):
        T.tap(gym["uid"], "done", TUE_1000)
    got: list = []
    out = T.tap(gym["uid"], "skipped", TUE_1000, got)
    assert out.startswith("Gym 17:30-18:30: skipped.")
    assert [p["uid"] for p in got] == [gym["uid"]]                           # a new slot is offered
    assert got[0]["args"]["action"] == "change" and got[0]["args"]["kind"] == "gym"
    assert got[0]["args"]["to_day"] > "2026-10-06"                           # not today, not its own slot
    assert D.drain_proposals() == []                                         # never the shared list


def test_a_pre_approved_kind_is_moved_at_once_on_a_skip(svc, token, agenda):
    from backend.storage import plans as st
    planned(svc)
    st.set_setting("auto_move", ["gym"])
    gym = block("gym")
    got: list = []
    out = T.tap(gym["uid"], "skipped", TUE_1000, got)
    assert got == [] and "Rescheduled (you pre-approved moving gym)" in out


def test_while_the_check_in_waits_a_tap_answers_it(svc, token, agenda):
    planned(svc)
    send_checkin(TUE)
    gym = block("gym")
    T.tap(gym["uid"], "done", TUE_1830)
    assert plans.outcomes([gym["uid"]])[gym["uid"]]["actor"] == "checkin"
    ci = checkins.get("2026-10-06")
    assert any(p["uid"] == gym["uid"] and p["via"] == "app" for p in ci["parsed"])
    assert ci["reply"] == ""                                                 # his words stay his words
    assert T.view(TUE_1830)["checkin"]["waiting"] is True


def test_every_block_tapped_means_no_evening_check_in(svc, token, agenda):
    planned(svc)
    for b in D.live_blocks_on(TUE):
        T.tap(b["uid"], "done", TUE_1830)
    assert D.checkin_payload(TUE) is None


def test_yesterday_stays_open_until_four_in_the_morning(svc, token, agenda):
    planned(svc)
    gym = block("gym")
    v = T.view(WED_0200)
    assert gym["uid"] in [b["uid"] for b in v["yesterday"]["blocks"]] and v["yesterday"]["label"] == "Tuesday"
    T.tap(gym["uid"], "done", WED_0200)
    assert gym["uid"] not in [b["uid"] for b in T.view(WED_0200)["yesterday"]["blocks"]]
    work = block("work")
    assert T.view(WED_0500)["yesterday"]["blocks"] == []
    with pytest.raises(W._Fail, match="not in today's plan"):
        T.tap(work["uid"], "done", WED_0500)


@pytest.mark.parametrize("uid,outcome,why", [("nope", "done", "not in today's plan"), (None, "unknown", "must be one of"),
                                             (None, "great", "must be one of")])
def test_a_bad_tap_says_why(svc, token, agenda, uid, outcome, why):
    planned(svc)
    with pytest.raises(W._Fail, match=why):
        T.tap(uid or block("gym")["uid"], outcome, TUE_1830)


def test_a_broken_plan_store_still_gives_a_day(monkeypatch, token, agenda, svc):
    monkeypatch.setattr(D, "live_blocks_on", lambda day: (_ for _ in ()).throw(RuntimeError("db gone")))
    v = T.view(TUE_1830)
    assert v["blocks"] == [] and "could not be read" in v["plan_error"] and "reminders" in v


# ------------------------------------------------------------------ routes

@pytest.fixture
def client(monkeypatch):
    from backend.mobile import routes
    from backend.mobile.test_routes import TOKEN
    monkeypatch.setenv("MOBILE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_the_routes_need_the_token_and_answer_with_the_day(client, monkeypatch):
    from backend.mobile.test_routes import AUTH
    monkeypatch.setattr(T, "view", lambda now=None, calendar=True: {"date": "2026-10-06", "blocks": []})
    assert client.get("/mobile/v1/today").status_code == 401
    assert client.get("/mobile/v1/today", headers=AUTH).json() == {"date": "2026-10-06", "blocks": []}


def test_a_tap_route_records_and_returns_the_new_day_or_409_with_the_reason(client, monkeypatch):
    from backend.mobile.test_routes import AUTH
    seen = []

    def tap(uid, outcome, now=None, proposals=None):
        seen.append((uid, outcome))
        if outcome == "done":
            raise W._Fail("Gym 17:30-18:30 has not started yet")
        proposals.append({"args": {}, "label": "Move Tuesday's gym (17:30-18:30) to Thursday 18:00-19:00",
                          "why": "", "uid": uid})
        proposals.append({"args": {}, "label": "Move Tuesday's yoga to Friday 08:00-09:00", "why": "", "uid": "y"})
        return "Gym 17:30-18:30: skipped.\nRescheduled (you pre-approved moving run): Moved Tuesday's run to Friday."
    monkeypatch.setattr(T, "tap", tap)
    monkeypatch.setattr(T, "view", lambda now=None, calendar=True: {"blocks": ["x"]})

    async def put_move(p):
        return {"id": 4} if "gym" in p["label"] else None             # the second could not be stored
    from backend.brain import reshuffle
    monkeypatch.setattr(reshuffle, "put_move", put_move)
    r = client.post("/mobile/v1/today/blocks/b1", json={"outcome": "done"}, headers=AUTH)
    assert r.status_code == 409 and "has not started" in r.json()["detail"]
    r = client.post("/mobile/v1/today/blocks/b1", json={"outcome": "skipped"}, headers=AUTH)
    assert r.status_code == 200
    moved = "Rescheduled (you pre-approved moving run): Moved Tuesday's run to Friday."
    lost = "I couldn't put the new slot to you as a request (Move Tuesday's yoga to Friday 08:00-09:00)."
    assert r.json() == {"text": f"Gym 17:30-18:30: skipped.\n{moved}\n"
                                f"Move Tuesday's gym (17:30-18:30) to Thursday 18:00-19:00?\n{lost}",
                        "note": f"{moved}\n{lost}",          # not the outcome (the badge) nor the offer (the card)
                        "asked": [4], "today": {"blocks": ["x"]}}
    assert seen == [("b1", "done"), ("b1", "skipped")]


def test_a_tap_never_takes_or_wipes_a_chat_reports_proposal(client, monkeypatch):
    """A `week_plan report` in a chat turn queues its proposal in planday's shared list and puts it
    after its executor returns. A tap landing in between must leave it there for that turn."""
    from backend.brain import reshuffle
    from backend.mobile.test_routes import AUTH
    theirs = {"args": {}, "label": "the chat turn's", "why": "", "uid": "chat"}
    D._pending_proposals.append(theirs)
    put = []

    async def put_move(p):
        put.append(p["label"])
        return {"id": 7}
    monkeypatch.setattr(reshuffle, "put_move", put_move)

    def tap(uid, outcome, now=None, proposals=None):
        proposals.append({"args": {}, "label": "the tap's", "why": "", "uid": uid})
        return "Gym 17:30-18:30: skipped."
    monkeypatch.setattr(T, "tap", tap)
    monkeypatch.setattr(T, "view", lambda now=None, calendar=True: {})
    r = client.post("/mobile/v1/today/blocks/b1", json={"outcome": "skipped"}, headers=AUTH)
    assert put == ["the tap's"] and r.json()["text"].endswith("the tap's?") and r.json()["asked"] == [7]
    assert r.json()["note"] == ""
    assert D._pending_proposals == [theirs]


def test_a_skip_offers_a_new_slot_on_the_card_and_approving_it_moves_the_block(client, svc, token, agenda, monkeypatch):
    """The whole path for real: the tap through the route, reshuffle's slot, the approval request,
    the offer on the gym's card, and approving it as the card does."""
    import asyncio

    from backend.mobile.test_routes import AUTH
    from backend.tools import approval_flow
    planned(svc)
    monkeypatch.setattr(W, "_now", lambda: TUE_1000)
    real_tap, real_view = T.tap, T.view
    monkeypatch.setattr(T, "tap", lambda uid, outcome, now=None, proposals=None: real_tap(uid, outcome, TUE_1000, proposals))
    monkeypatch.setattr(T, "view", lambda now=None, calendar=True: real_view(TUE_1000, calendar=calendar))
    approval_flow.set_notifier(None)
    gym = block("gym")
    r = client.post(f"/mobile/v1/today/blocks/{gym['uid']}", json={"outcome": "skipped"}, headers=AUTH).json()
    (rid,) = r["asked"]
    assert r["text"].startswith("Gym 17:30-18:30: skipped.\nMove Tuesday's gym (17:30-18:30) to ")
    assert r["text"].endswith("?") and "he taps" not in r["text"]
    assert r["note"] == ""                                                   # the card says all of it
    card = next(b for b in r["today"]["blocks"] if b["uid"] == gym["uid"])
    assert card["outcome"]["outcome"] == "skipped"
    from datetime import date

    from backend.brain import planner as P
    from backend.storage import approvals
    args = approvals.get(rid)["arguments"]
    to_day = date.fromisoformat(args["to_day"])
    assert to_day > TUE                                                      # (Wednesday 07:45 with these rules)
    assert card["offer"] == {"id": rid, "label": f"Move it to {P.day_name(to_day)} {args['start']}",
                             "why": "you skipped it"}
    assert all(b["offer"] is None for b in r["today"]["blocks"] if b["uid"] != gym["uid"])
    text = asyncio.run(approval_flow.approve(rid, "phone"))
    assert f"Moved Tuesday's gym to {P.day_name(to_day)}" in text, text
    assert gym["uid"] in [b["uid"] for b in D.live_blocks_on(to_day)]
    assert gym["uid"] not in [b["uid"] for b in client.get("/mobile/v1/today", headers=AUTH).json()["blocks"]]


def test_any_pending_move_shows_on_its_block_and_a_same_day_one_says_today(svc, token, agenda):
    """A clash found by the 30-minute look can move a block later the same day; other requests
    (an email, a move of another block) stay off the card."""
    from backend.storage import approvals
    planned(svc)
    move = {"action": "change", "week": "2026-10-05", "kind": "gym", "day": "2026-10-06", "near": "17:30",
            "to_day": "2026-10-06", "start": "19:45"}
    rid = approvals.create(context="planner", tool="week_plan", arguments=move, preview="Move it", risk="own",
                           why="'Board dinner' now overlaps it")
    approvals.create(context="planner", tool="week_plan", arguments={**move, "near": "09:00", "kind": "work"},
                     preview="x", risk="own", why="")
    approvals.create(context="turn", tool="gmail_action", arguments={"action": "send_email", "day": "2026-10-06"},
                     preview="x", risk="outward", why="")
    blocks = {b["kind"] + b["uid"]: b for b in T.view(TUE_1000)["blocks"]}
    gym = next(b for k, b in blocks.items() if k.startswith("gym"))
    assert gym["offer"] == {"id": rid, "label": "Move it to today 19:45", "why": "'Board dinner' now overlaps it"}
    assert [b["offer"] for b in blocks.values() if b["kind"] == "work" and b["offer"]] != []    # its own request
    assert all(b["offer"] is None or b["offer"]["id"] != rid for b in blocks.values() if b["kind"] == "work")


def test_a_block_ahead_has_not_started(svc, token, agenda):
    planned(svc)
    gym = next(b for b in T.view(TUE_1000)["blocks"] if b["kind"] == "gym")
    assert gym["started"] is False
    assert all(b["started"] for b in T.view(TUE_1000)["blocks"] if b["kind"] == "work" and b["start"] <= TUE_1000.timestamp())


def test_a_skip_told_after_the_block_can_still_be_offered_later_that_evening(svc, token, agenda):
    """The gym ended at 18:30 and he says at 18:40 that he skipped it: there is still room at
    18:45 tonight, so that is offered (a skip ahead of time looks from the next day on)."""
    planned(svc)
    got: list = []
    T.tap(block("gym")["uid"], "skipped", datetime(2026, 10, 6, 16, 40, tzinfo=timezone.utc), got)
    assert (got[0]["args"]["to_day"], got[0]["args"]["start"]) == ("2026-10-06", "18:45")
