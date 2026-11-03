"""M1: the phone as a conversation channel.

Driven through main.py's real admission and turn path; only chat() (the model) is stubbed,
and the database is the per-test one conftest provides. Covers the plan's M1 gate:
acceptance and retry, isolation from Discord and the desk, restart, a missed completion
recovered from the snapshot, and a new chat while messages wait.
"""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "mobile-" + "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "TINYFISH_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("NO_TTS", "1")
    monkeypatch.setenv("MOBILE_TOKEN", TOKEN)
    from backend.brain import engine, steering
    from backend.mobile import events
    from backend.tools import vault
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    monkeypatch.setattr(events, "HUB", events.Hub())
    from backend.mobile import conversation
    monkeypatch.setattr(conversation, "HUB", events.HUB)
    steering._reset_memory()
    engine._reset_turn_stats()
    yield
    steering._reset_memory()
    engine._reset_turn_stats()


def run(coro):
    return asyncio.run(coro)


def _main(monkeypatch, answer=lambda text: f"ANSWER TO {text}", events_to_send=()):
    """main.py with a stub model that streams `events_to_send` and returns `answer(text)`."""
    import backend.main as M
    calls = []

    async def fake_chat(text, history, send, sentence_queue=None, channel="local", **kw):
        calls.append((text, channel))
        for evt in events_to_send:
            await send(evt)
        return answer(text)
    monkeypatch.setattr(M, "chat", fake_chat)
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    monkeypatch.setattr(M, "broadcast", _noop)
    monkeypatch.setattr(M, "send_state", _noop)
    M.mobile_conversation.configure(admit=lambda: None, running=lambda: dict(M._running),
                                    new_chat=M.mobile_new_session)
    return M, calls


async def _noop(*a, **k):
    return None


# ------------------------------------------------------------------ acceptance and retry

