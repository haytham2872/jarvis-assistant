"""Phone slice 4: the Inbox routes, the delivery setting, push registration, what the phone schedules
itself (mobile/upcoming.py), and a phone message answering an Inbox item."""
import datetime as dt

import pytest

from backend.brain import notices
from backend.mobile import upcoming as U
from backend.mobile.test_conversation import AUTH, _client, _isolation, _main, run  # noqa: F401 (fixture)
from backend.storage import notices as store

LOCAL_TZ = U.LOCAL_TZ
# Tuesday 6 October 2026, 10:00 Paris
NOW = dt.datetime(2026, 10, 6, 10, 0, tzinfo=LOCAL_TZ)


def at(day, h, m=0):
    return dt.datetime(2026, 10, day, h, m, tzinfo=LOCAL_TZ)


# ------------------------------------------------------------------ routes

def test_the_inbox_lists_newest_first_and_marks_read(monkeypatch):
    c = _client(monkeypatch)
    a = store.record("reminder", "Reminder: notary")
    b = store.record("brief", "Good morning")
    body = c.get("/mobile/v1/inbox", headers=AUTH).json()
    assert [n["id"] for n in body["notices"]] == [b, a] and body["unread"] == 2
    assert body["route"] == "discord" and body["push"] == "unknown"
    assert c.post("/mobile/v1/inbox/read", json={"up_to": b}, headers=AUTH).json() == {"unread": 0}
    assert c.post("/mobile/v1/inbox/read", json={"up_to": "x"}, headers=AUTH).status_code == 400
    older = c.get(f"/mobile/v1/inbox?before={b}", headers=AUTH).json()["notices"]
    assert [n["id"] for n in older] == [a]
    assert c.get("/mobile/v1/inbox").status_code == 401


