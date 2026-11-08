"""A heard phone turn speaks as it writes (live_speech.py).

Each sentence JARVIS writes becomes a speech_clip event (after the claim-pattern safety
check) and starts being synthesized at once; a short line covers the first tool call when
nothing has been said; the checked final answer's remainder is spoken at the end, then
speech_end. All of it is published before run_finished. The synthesizer and the HUB are
faked: no voice is called.
"""
import asyncio
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile import conversation, live_speech, routes, spoken
from backend.mobile.events import HUB

TOKEN = "mobile-" + "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fake_voice(monkeypatch):
    calls = []

    async def clip_audio(rev, clip):
        calls.append((rev, clip["seq"], clip["text"], clip["lang"]))
        return b"MP3:" + clip["text"].encode(), "chirp"
    monkeypatch.setattr(spoken, "clip_audio", clip_audio)
    live_speech._turns.clear()
    yield calls
    live_speech._turns.clear()


def make(user_text="what is on today"):
    events = []
    return live_speech.LiveSpeech("req-0001", user_text, publish=events.append), events


def clip_events(events):
    return [e for e in events if e["type"] == "speech_clip"]


async def feed(ls, *items):
    q = asyncio.Queue()
    for i in items:
        q.put_nowait(i)
    q.put_nowait(None)
    await ls.run(q)
    await asyncio.sleep(0)             # let the synthesis tasks start


# ------------------------------------------------------------------ sentences as they come

def test_each_sentence_becomes_a_clip_in_order_with_markdown_gone_and_its_language(fake_voice):
    ls, events = make()

    async def go():
        await feed(ls, "Your **meeting** is at nine.", "Ensuite, tu as le notaire à quatorze heures.")
    run(go())
    clips = clip_events(events)
    assert [(c["clip"], c["text"], c["lang"]) for c in clips] == [
        (0, "Your meeting is at nine.", "en"),
        (1, "Ensuite, tu as le notaire à quatorze heures.", "fr")]
    assert all(c["request_id"] == "req-0001" for c in clips)
    # synthesis started per clip without waiting for anything else
    assert [(c[0], c[1]) for c in fake_voice] == [("live-req-0001", 0), ("live-req-0001", 1)]


def test_the_clip_bytes_come_back_and_a_synthesis_in_flight_is_waited_for(monkeypatch):
    gate = {}

    async def slow(rev, clip):
        await gate["open"].wait()
        return b"SLOW", "chirp"
    monkeypatch.setattr(spoken, "clip_audio", slow)
    ls, _ = make()

    async def go():
        gate["open"] = asyncio.Event()
        await feed(ls, "Your meeting is at nine.")
        waiting = asyncio.create_task(ls.clip_bytes(0))
        await asyncio.sleep(0.01)
        assert not waiting.done()
        gate["open"].set()
        return await waiting
    assert run(go()) == (b"SLOW", "chirp")


def test_an_unknown_clip_is_a_key_error():
    ls, _ = make()
    with pytest.raises(KeyError):
        run(ls.clip_bytes(3))


def test_a_clip_that_never_finishes_gives_up_after_the_wait(monkeypatch):
    async def never(rev, clip):
        await asyncio.sleep(3600)
    monkeypatch.setattr(spoken, "clip_audio", never)
    monkeypatch.setattr(live_speech, "WAIT_S", 0.05)
    ls, _ = make()

    async def go():
        await feed(ls, "Your meeting is at nine.")
        with pytest.raises(asyncio.TimeoutError):
            await ls.clip_bytes(0)
    run(go())


# ------------------------------------------------------------------ held sentences

def test_a_claim_sentence_is_held_with_everything_after_it(fake_voice):
    ls, events = make()

    async def go():
        await feed(ls, "Let me check.", "I've set the reminder for 9.", "It is at nine tonight.")
    run(go())
    assert [c["text"] for c in clip_events(events)] == ["Let me check."]
    assert len(fake_voice) == 1