def test_a_message_is_stored_before_it_is_acknowledged_and_runs_as_a_mobile_turn(monkeypatch):
    M, calls = _main(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import inbox
    first = conversation.submit("req-00000001", "what's NVDA at?")
    assert first["state"] == "queued"
    assert inbox.get("mobile:req-00000001")["conversation_id"] == "mobile"
    run(M._admit_pending())
    assert calls == [("what's NVDA at?", "mobile")]
    done = conversation.status("req-00000001")
    assert done["state"] == "answered" and done["answer"] == "ANSWER TO what's NVDA at?"
    assert done["text"] == "what's NVDA at?"      # a phone that restarted can show what it sent
    assert done["run_id"]


def test_a_retry_after_a_lost_acknowledgement_does_not_run_twice(monkeypatch):
    M, calls = _main(monkeypatch)
    from backend.mobile import conversation
    conversation.submit("req-00000002", "book the gym")
    run(M._admit_pending())
    again = conversation.submit("req-00000002", "book the gym")      # same id, same text
    run(M._admit_pending())
    assert len(calls) == 1
    assert again["state"] == "answered"


def test_the_same_request_id_with_different_text_is_refused(monkeypatch):
    _main(monkeypatch)
    from backend.mobile import conversation
    conversation.submit("req-00000003", "email Sarah")
    with pytest.raises(conversation.Rejected) as exc:
        conversation.submit("req-00000003", "email Marc")
    assert exc.value.status == 409


@pytest.mark.parametrize("rid,text,kind,status", [
    ("short", "hi", "text", 400),
    ("bad id with spaces", "hi", "text", 400),
    ("req-00000004", "   ", "text", 400),
    ("req-00000004", "x" * 8_001, "text", 413),
    ("req-00000004", "hi", "telepathy", 400),
])
def test_malformed_messages_are_refused_before_anything_is_stored(monkeypatch, rid, text, kind, status):
    _main(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import mobile_requests
    with pytest.raises(conversation.Rejected) as exc:
        conversation.submit(rid, text, kind)
    assert exc.value.status == status
    assert mobile_requests.get(rid) is None


def test_an_inbox_failure_is_not_acknowledged_and_a_retry_works(monkeypatch):
    M, calls = _main(monkeypatch)
    from backend.brain import steering
    from backend.mobile import conversation
    monkeypatch.setattr(steering, "_store_insert", lambda **kw: 0)
    with pytest.raises(conversation.Rejected) as exc:
        conversation.submit("req-00000005", "remind me at 9")
    assert exc.value.status == 503
    monkeypatch.undo()
    _main(monkeypatch)
    assert conversation.submit("req-00000005", "remind me at 9")["state"] == "queued"


# ------------------------------------------------------------------ isolation

def test_a_phone_message_never_steers_a_desk_or_discord_turn(monkeypatch):
    M, _ = _main(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import inbox
    M._running.update(conversation="discord:dm", run_id="run-discord")
    try:
        conversation.submit("req-00000006", "also check TTWO")
    finally:
        M._running.clear()
    assert inbox.get("mobile:req-00000006")["target_run_id"] is None


def test_a_phone_message_during_a_phone_turn_joins_it(monkeypatch):
    M, _ = _main(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import inbox
    M._running.update(conversation="mobile", run_id="run-phone")
    try:
        conversation.submit("req-00000007", "in euros please")
    finally:
        M._running.clear()
    assert inbox.get("mobile:req-00000007")["target_run_id"] == "run-phone"


def test_a_phone_turn_uses_its_own_history_not_discords(monkeypatch):
    M, calls = _main(monkeypatch)
    from backend.mobile import conversation
    M.conversation_histories["discord"] = [{"role": "user", "content": "discord secret"}]
    conversation.submit("req-00000008", "hello")
    run(M._admit_pending())
    assert M._get_history("mobile")[-2:] == [{"role": "user", "content": "hello"},
                                             {"role": "assistant", "content": "ANSWER TO hello"}]
    assert all("discord secret" not in m["content"] for m in M._get_history("mobile"))


# ------------------------------------------------------------------ events and snapshot

def test_the_phone_sees_the_turn_live_and_the_final_answer(monkeypatch):
    M, _ = _main(monkeypatch, events_to_send=[{"type": "thinking", "delta": "hmm"},
                                              {"type": "response", "delta": "ANSWER"},
                                              {"type": "amplitude", "value": 0.4}])
    from backend.mobile import conversation, events
    conversation.submit("req-00000009", "status?")
    run(M._admit_pending())
    types = [e["type"] for e in events.HUB.since(events.HUB.epoch, 0)]
    assert types == ["accepted", "run_started", "thinking", "response", "run_finished"]
    final = events.HUB.since(events.HUB.epoch, 0)[-1]
    assert final["request_id"] == "req-00000009" and final["answer"] == "ANSWER TO status?"


def test_a_missed_completion_is_in_the_snapshot(monkeypatch):
    M, _ = _main(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import exchanges
    conversation.submit("req-00000010", "what did I miss?")
    run(M._admit_pending())
    now = time.time()
    exchanges.record_exchange(exchange_id="ex-1", session_id="s-1", channel="mobile", timestamp=now - 60,
                              user_text="earlier", assistant_text="earlier answer", significance=0.1, high=False)
    exchanges.record_exchange(exchange_id="ex-2", session_id="s-1", channel="discord", timestamp=now - 30,
                              user_text="discord only", assistant_text="x", significance=0.1, high=False)
    snap = conversation.snapshot()
    assert [e["user"] for e in snap["exchanges"]] == ["earlier"]
    assert {r["request_id"]: r["answer"] for r in snap["requests"]}["req-00000010"] == "ANSWER TO what did I miss?"
    assert snap["epoch"] and snap["seq"] >= 3


def test_a_failed_turn_is_reported_not_silent(monkeypatch):
    def boom(text):
        raise RuntimeError("provider down")
    M, _ = _main(monkeypatch, answer=boom)
    from backend.mobile import conversation, events
    conversation.submit("req-00000011", "hello")
    run(M._admit_pending())
    assert conversation.status("req-00000011")["state"] == "failed"
    assert events.HUB.since(events.HUB.epoch, 0)[-1]["outcome"] == "failed"


def test_a_restart_mid_turn_reports_interrupted_and_never_reruns(monkeypatch):
    _main(monkeypatch)
    from backend.brain import steering
    from backend.mobile import conversation
    from backend.storage import inbox
    conversation.submit("req-00000012", "send the report")
    assert steering.claim_next()["message_key"] == "mobile:req-00000012"   # running when it died
    steering._reset_memory()
    steering.recover()
    assert conversation.status("req-00000012")["state"] == "interrupted"
    assert inbox.claim_next() is None


def test_new_chat_cancels_waiting_messages_and_says_so(monkeypatch):
    M, _ = _main(monkeypatch)
    from backend.mobile import conversation
    monkeypatch.setattr(M, "force_new_session", lambda ch: None)
    monkeypatch.setattr(M.history_refill, "mark_new", lambda ch: None)
    conversation.submit("req-00000013", "first")
    conversation.submit("req-00000014", "second")
    msg = run(conversation.new_chat())
    assert "2 earlier message" in msg
    assert conversation.status("req-00000013")["state"] == "cancelled"


def test_hub_resync_rules():
    from backend.mobile.events import Hub
    hub = Hub(buffer=3)
    for i in range(5):
        hub.publish({"type": "x", "i": i})
    assert hub.since("other-epoch", 0) is None             # an earlier process
    assert hub.since(hub.epoch, 99) is None                 # a position from the future
    assert hub.since(hub.epoch, 0) is None                  # fell out of the buffer
    assert [e["i"] for e in hub.since(hub.epoch, 3)] == [3, 4]
    assert hub.since(hub.epoch, 5) == []


# ------------------------------------------------------------------ the routes

def _client(monkeypatch):
    _main(monkeypatch)
    from backend.mobile import routes
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_routes_accept_resolve_and_snapshot(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/mobile/v1/messages", json={"request_id": "req-00000015", "text": "hi"}, headers=AUTH)
    assert r.status_code == 202 and r.json()["state"] == "queued"
    assert c.post("/mobile/v1/messages", json={"request_id": "req-00000015", "text": "other"},
                  headers=AUTH).status_code == 409
    assert c.get("/mobile/v1/requests/req-00000015", headers=AUTH).json()["state"] == "queued"
    assert c.get("/mobile/v1/requests/req-unknown1", headers=AUTH).status_code == 404
    assert c.get("/mobile/v1/snapshot", headers=AUTH).json()["schema_version"] == 1
    assert c.post("/mobile/v1/messages", json={"request_id": "req-00000016", "text": "hi"}).status_code == 401


def test_the_events_socket_resyncs_a_stale_phone_then_streams(monkeypatch):
    c = _client(monkeypatch)
    from backend.mobile import conversation, events
    with c.websocket_connect("/mobile/v1/events?epoch=old&after=7", headers=AUTH) as ws:
        first = ws.receive_json()
        assert first["type"] == "resync" and first["epoch"] == events.HUB.epoch
        conversation.submit("req-00000017", "live?")
        evt = ws.receive_json()
        assert evt["type"] == "accepted" and evt["request_id"] == "req-00000017"


def test_the_events_socket_replays_what_a_phone_missed_once(monkeypatch):
    c = _client(monkeypatch)
    from backend.mobile import conversation, events
    conversation.submit("req-00000018", "one")
    seen_before = events.HUB.seq
    conversation.submit("req-00000019", "two")
    with c.websocket_connect(f"/mobile/v1/events?epoch={events.HUB.epoch}&after={seen_before}",
                             headers=AUTH) as ws:
        missed = ws.receive_json()
        assert missed["request_id"] == "req-00000019"
        conversation.submit("req-00000020", "three")
        nxt = ws.receive_json()
        assert nxt["request_id"] == "req-00000020"         # not req-19 again


def test_the_events_socket_needs_the_token(monkeypatch):
    c = _client(monkeypatch)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/mobile/v1/events"):
            pass
    assert exc.value.code == 4401


def test_a_real_phone_turn_registers_as_the_mobile_conversation(monkeypatch):
    """A message sent during a running phone turn joins that turn; that only works if the
    turn itself registers as the "mobile" conversation (main._turn_identity)."""
    import backend.main as M
    from backend.mobile import conversation
    from backend.storage import inbox
    seen = {}

    def answer(text):
        if text == "first":
            seen["running"] = dict(M._running)
            conversation.submit("req-00000022", "and in euros")
        return f"ANSWER TO {text}"
    M, _ = _main(monkeypatch, answer=answer)
    conversation.submit("req-00000021", "first")
    run(M._admit_pending())
    assert seen["running"]["conversation"] == "mobile"
    assert inbox.get("mobile:req-00000022")["target_run_id"] == seen["running"]["run_id"]
    # This stub turn has no tool loop, so no safe point absorbed it: under the G3 rules it
    # became its own turn once the first was done, with its own answer.
    late = conversation.status("req-00000022")
    assert late["state"] == "answered" and late["answer"] == "ANSWER TO and in euros"


def test_a_new_chat_starts_the_snapshot_empty_and_the_old_one_moves_to_the_chat_list(monkeypatch, tmp_path):
    _main(monkeypatch)
    from backend.brain import history_refill
    from backend.mobile import conversation
    from backend.storage import exchanges
    monkeypatch.setattr(history_refill, "_STATE_FILE", tmp_path / "history.json")
    exchanges.record_exchange(exchange_id="ex-old", session_id="s-old", channel="mobile",
                              timestamp=time.time() - 120, user_text="old chat", assistant_text="old answer",
                              significance=0.1, high=False)
    assert [e["user"] for e in conversation.snapshot()["exchanges"]] == ["old chat"]
    history_refill.mark_new("mobile")
    assert conversation.snapshot()["exchanges"] == []
    assert [c["session_id"] for c in conversation.chats()] == ["s-old"]
    assert [e["user"] for e in conversation.chat_history("s-old")] == ["old chat"]


def test_a_conversation_quiet_past_the_session_gap_is_not_the_current_one(monkeypatch):
    _main(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import exchanges
    exchanges.record_exchange(exchange_id="ex-stale", session_id="s-stale", channel="mobile",
                              timestamp=time.time() - 3 * 3600, user_text="yesterday", assistant_text="x",
                              significance=0.1, high=False)
    assert conversation.snapshot()["exchanges"] == []


def test_the_tools_a_turn_used_reach_the_phone_live_and_stay_in_its_status(monkeypatch):
    """Sam, 14 October: the tool lines vanished when the final answer replaced the draft."""
    import backend.main as M
    from backend.mobile import conversation, events
    from backend.storage import operations

    def answer(text):
        operations.record_operation(run_id=M._running["run_id"], tool="web_browse", outcome="ok",
                                    started_at=time.time(), args={"query": "NVDA close today"})
        return "NVDA closed at $181."
    M, _ = _main(monkeypatch, answer=answer, events_to_send=[
        {"type": "tool_call", "tool": "web_browse", "input": {"query": "NVDA close today", "body": "x" * 5000}}])
    conversation.submit("req-00000030", "where did NVDA close?")
    run(M._admit_pending())
    live = [e for e in events.HUB.since(events.HUB.epoch, 0) if e["type"] == "tool_call"][0]
    assert live["tool"] == "web_browse" and live["detail"] == "NVDA close today"
    assert "input" not in live                            # never the full arguments
    tools = conversation.status("req-00000030")["tools"]
    assert tools == [{"tool": "web_browse", "outcome": "ok", "detail": "NVDA close today"}]


def test_tool_detail_is_short_and_never_the_whole_input():
    from backend.mobile.conversation import tool_detail
    assert tool_detail({"action": "search", "query": "Sarah"}) == "search · Sarah"
    assert tool_detail({"content": "secret note " * 50}) == ""
    assert len(tool_detail({"query": "x" * 500})) == 100
    assert tool_detail("{'ticker': 'TTWO', 'action': 'show'}") == "show · TTWO"
    assert tool_detail("not a dict repr") == "not a dict repr"


# ------------------------------------------------------------------ slice 6: the orb's mood

def _set_mood(monkeypatch, tone, intensity):
    from backend.brain import mood
    state = None if tone is None else mood.MoodState(tone=tone, intensity=intensity, trajectory="stable",
                                                       turn_count=3, last_saved=0.0)
    monkeypatch.setattr(mood, "get_mood", lambda: state)


def test_the_snapshot_and_each_finished_turn_carry_the_mood(monkeypatch):
    M, _ = _main(monkeypatch)
    from backend.mobile import conversation, events
    _set_mood(monkeypatch, "curious", 0.62)
    conversation.submit("req-00000061", "hello")
    run(M._admit_pending())
    assert events.HUB.since(events.HUB.epoch, 0)[-1]["mood"] == {"tone": "curious", "intensity": 0.62}
    assert conversation.snapshot()["mood"] == {"tone": "curious", "intensity": 0.62}


def test_the_mood_is_only_tone_and_a_bounded_intensity(monkeypatch):
    from backend.mobile import conversation
    _set_mood(monkeypatch, "restless", 1.7)
    assert conversation.mood() == {"tone": "restless", "intensity": 1.0}
    _set_mood(monkeypatch, None, 0)
    assert conversation.mood() is None


def test_a_broken_mood_engine_never_breaks_a_reply(monkeypatch):
    from backend.brain import mood
    def boom():
        raise RuntimeError("qdrant down")
    monkeypatch.setattr(mood, "get_mood", boom)
    M, _ = _main(monkeypatch)
    from backend.mobile import conversation, events
    conversation.submit("req-00000062", "hello")
    run(M._admit_pending())
    final = events.HUB.since(events.HUB.epoch, 0)[-1]
    assert final["answer"] == "ANSWER TO hello" and final["mood"] is None
    assert conversation.snapshot()["mood"] is None
