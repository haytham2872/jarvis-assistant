"""M3: what the phone says aloud, and the routes that serve it. No real voice call is made:
Chirp is stubbed, the model summary is stubbed, the database is the per-test one."""
import asyncio
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.mobile import spoken
from backend.mobile.test_conversation import AUTH, _isolation, _main, run  # noqa: F401  (fixture)
from backend.mobile.test_routes import m4a
from backend.mobile.test_voice import heard  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _fresh_caches():
    spoken._plans.clear()
    spoken._audio.clear()
    spoken._inflight.clear()
    yield
    spoken._plans.clear()
    spoken._audio.clear()


# ------------------------------------------------------------------ Markdown to speech

def test_formatting_goes_and_links_are_read_by_their_text():
    said = spoken.spoken_text("Your **NVDA** is up. See [Reuters](https://reuters.com/x) or "
                              "https://www.ft.com/content/abc and `AAPL`.")
    assert said == "Your NVDA is up. See Reuters or a link to ft.com and AAPL."


def test_code_and_tables_stay_on_screen_with_one_sentence_each():
    said = spoken.spoken_text("Here:\n```py\nprint(1)\n```\nand\n```\nx\n```\n"
                              "| a | b |\n|---|---|\n| 1 | 2 |\nDone.")
    assert said.count(spoken.ON_SCREEN_CODE) == 1 and said.count(spoken.ON_SCREEN_TABLE) == 1
    assert "print" not in said and "|" not in said and said.endswith("Done.")


def test_headings_and_bullets_become_sentences_and_the_footer_is_dropped():
    said = spoken.spoken_text("## Plan\n- gym at 6\n- call Samir\n\n· followed: stock-research")
    assert said == "Plan. gym at 6. call Samir."


def test_an_arrow_is_a_pause_not_a_word():
    assert spoken.spoken_text("Up 3% → still below cost") == "Up 3%, still below cost"


# ------------------------------------------------------------------ sentences, clips, language

def test_abbreviations_and_decimals_do_not_end_a_sentence():
    assert spoken.sentences("Dr. Smith paid $181.40 at 3 p.m. today. Then he left.") == [
        "Dr. Smith paid $181.40 at 3 p.m. today.", "Then he left."]


@pytest.mark.parametrize("text, lang", [
    ("Ton rendez-vous chez le notaire est jeudi à quatorze heures.", "fr"),
    ("J'ai regardé ton portfolio : NVIDIA a pris trois pour cent aujourd'hui.", "fr"),
    ("Your meeting with Marc moved to Thursday at two.", "en"),
    ("OK.", "en"),
])
def test_language_of_a_sentence(text, lang):
    assert spoken.language(text) == lang


def test_clips_keep_every_word_start_short_and_never_mix_languages():
    text = ("Oui. Ton rendez-vous est jeudi à quatorze heures, chez le notaire. "
            "Your gym block stays at half past five. " + "It is a long sentence about nothing. " * 12)
    out = spoken.clips(text)
    assert " ".join(c["text"] for c in out) == text.strip()
    assert out[0]["lang"] == "fr" and len(out[0]["text"]) < 120
    assert all(len(c["text"]) <= spoken.CLIP_HARD_MAX for c in out)
    assert [c["seq"] for c in out] == list(range(len(out)))
    for c in out:
        assert spoken.language(c["text"], c["lang"]) == c["lang"]


def test_a_change_of_language_mid_answer_starts_a_new_clip():
    out = spoken.clips("Your call is at ten. Bring the lease and your ID. "
                       "Ensuite, tu as le notaire à quatorze heures.")
    assert [c["lang"] for c in out] == ["en", "fr"]
    assert out[1]["text"] == "Ensuite, tu as le notaire à quatorze heures."


def test_a_very_long_sentence_is_cut_at_commas():
    sentence = ", ".join(["the figure rose again"] * 40) + "."
    out = spoken.clips(sentence)
    assert len(out) > 1 and all(len(c["text"]) <= spoken.CLIP_HARD_MAX for c in out)


def test_each_language_has_its_fenrir_and_both_can_be_changed(monkeypatch):
    assert spoken.voice_for("fr") == "fr-FR-Chirp3-HD-Fenrir"
    assert spoken.voice_for("en") == "en-US-Chirp3-HD-Fenrir"
    monkeypatch.setenv("MOBILE_TTS_VOICE_FR", "fr-FR-Chirp3-HD-Charon")
    assert spoken.voice_for("fr") == "fr-FR-Chirp3-HD-Charon"


# ------------------------------------------------------------------ the plan of an answer

def _stub_llm(monkeypatch, reply):
    from backend.brain import research
    calls = []

    async def fake(prompt, max_tokens=0, **kw):
        calls.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(research, "_llm", fake)
    return calls


def test_a_short_answer_is_read_in_full_without_a_model_call(monkeypatch):
    calls = _stub_llm(monkeypatch, "never")
    p = run(spoken.plan("req:x", "It is 9:00. Your call is at ten."))
    assert not p["summarised"] and calls == []
    assert " ".join(c["text"] for c in p["clips"]) == "It is 9:00. Your call is at ten."