def test_the_held_sentence_and_what_follows_are_spoken_at_finish():
    ls, events = make()

    async def go():
        await feed(ls, "Let me check.", "I've set the reminder for 9.", "It is at nine tonight.")
        ls.finish("I've set the reminder for 9. It is at nine tonight.", True)
    run(go())
    clips = clip_events(events)
    # "Let me check." is not in the final answer: it does not block the remainder
    assert [(c["clip"], c["text"]) for c in clips] == [
        (0, "Let me check."), (1, "I've set the reminder for 9. It is at nine tonight.")]
    assert events[-1] == {"type": "speech_end", "request_id": "req-0001", "clips": 2}


# ------------------------------------------------------------------ the tool filler

@pytest.mark.parametrize("user_text,tool,expected", [
    ("what is on today", "web_browse", live_speech.FILLERS["web_browse"][0]),
    ("qu'est-ce que j'ai demain, et pour le notaire", "web_browse", live_speech.FILLERS["web_browse"][1]),
    ("what is on today", "some_unknown_tool", live_speech.DEFAULT_FILLER[0]),
])
def test_the_first_tool_call_with_nothing_said_gets_one_filler(user_text, tool, expected):
    ls, events = make(user_text)

    async def go():
        ls.on_event({"type": "tool_call", "tool": tool, "input": {}})
        ls.on_event({"type": "tool_call", "tool": tool, "input": {}})      # never a second one
    run(go())
    clips = clip_events(events)
    assert [c["text"] for c in clips] == [expected]


def test_no_filler_when_something_was_already_said():
    ls, events = make()

    async def go():
        await feed(ls, "Let me check.")
        ls.on_event({"type": "tool_call", "tool": "web_browse", "input": {}})
    run(go())
    assert [c["text"] for c in clip_events(events)] == ["Let me check."]


def test_other_events_are_not_fillers():
    ls, events = make()

    async def go():
        ls.on_event({"type": "thinking", "text": "hmm"})
    run(go())
    assert events == []


# ------------------------------------------------------------------ finish

def test_finish_speaks_what_was_not_said_from_the_first_unsaid_sentence():
    ls, events = make()

    async def go():
        await feed(ls, "Your meeting is at nine!")
        ls.finish("Your meeting is at nine. Bring the lease.", True)
    run(go())
    assert [(c["clip"], c["text"]) for c in clip_events(events)] == [
        (0, "Your meeting is at nine!"), (1, "Bring the lease.")]
    assert events[-1]["type"] == "speech_end" and events[-1]["clips"] == 2


def test_everything_already_said_leaves_only_the_end():
    ls, events = make()

    async def go():
        await feed(ls, "Your meeting is at nine.", "Bring the lease.")
        ls.finish("Your meeting is at nine. Bring the lease.", True)
    run(go())
    assert len(clip_events(events)) == 2
    assert events[-1] == {"type": "speech_end", "request_id": "req-0001", "clips": 2}


def test_finish_reads_the_checked_answer_not_the_draft():
    ls, events = make()

    async def go():
        await feed(ls, "Let me check.")
        ls.finish("**Done.** Nothing was saved this turn.\n\n· followed: skill", True)
    run(go())
    assert [c["text"] for c in clip_events(events)] == [
        "Let me check.", "Done. Nothing was saved this turn."]


def test_a_turn_that_did_not_complete_publishes_only_the_end():
    ls, events = make()

    async def go():
        await feed(ls, "Your meeting is at nine.")
        ls.finish(None, False)
    run(go())
    assert [e["type"] for e in events] == ["speech_clip", "speech_end"]
    assert events[-1]["clips"] == 1