def test_the_route_is_set_from_the_phone_and_checked(monkeypatch):
    c = _client(monkeypatch)
    r = c.put("/mobile/v1/notices/settings", json={"route": "phone"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["route"] == "phone"
    assert c.get("/mobile/v1/notices/settings", headers=AUTH).json()["route"] == "phone"
    assert c.put("/mobile/v1/notices/settings", json={"route": "sms"}, headers=AUTH).status_code == 400
    assert store.route() == "phone"


def test_a_push_token_gets_a_test_and_is_trusted_only_once_the_phone_saw_it(monkeypatch):
    c = _client(monkeypatch)
    tests = []

    async def fake_test(token):
        tests.append(token)
        return 1
    monkeypatch.setattr(notices, "send_test", fake_test)
    head = {**AUTH, "X-Push-Token": "ExponentPushToken[abc]"}
    r = c.post("/mobile/v1/push", headers=head).json()
    assert r["push"] == "waiting" and r["test_sent"] is True and tests == ["ExponentPushToken[abc]"]
    assert store.tokens() == []                   # not pushed to until confirmed
    assert c.post("/mobile/v1/push/confirm", headers=head).json()["push"] == "confirmed"
    assert store.tokens() == ["ExponentPushToken[abc]"]
    # registering again once confirmed sends no second test
    c.post("/mobile/v1/push", headers=head)
    assert tests == ["ExponentPushToken[abc]"]
    assert c.post("/mobile/v1/push", headers=AUTH).status_code == 400
    assert c.post("/mobile/v1/push/confirm",
                  headers={**AUTH, "X-Push-Token": "ExponentPushToken[nope]"}).status_code == 404


def test_upcoming_answers_with_the_items_and_the_settings(monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setattr(U, "items", lambda: [{"id": "x", "kind": "reminder", "at": 1.0, "title": "Reminder",
                                              "body": "gym", "screen": "inbox"}])
    body = c.get("/mobile/v1/upcoming", headers=AUTH).json()
    assert body["items"][0]["body"] == "gym" and body["route"] == "discord"


# ------------------------------------------------------------------ answering an Inbox message

def test_a_message_answering_an_inbox_item_shows_the_turn_that_item(monkeypatch):
    from backend.brain import turn_input
    from backend.mobile import conversation
    from backend.storage import inbox
    M, _ = _main(monkeypatch)
    seen = []

    async def chat(text, history, send, sentence_queue=None, channel="local", **kw):
        seen.append(turn_input.reply_to.get())
        return "ok"
    monkeypatch.setattr(M, "chat", chat)
    n = store.record("research", "Camus's notebooks were acquired by the BnF.")
    conversation.submit("req-i0000001", "tell me more about this", reply_to=n)
    conversation.submit("req-i0000002", "and something else")
    run(M._admit_pending())
    assert inbox.get("mobile:req-i0000001")["context"]["reply_to"] == n
    assert seen[0]["text"].startswith("Camus's notebooks") and seen[0]["kind"] == "research"
    assert seen[1] is None
    block = turn_input.render_reply(seen[0])
    assert "These are your words" in block and "Camus's notebooks" in block


def test_the_routes_carry_reply_to_and_ignore_nonsense(monkeypatch):
    from backend.storage import inbox
    c = _client(monkeypatch)
    for rid, value in (("req-i0000003", 7), ("req-i0000004", "x"), ("req-i0000005", True),
                       ("req-i0000006", -2)):
        assert c.post("/mobile/v1/messages", json={"request_id": rid, "text": "hm", "reply_to": value},
                      headers=AUTH).status_code == 202
    assert inbox.get("mobile:req-i0000003")["context"]["reply_to"] == 7
    for rid in ("req-i0000004", "req-i0000005", "req-i0000006"):
        assert "reply_to" not in inbox.get(f"mobile:{rid}")["context"]


def test_the_turn_is_shown_the_message_only_on_the_phone():
    import inspect
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert '_turn_input.render_reply(_turn_input.reply_to.get()) if channel == "mobile" else ""' in src
    assert turn_render_is_empty()


def turn_render_is_empty() -> bool:
    from backend.brain import turn_input
    return turn_input.render_reply(None) == "" and turn_input.render_reply({"text": ""}) == ""


# ------------------------------------------------------------------ what the phone schedules

@pytest.fixture
def world(monkeypatch):
    from backend.brain import chronos, planday
    from backend.storage import pauses, plans
    state = {"reminders": [], "tasks": [], "brief": {"enabled": False, "time": "08:00", "last_date": ""},
             "blocks": set(), "today_payload": None, "checkin": (False, "21:30"), "review": (False, "19:00"),
             "applied": set(), "pause": None}
    monkeypatch.setattr(chronos, "agenda", lambda: {"reminders": state["reminders"], "tasks": state["tasks"],
                                                    "night": [], "done": [], "brief": state["brief"],
                                                    "parked": 0, "night_results": 0})
    monkeypatch.setattr(planday, "checkin_config", lambda: state["checkin"])
    monkeypatch.setattr(planday, "review_config", lambda: state["review"])
    monkeypatch.setattr(planday, "live_blocks_on", lambda d: [{"uid": "u"}] if d in state["blocks"] else [])
    monkeypatch.setattr(planday, "checkin_payload", lambda d: state["today_payload"])
    monkeypatch.setattr(plans, "applied", lambda week: {"id": 1} if week in state["applied"] else None)
    monkeypatch.setattr(pauses, "active", lambda now=None: state["pause"])
    return state


def kinds(items):
    return [(i["kind"], dt.datetime.fromtimestamp(i["at"], LOCAL_TZ).strftime("%a %H:%M")) for i in items]


def test_a_reminder_carries_its_own_words_and_a_daily_one_repeats(world):
    world["reminders"] = [{"due": at(6, 9), "repeat": "once", "message": "past one"},
                          {"due": at(6, 18), "repeat": "once", "message": "call the notary"},
                          {"due": at(6, 8, 30), "repeat": "daily", "message": "vitamins"}]
    items = U.items(NOW)
    assert [i["body"] for i in items if i["kind"] == "reminder"][:2] == ["call the notary", "vitamins"]
    assert sum(1 for i in items if i["body"] == "vitamins") == 7     # Wed..Tue next week
    assert not any(i["body"] == "past one" for i in items)
    assert all(i["screen"] == "inbox" for i in items)


def test_the_brief_comes_a_few_minutes_after_its_time_and_not_twice_a_day(world):
    world["brief"] = {"enabled": True, "time": "08:00", "last_date": "2026-10-07"}
    times = kinds(U.items(NOW))
    assert times[0] == ("brief", "Thu 08:03")                  # Wednesday's was already sent
    assert ("brief", "Wed 08:03") not in times
    world["brief"]["enabled"] = False
    assert U.items(NOW) == []


def test_a_scheduled_check_follows_its_recurrence_and_its_end(world):
    world["tasks"] = [{"due": at(6, 15, 30), "repeat": "weekdays", "until": "2026-10-08", "tz": "Europe/Paris",
                       "task": "NVDA at the open", "skill": ""}]
    times = kinds(U.items(NOW))
    assert times == [("check", "Tue 15:32"), ("check", "Wed 15:32"), ("check", "Thu 15:32")]


def test_the_checkin_opens_today_and_only_on_days_with_a_plan(world):
    world["checkin"] = (True, "21:30")
    world["blocks"] = {dt.date(2026, 10, 7)}
    world["today_payload"] = ("Evening check-in", ["u"])
    items = [i for i in U.items(NOW) if i["kind"] == "checkin"]
    assert kinds(items) == [("checkin", "Tue 21:30"), ("checkin", "Wed 21:30")]
    assert {i["screen"] for i in items} == {"today"}
    world["today_payload"] = None                               # everything told already today
    assert kinds([i for i in U.items(NOW) if i["kind"] == "checkin"]) == [("checkin", "Wed 21:30")]


def test_the_review_only_for_a_week_with_a_plan(world, monkeypatch):
    from backend.storage import plans
    world["review"] = (True, "19:00")
    world["applied"] = {"2026-10-05"}
    monkeypatch.setattr(plans, "get_setting", lambda key, default=None: None)
    assert kinds(U.items(NOW)) == [("review", "Sun 19:02")]
    world["applied"] = set()
    assert U.items(NOW) == []


def test_a_pause_leaves_out_what_it_holds(world):
    world["reminders"] = [{"due": at(6, 18), "repeat": "once", "message": "gym"}]
    world["tasks"] = [{"due": at(6, 15, 30), "repeat": "once", "until": "", "tz": "Europe/Paris",
                       "task": "NVDA", "skill": ""}]
    world["pause"] = {"scope": "work", "until_at": None}
    assert [i["kind"] for i in U.items(NOW)] == ["reminder"]   # /pause: checks wait, reminders come
    world["pause"] = {"scope": "all", "until_at": None}
    assert U.items(NOW) == []
    world["pause"] = {"scope": "all", "until_at": at(6, 17).timestamp()}
    assert [i["kind"] for i in U.items(NOW)] == ["reminder"]   # ends before the reminder


def test_one_unreadable_part_leaves_the_rest(world, monkeypatch):
    from backend.brain import planday
    world["reminders"] = [{"due": at(6, 18), "repeat": "once", "message": "gym"}]
    world["checkin"] = (True, "21:30")

    def boom(day):
        raise RuntimeError("plan unreadable")
    monkeypatch.setattr(planday, "checkin_payload", boom)
    assert [i["kind"] for i in U.items(NOW)] == ["reminder"]


def test_never_more_than_ios_keeps(world):
    world["reminders"] = [{"due": at(6, 11) + dt.timedelta(minutes=10 * k), "repeat": "once",
                           "message": f"r{k}"} for k in range(80)]
    items = U.items(NOW)
    assert len(items) == U.MAX_ITEMS and items[0]["body"] == "r0"


# ------------------------------------------------------------------ slice 5: an Inbox message read aloud

def test_an_inbox_message_can_be_read_aloud_from_the_servers_own_copy(monkeypatch):
    c = _client(monkeypatch)
    n = store.record("brief", "Good morning. **Two things** today: the notary, and NVDA's close.")
    plan = c.get(f"/mobile/v1/speech/plan?source=notice:{n}", headers=AUTH).json()
    said = " ".join(clip["text"] for clip in plan["clips"])
    assert "Two things today" in said and "**" not in said
    assert c.get("/mobile/v1/speech/plan?source=notice:99999", headers=AUTH).status_code == 404
    assert c.get("/mobile/v1/speech/plan?source=notice:abc", headers=AUTH).status_code == 400