def test_a_long_answer_is_read_word_for_word_never_summarised(monkeypatch):
    """Sam, 15 October: a 16-step answer was read as a model summary he could not see on
    screen. Every word heard is the answer's own, and no model is called."""
    calls = _stub_llm(monkeypatch, "never")
    answer = ("NVDA closed at 181.40 dollars, up three percent. " * 60).strip()
    assert 1500 < len(answer) < spoken.READ_MAX_CHARS   # longer than the old summary limit
    p = run(spoken.plan("req:x", answer))
    said = " ".join(c["text"] for c in p["clips"])
    assert calls == [] and not p["summarised"]
    assert said == answer


def test_a_very_long_answer_reads_its_opening_then_points_at_the_screen(monkeypatch):
    calls = _stub_llm(monkeypatch, "never")
    sentence = "A long sentence about the stock and what the filing says. "
    answer = sentence * 120
    p = run(spoken.plan("req:x", answer))
    said = " ".join(c["text"] for c in p["clips"])
    assert calls == [] and p["summarised"]
    assert said.endswith(spoken.REST_ON_SCREEN)
    body = said[: -len(spoken.REST_ON_SCREEN)].strip()
    assert answer.startswith(body) and len(body) <= spoken.READ_MAX_CHARS
    assert len(body) > spoken.READ_MAX_CHARS - len(sentence)      # as much as fits, whole sentences
    assert run(spoken.plan("req:x", answer)) is p                  # cached


# ------------------------------------------------------------------ audio and the allowance

def _stub_chirp(monkeypatch, fail=None):
    from backend.mobile import speech
    monkeypatch.setenv("GOOGLE_TTS_API_KEY", "k")
    calls = []

    async def fake(text, voice=""):
        calls.append((text, voice))
        await asyncio.sleep(0.01)
        if fail:
            raise speech.SpeechError(fail)
        return b"MP3:" + text.encode()
    monkeypatch.setattr(speech, "chirp_mp3", fake)
    return calls


def test_a_clip_is_counted_against_the_month_and_cached(monkeypatch):
    from backend.storage import speech_usage
    calls = _stub_chirp(monkeypatch)
    clip = {"seq": 0, "text": "Bonjour Sam, ton rendez-vous est jeudi.", "lang": "fr"}
    audio, engine = run(spoken.clip_audio("rev1", clip))
    assert audio.startswith(b"MP3:") and engine == "chirp"
    assert calls == [(clip["text"], "fr-FR-Chirp3-HD-Fenrir")]
    assert speech_usage.used() == len(clip["text"])
    run(spoken.clip_audio("rev1", clip))
    assert len(calls) == 1 and speech_usage.used() == len(clip["text"])


def test_two_requests_for_one_clip_share_one_call(monkeypatch):
    calls = _stub_chirp(monkeypatch)
    clip = {"seq": 2, "text": "Same clip.", "lang": "en"}

    async def both():
        return await asyncio.gather(spoken.clip_audio("r", clip), spoken.clip_audio("r", clip))
    a, b = run(both())
    assert a == b and len(calls) == 1


def test_a_failed_call_is_refunded(monkeypatch):
    from backend.mobile import speech
    from backend.storage import speech_usage
    _stub_chirp(monkeypatch, fail="Chirp answered 500")
    with pytest.raises(speech.SpeechError):
        run(spoken.clip_audio("r", {"seq": 0, "text": "Hello there.", "lang": "en"}))
    assert speech_usage.used() == 0


def test_past_the_monthly_allowance_chirp_is_not_called(monkeypatch):
    calls = _stub_chirp(monkeypatch)
    monkeypatch.setenv("MOBILE_TTS_MONTHLY_CHARS", "20")
    run(spoken.clip_audio("r", {"seq": 0, "text": "Fifteen chars!!", "lang": "en"}))
    with pytest.raises(spoken.OverAllowance):
        run(spoken.clip_audio("r", {"seq": 1, "text": "Ten chars.", "lang": "en"}))
    assert len(calls) == 1


def test_the_allowance_is_per_paris_month_and_refused_without_storage(monkeypatch):
    from datetime import datetime, timezone
    from backend.storage import operations, speech_usage
    oct31 = datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc)     # 23:30 in Paris
    nov1 = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)      # 00:30 on 1 November
    assert speech_usage.reserve(10, 10, oct31) and not speech_usage.reserve(1, 10, oct31)
    assert speech_usage.reserve(10, 10, nov1)
    monkeypatch.setattr(operations, "_ensure", lambda: None)
    assert speech_usage.reserve(1, 10**6) is False


# ------------------------------------------------------------------ routes

def _client(monkeypatch):
    M, calls = _main(monkeypatch, answer=lambda text: "Your call is at **ten**. Bring the lease.")
    from backend.mobile import routes
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), M


def _answered(c, M, rid="req-s0000001", text="when is my call"):
    from backend.mobile import conversation
    conversation.submit(rid, text)
    run(M._admit_pending())
    return rid