def test_the_total_spoken_live_is_capped_with_the_pointer(monkeypatch):
    monkeypatch.setattr(spoken, "READ_MAX_CHARS", 100)
    ls, events = make()
    s1 = "This is the first long sentence of the answer, about sixty."
    s2 = "This is the second long sentence of the answer, about sixty."
    s3 = "This is the third one."

    async def go():
        await feed(ls, s1, s2, s3)
        ls.finish(f"{s1} {s2} {s3}", True)
    run(go())
    assert [c["text"] for c in clip_events(events)] == [s1, spoken.REST_ON_SCREEN]
    assert events[-1]["clips"] == 2


def test_finish_is_idempotent():
    ls, events = make()

    async def go():
        ls.finish("Hello there.", True)
        ls.finish("Hello there.", True)
    run(go())
    assert [e["type"] for e in events].count("speech_end") == 1


# ------------------------------------------------------------------ registry and sink

def test_the_registry_keeps_the_last_eight_turns():
    for i in range(10):
        live_speech.start(f"req-{i:04d}", "hi")
    assert live_speech.get("req-0000") is None and live_speech.get("req-0001") is None
    assert live_speech.get("req-0009") is not None
    assert len(live_speech._turns) == 8


def test_the_phone_sink_hands_tool_calls_to_the_turn():
    live_speech.start("req-0001", "what is on today")
    before = HUB.seq

    async def base(msg):
        pass

    async def go():
        send = conversation.sink(base, "run1", "req-0001")
        await send({"type": "tool_call", "tool": "web_browse", "input": {"query": "x"}})
    run(go())
    mine = [e for e in HUB._events if e["seq"] > before]
    assert [e["type"] for e in mine] == ["speech_clip", "tool_call"] or \
        [e["type"] for e in mine] == ["tool_call", "speech_clip"]


# ------------------------------------------------------------------ the route

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class _Turn:
    def __init__(self, result):
        self.result = result

    async def clip_bytes(self, seq):
        if self.result == "missing":
            raise KeyError(seq)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _route(client, result, rid="req-0001", seq=0):
    live_speech._turns.clear()
    if result is not None:
        live_speech._turns[rid] = _Turn(result)
    return client.get("/mobile/v1/speech/live", params={"request_id": rid, "seq": seq}, headers=AUTH)


def test_the_route_serves_a_clip(client):
    r = _route(client, (b"MP3DATA", "chirp"))
    assert r.status_code == 200 and r.content == b"MP3DATA"
    assert r.headers["content-type"] == "audio/mpeg" and r.headers["x-engine"] == "chirp"
    assert "x-synth-ms" in r.headers


def test_the_route_needs_the_token(client):
    live_speech._turns["req-0001"] = _Turn((b"x", "chirp"))
    assert client.get("/mobile/v1/speech/live", params={"request_id": "req-0001", "seq": 0}).status_code == 401


def test_the_route_404s_unknown_turns_and_clips(client):
    assert _route(client, None).status_code == 404
    assert _route(client, "missing").status_code == 404


def test_the_route_503_over_allowance_and_502_voice_failed(client):
    from backend.mobile import speech
    assert _route(client, spoken.OverAllowance("used up")).status_code == 503
    assert _route(client, speech.SpeechError("chirp 500")).status_code == 502
    assert _route(client, asyncio.TimeoutError()).status_code == 502


# ------------------------------------------------------------------ through the real turn

def _turn_env(monkeypatch, sentences, answer, steps=()):
    import backend.main as M

    async def fake_chat(text, history, send, sentence_queue=None, channel="local",
                        history_bridge="", history_since=None):
        for i, s in enumerate(sentences):
            await sentence_queue.put(s)
            await asyncio.sleep(0)
            if i == 0:                       # narration first, then the tool runs
                for step in steps:
                    await send(step)
        return answer
    monkeypatch.setattr(M, "chat", fake_chat)
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    for ch in ("local", "mobile", "discord"):
        monkeypatch.setitem(M.conversation_histories, ch, [])
        monkeypatch.setitem(M.history_times, ch, [])
    return M


