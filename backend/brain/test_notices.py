"""Phone slice 4: JARVIS's own messages kept for the Inbox and routed to Discord, the phone or both
(brain/notices.py, storage/notices.py). The SQLite rail in backend/conftest.py gives each test its
own database; nothing reaches Discord or Expo's push service."""
import asyncio

import pytest

from backend.brain import notices
from backend.storage import notices as store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def hub(monkeypatch):
    from backend.mobile import events
    h = events.Hub()
    monkeypatch.setattr(events, "HUB", h)
    return h


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def fake_post(messages):
        sent.extend(messages)
        return [{"status": "ok"} for _ in messages]
    monkeypatch.setattr(notices, "_post_push", fake_post)
    return sent


def discord(result=True):
    calls = []

    async def send(text):
        calls.append(text)
        return result
    send.calls = calls
    return send


# ------------------------------------------------------------------ the store

def test_a_message_is_kept_newest_first_and_counted_unread():
    a = store.record("reminder", "Reminder: call the notary", now=1000)
    b = store.record("brief", "Good morning", now=2000)
    rows = store.listing()
    assert [r["id"] for r in rows] == [b, a]
    assert rows[0]["kind"] == "brief" and rows[0]["read"] is False and rows[0]["discord"] is None
    assert store.unread_count() == 2
    assert store.mark_read(a) == 1 and store.unread_count() == 1
    assert [r["id"] for r in store.listing(before=b)] == [a]


def test_the_same_key_is_one_message():
    first = store.record("goal", "Your goal is done", key="goal:g1:result")
    assert store.record("goal", "Your goal is done", key="goal:g1:result") == first
    assert len(store.listing()) == 1


def test_a_failed_send_retried_with_the_same_words_reuses_its_row():
    first = store.record("checkin", "How did today go?", now=1000)
    store.set_discord(first, False)
    assert store.record("checkin", "How did today go?", now=1060) == first
    store.set_discord(first, True)
    # once it went out, the same words later are a new message (tomorrow's check-in)
    assert store.record("checkin", "How did today go?", now=1120) != first


def test_a_brief_recomposed_after_a_failed_send_replaces_the_unsent_one():
    first = store.record("brief", "Brief, first attempt", now=1_760_000_000)
    store.set_discord(first, False)
    again = store.record("brief", "Brief, second attempt", now=1_760_000_060)
    assert again == first
    assert store.get(first)["text"] == "Brief, second attempt"
    assert len(store.listing()) == 1


def test_a_success_is_never_turned_back_into_a_failure():
    n = store.record("watch", "NVDA closed below 180")
    store.set_discord(n, True)
    store.set_discord(n, False)
    assert store.get(n)["discord"] is True


def test_the_route_defaults_to_discord_and_refuses_anything_else():
    assert store.route() == "discord"
    assert store.set_route("phone") and store.route() == "phone"
    assert not store.set_route("pigeon") and store.route() == "phone"


def test_a_push_token_counts_only_once_confirmed():
    store.add_token("ExponentPushToken[a]")
    assert store.token_state("ExponentPushToken[a]") == "waiting"
    assert store.tokens() == []
    assert store.confirm_token("ExponentPushToken[a]")
    assert store.tokens() == ["ExponentPushToken[a]"]
    assert store.token_state("ExponentPushToken[zz]") == "unknown"


# ------------------------------------------------------------------ the route

def test_with_discord_it_is_sent_as_before_and_kept_with_what_became_of_it(hub, pushes):
    store.add_token("T1"); store.confirm_token("T1")      # a confirmed phone is still not pushed
    send = discord(True)

    async def scenario():
        ok = await notices.deliver("watch", send)("NVDA closed below 180")
        await asyncio.gather(*notices._pending)
        return ok
    assert run(scenario()) is True
    assert send.calls == ["NVDA closed below 180"]
    row = store.listing()[0]
    assert row["kind"] == "watch" and row["discord"] is True
    assert pushes == []                           # Discord only: the phone is not pushed


def test_with_discord_a_failed_send_is_still_a_failure_for_the_caller(hub):
    send = discord(False)
    assert run(notices.deliver("watch", send)("x")) is False
    assert store.listing()[0]["discord"] is False


def test_with_phone_nothing_goes_to_discord_and_the_phone_hears_of_it(hub, pushes):
    store.set_route("phone")
    store.add_token("T1"); store.confirm_token("T1")
    store.add_token("T2")                         # never confirmed: not pushed
    send = discord(True)

    async def scenario():
        ok = await notices.deliver("research", send)("A finding about Camus")
        await asyncio.gather(*notices._pending)
        return ok
    assert run(scenario()) is True
    assert send.calls == []
    assert store.listing()[0]["discord"] is None
    assert [e["type"] for e in hub._events] == ["notice"]
    assert hub._events[0]["notice"]["text"] == "A finding about Camus"
    assert [p["to"] for p in pushes] == ["T1"] and pushes[0]["title"] == "Research"