def test_an_answered_request_is_spoken_from_its_stored_answer(monkeypatch):
    c, M = _client(monkeypatch)
    _stub_chirp(monkeypatch)
    rid = _answered(c, M)
    plan = c.get("/mobile/v1/speech/plan", params={"source": f"req:{rid}"}, headers=AUTH).json()
    assert " ".join(x["text"] for x in plan["clips"]) == "Your call is at ten. Bring the lease."
    r = c.get("/mobile/v1/speech/clip", headers=AUTH,
              params={"source": f"req:{rid}", "revision": plan["revision"], "seq": 0})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.content.startswith(b"MP3:") and r.headers["x-engine"] == "chirp"


def test_an_unanswered_unknown_or_foreign_source_is_refused(monkeypatch):
    c, M = _client(monkeypatch)
    from backend.mobile import conversation
    from backend.storage import exchanges
    conversation.submit("req-s0000002", "still waiting")              # never admitted
    get = lambda s: c.get("/mobile/v1/speech/plan", params={"source": s}, headers=AUTH).status_code
    assert get("req:req-s0000002") == 409
    assert get("req:req-s9999999") == 404
    exchanges.record_exchange(exchange_id="x-discord", session_id="s", channel="discord",
                              timestamp=1.0, user_text="hi", assistant_text="Discord answer",
                              significance=0.1, high=False)
    assert get("ex:x-discord") == 404                                  # not a phone exchange
    assert get("anything") == 400


def test_a_phone_exchange_in_history_can_be_replayed(monkeypatch):
    c, M = _client(monkeypatch)
    from backend.storage import exchanges
    exchanges.record_exchange(exchange_id="x-phone", session_id="s", channel="mobile",
                              timestamp=1.0, user_text="hi", assistant_text="Hello, Sam.",
                              significance=0.1, high=False)
    plan = c.get("/mobile/v1/speech/plan", params={"source": "ex:x-phone"}, headers=AUTH).json()
    assert plan["clips"][0]["text"] == "Hello, Sam."


def test_a_stale_revision_an_exhausted_month_and_a_failed_voice(monkeypatch):
    c, M = _client(monkeypatch)
    rid = _answered(c, M)
    plan = c.get("/mobile/v1/speech/plan", params={"source": f"req:{rid}"}, headers=AUTH).json()
    clip = lambda rev, seq=0: c.get("/mobile/v1/speech/clip", headers=AUTH,
                                    params={"source": f"req:{rid}", "revision": rev, "seq": seq})
    assert clip("0" * 16).status_code == 409
    assert clip(plan["revision"], 99).status_code == 404
    _stub_chirp(monkeypatch, fail="Chirp answered 500")
    assert clip(plan["revision"]).status_code == 502
    _stub_chirp(monkeypatch)
    monkeypatch.setenv("MOBILE_TTS_MONTHLY_CHARS", "3")
    assert clip(plan["revision"]).status_code == 503


def test_the_speech_routes_need_the_token(monkeypatch):
    c, M = _client(monkeypatch)
    assert c.get("/mobile/v1/speech/plan", params={"source": "req:abcdefgh"}).status_code == 401
    assert c.get("/mobile/v1/speech/clip", params={"source": "req:abcdefgh", "revision": "x",
                                                   "seq": 0}).status_code == 401


# ------------------------------------------------------------------ the turn knows

def test_a_voice_message_the_phone_will_speak_tells_the_turn(monkeypatch):
    from backend.brain import turn_input
    from backend.mobile import conversation
    from backend.storage import inbox
    M, _ = _main(monkeypatch)
    seen = []

    async def chat(text, history, send, sentence_queue=None, channel="local", **kw):
        seen.append((turn_input.voice.get(), turn_input.spoken.get()))
        return "ok"
    monkeypatch.setattr(M, "chat", chat)
    conversation.submit("req-p0000001", "spoken one", "voice", spoken=True)
    conversation.submit("req-p0000002", "typed one")
    run(M._admit_pending())
    assert inbox.get("mobile:req-p0000001")["context"]["spoken"] is True
    assert seen == [(True, True), (False, False)]


def test_the_prompt_asks_for_a_spoken_reply_only_when_it_will_be_spoken():
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert 'channel == "mobile" and _turn_input.spoken.get()' in src
    assert "read aloud" in engine._SPOKEN_NOTE


def test_the_upload_carries_the_wish_to_hear_the_reply(monkeypatch, heard):
    from backend.storage import inbox
    c, M = _client(monkeypatch)
    for rid, query in (("req-u0000001", "&spoken=1"), ("req-u0000002", "")):
        r = c.post(f"/mobile/v1/recordings?request_id={rid}{query}", content=m4a(1.0),
                   headers={**AUTH, "Content-Type": "audio/mp4"})
        assert r.status_code == 202, r.text
    assert inbox.get("mobile:req-u0000001")["context"]["spoken"] is True
    assert inbox.get("mobile:req-u0000002")["context"]["spoken"] is False