def _phone_turn(M, heard, channel="mobile", text="what is on today"):
    from backend.brain import steering, turn_input
    before = HUB.seq

    async def go():
        token = turn_input.spoken.set(heard)
        try:
            with steering.turn("mobile", None, "mobile:req-turn-0001"):
                return await M._execute_turn_once(text, channel, False)
        finally:
            turn_input.spoken.reset(token)
    out = run(go())
    return out, [e for e in HUB._events if e["seq"] > before]


def test_a_heard_phone_turn_publishes_all_speech_before_run_finished(monkeypatch):
    M = _turn_env(monkeypatch, ["Let me check.", "It is at nine."], "It is at nine. Bring the lease.",
                  steps=[{"type": "tool_call", "tool": "web_browse", "input": {}}])
    out, events = _phone_turn(M, True)
    assert out == "It is at nine. Bring the lease."
    kinds = [e["type"] for e in events]
    assert kinds.count("run_finished") == 1 and kinds.count("speech_end") == 1
    last_speech = max(i for i, k in enumerate(kinds) if k in ("speech_clip", "speech_end"))
    assert last_speech < kinds.index("run_finished")
    assert kinds[last_speech] == "speech_end"
    texts = [e["text"] for e in events if e["type"] == "speech_clip"]
    assert texts[0] == "Let me check." and "Bring the lease." in texts[-1]
    assert all(e["request_id"] == "req-turn-0001" for e in events if e["type"].startswith("speech"))


def test_a_stopped_heard_turn_still_ends_its_speech_before_run_finished(monkeypatch):
    import backend.main as M

    async def hang(text, history, send, sentence_queue=None, channel="local",
                   history_bridge="", history_since=None):
        await sentence_queue.put("Let me check.")
        raise RuntimeError("boom")
    monkeypatch.setattr(M, "chat", hang)
    monkeypatch.setattr(M, "_pipeline_lock", asyncio.Lock())
    monkeypatch.setitem(M.conversation_histories, "mobile", [])
    monkeypatch.setitem(M.history_times, "mobile", [])
    out, events = _phone_turn(M, True)
    kinds = [e["type"] for e in events]
    assert out is None
    assert kinds.index("speech_end") < kinds.index("run_finished")


def test_a_phone_turn_that_is_not_heard_has_no_speech_events(monkeypatch):
    M = _turn_env(monkeypatch, ["Let me check.", "It is at nine."], "It is at nine.",
                  steps=[{"type": "tool_call", "tool": "web_browse", "input": {}}])
    out, events = _phone_turn(M, False)
    assert out == "It is at nine."
    assert not [e for e in events if e["type"] in ("speech_clip", "speech_end")]
    assert [e for e in events if e["type"] == "run_finished"]


def test_a_desk_turn_is_exactly_as_before(monkeypatch):
    M = _turn_env(monkeypatch, ["Let me check.", "It is at nine."], "It is at nine.")
    out, events = _phone_turn(M, True, channel="local")
    assert out == "It is at nine."
    assert not [e for e in events if e["type"].startswith("speech")]
    assert live_speech._turns == {}


def test_main_runs_live_speech_only_for_a_heard_phone_turn():
    import backend.main as M
    src = inspect.getsource(M._execute_turn_once)
    assert "live_speech" in src and "_drain_sentence_queue(sentence_queue)" in src
    assert src.index("live.finish(") < src.index("mobile_conversation.finished(")


def test_the_clip_number_survives_the_hub_envelope():
    """HUB.publish overwrites "seq" with its own number, so the clip number is "clip"."""
    ls = live_speech.LiveSpeech("req-0001", "hi", publish=HUB.publish)
    before = HUB.seq

    async def go():
        await feed(ls, "Your meeting is at nine.", "Bring the lease.")
    run(go())
    clips = [e for e in HUB._events if e["seq"] > before and e["type"] == "speech_clip"]
    assert [c["clip"] for c in clips] == [0, 1]
    assert clips[1]["seq"] == clips[0]["seq"] + 1          # the envelope's own number