def test_with_phone_a_broken_store_falls_back_to_discord(hub, monkeypatch):
    store.set_route("phone")
    monkeypatch.setattr(store, "record", lambda *a, **k: None)
    send = discord(True)
    assert run(notices.deliver("reminder", send)("Reminder: gym")) is True
    assert send.calls == ["Reminder: gym"]


def test_with_both_a_failed_dm_still_reached_the_phone(hub, pushes):
    store.set_route("both")
    send = discord(False)
    assert run(notices.deliver("job", send)("Done: the report")) is True
    assert send.calls == ["Done: the report"]
    assert store.listing()[0]["discord"] is False


def test_a_label_set_by_the_caller_names_the_kind(hub):
    send = discord(True)

    async def scenario():
        with notices.labelled("brief"):
            await notices.deliver("check", send)("Good morning")
        await notices.deliver("check", send)("I missed a scheduled check")
    run(scenario())
    assert [r["kind"] for r in store.listing()] == ["check", "brief"]


def test_a_keyed_message_retried_is_one_inbox_entry(hub):
    calls = []

    async def keyed(text, key):
        calls.append(key)
        return len(calls) > 1                     # fails once, then goes out
    deliver = notices.deliver_keyed("goal", keyed)
    assert run(deliver("Goal done", "goal:g1:result")) is False
    assert run(deliver("Goal done", "goal:g1:result")) is True
    assert calls == ["goal:g1:result", "goal:g1:result"]
    rows = store.listing()
    assert len(rows) == 1 and rows[0]["discord"] is True


def test_a_reminder_pushes_its_own_words():
    assert notices.push_text("reminder", "Reminder: call the notary") == ("Reminder", "call the notary")
    title, body = notices.push_text("brief", "x" * 400)
    assert title == "Morning brief" and len(body) <= 178 and body.endswith("…")


def test_a_token_expo_no_longer_knows_is_forgotten(monkeypatch):
    store.add_token("T1"); store.confirm_token("T1")
    store.add_token("T2"); store.confirm_token("T2")

    async def fake_post(messages):
        return [{"status": "error", "details": {"error": "DeviceNotRegistered"}}, {"status": "ok"}]
    monkeypatch.setattr(notices, "_post_push", fake_post)
    assert run(notices.push(["T1", "T2"], "t", "b", {})) == 1
    assert store.tokens() == ["T2"]


def test_a_push_service_failure_never_raises(monkeypatch):
    store.add_token("T1"); store.confirm_token("T1")

    async def boom(messages):
        raise RuntimeError("no network")
    monkeypatch.setattr(notices, "_post_push", boom)
    assert run(notices.push(["T1"], "t", "b", {})) == 0
    assert store.tokens() == ["T1"]               # one failure does not drop a confirmed token


def test_every_module_that_messages_him_goes_through_the_router():
    """main.py cannot be started in a test; this pins the wiring."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "bootstrap.wire_notifier(" not in src
    block = src.split("Phone slice 4: every message")[1].split("research.set_notifier")[0]
    for module in ("backup_loop", "canary", "chronos", "watches", "forecasts", "people_research",
                   "portfolio", "background", "planloop", "pause"):
        assert f"({module}, " in block, module
    assert 'research.set_notifier(notices.deliver("research"' in src
    assert 'goal_runner.set_notifier(notices.deliver_keyed("goal"' in src


def test_reminders_briefs_checkins_reviews_and_checks_are_labelled():
    import inspect
    from backend.brain import background, chronos, planloop
    assert 'labelled("reminder")' in inspect.getsource(chronos._fire_due_reminders)
    assert 'labelled("brief")' in inspect.getsource(chronos._send_brief)
    assert 'labelled("checkin")' in inspect.getsource(planloop._maybe_checkin)
    assert inspect.getsource(planloop._maybe_review).count('labelled("review")') == 2
    assert '"check" if t.kind == "scheduled" else "job"' in inspect.getsource(background)


def test_jarvis_is_told_where_his_messages_go_only_when_not_discord_alone():
    from backend.brain import runtime_status
    assert notices.prompt_line() == ""
    assert "[Delivery" not in runtime_status.runtime_identity_text()
    store.set_route("phone")
    line = notices.prompt_line()
    assert "phone's Inbox, not Discord" in line and "never that you will DM him on Discord" in line
    assert line in runtime_status.runtime_identity_text()
    store.set_route("both")
    assert "go to Discord and to his phone's Inbox" in notices.prompt_line()
