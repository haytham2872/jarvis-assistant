"""LiveSession: cutting, transcribing and ending a streamed turn, with fake models."""
import asyncio

import numpy as np

from backend.audio import live
from backend.audio.live import LiveSession

SR = 16000


def tone(ms, amp=0.1):
    n = SR * ms // 1000
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * 220 * t) * amp * 32767 * 1.4).astype("<i2").tobytes()


def quiet(ms):
    return bytes(2 * (SR * ms // 1000))


def fake_vad_factory():
    return lambda frame: 1.0 if float(np.sqrt(np.mean(frame ** 2))) > 0.01 else 0.0


class Rig:
    def __init__(self, mode="conversation", p=0.9, vad=fake_vad_factory):
        self.sent, self.asr_lens, self.turn_calls = [], [], []
        self.warmed = 0
        self.p = p

        def asr(pcm):
            self.asr_lens.append(len(pcm))
            return f"seg{len(self.asr_lens)}"

        def turn_prob(pcm):
            self.turn_calls.append(len(pcm))
            return self.p

        async def send(msg):
            self.sent.append(msg)
        self.s = LiveSession(mode, send, vad_factory=vad, asr=asr,
                             warm=lambda: setattr(self, "warmed", self.warmed + 1),
                             turn_prob=turn_prob,
                             commit_after=lambda p: 250 if p >= 0.7 else 2000)

    async def play(self, script):
        """script: bytes (split in 100 ms chunks), 'end', 'cancel', or 'q' (let work settle)."""
        items = []
        for it in script:
            if isinstance(it, bytes):
                items += [it[i:i + 3200] for i in range(0, len(it), 3200)]
            else:
                items.append(it)
        items.reverse()

        async def receive():
            while items:
                it = items.pop()
                if it == "q":
                    await self.s.quiet()
                    continue
                return it
            await asyncio.sleep(3)          # the phone is quiet: only a commit can end this
            return None
        return await self.s.run(receive)

    def heard(self):
        return [m["text"] for m in self.sent if m["type"] == "heard"]


def go(rig, script):
    return asyncio.run(rig.play(script))


def test_two_segments_heard_after_each_and_joined():
    r = Rig("note")
    res = go(r, [quiet(200), tone(1000), quiet(300), tone(1000), "end"])
    assert r.heard() == ["seg1", "seg1 seg2"]
    assert (res.outcome, res.text) == ("turn", "seg1 seg2")
    assert len(r.asr_lens) == 2 and r.warmed == 1


def test_preroll_and_postpad():
    r = Rig("note")
    go(r, [quiet(1000), tone(1000), quiet(1000), "end"])
    assert len(r.asr_lens) == 1
    # 300 ms pre-roll + about 1000 ms voiced + 100 ms post-pad
    assert abs(r.asr_lens[0] - SR * 1.4) <= 2 * live.FRAME


def test_short_pause_does_not_cut():
    r = Rig("note")
    go(r, [tone(500), quiet(200), tone(500), "end"])
    assert len(r.asr_lens) == 1


def test_long_speech_is_cut_at_quietest_frame_without_loss():
    dip = 7 * SR // live.FRAME

    def factory():
        calls = [0]

        def vad(frame):
            i = calls[0]
            calls[0] += 1
            return 0.4 if i == dip else 1.0
        return vad
    r = Rig("note", vad=factory)
    go(r, [tone(10000), "end"])
    assert len(r.asr_lens) == 2
    assert abs(r.asr_lens[0] - dip * live.FRAME) <= live.FRAME     # cut at the dipped frame
    assert sum(r.asr_lens) == 10 * SR // live.FRAME * live.FRAME   # nothing lost, nothing twice


def test_conversation_asks_once_and_commits_on_high_probability():
    r = Rig(p=0.9)
    res = go(r, [tone(1000), quiet(600)])
    assert res.outcome == "turn" and res.text == "seg1" and r.s._reason == "silence"
    assert len(r.turn_calls) == 1 and r.turn_calls[0] >= SR


def test_turn_check_runs_after_the_last_phrase_is_transcribed():
    """Mutation check: the detector must wait for pending transcription (2 cores: the two
    would otherwise fight, and the last phrase is what the commit needs anyway)."""
    import time
    order = []
    r = Rig(p=0.9)

    def slow_asr(pcm):
        time.sleep(0.3)
        order.append("asr")
        return "seg"

    def turn_prob(pcm):
        order.append("turn")
        return 0.9
    r.s._asr, r.s._turn_prob = slow_asr, turn_prob
    go(r, [tone(1000), quiet(600)])
    assert order == ["asr", "turn"]


def test_low_probability_waits_two_seconds():
    r = Rig(p=0.1)
    go(r, [tone(1000), quiet(1000), "q", "end"])
    assert r.s._reason == "end"                                    # 1 s of silence did not commit
    r2 = Rig(p=0.1)
    res = go(r2, [tone(1000), quiet(1000), "q", quiet(1100)])
    assert r2.s._reason == "silence" and res.text == "seg1"
    assert len(r2.turn_calls) == 1


def test_speech_resuming_cancels_commit_and_a_later_pause_asks_again():
    r = Rig(p=0.1)
    go(r, [tone(1000), quiet(500), "q", tone(800), quiet(500), "q", "end"])
    assert len(r.turn_calls) == 2 and r.s._reason == "end"


def test_commit_waits_for_every_transcript_and_keeps_order():
    r = Rig(p=0.9)
    res = go(r, [tone(500), quiet(300), tone(500), quiet(400)])
    assert (res.outcome, res.text) == ("turn", "seg1 seg2")


def test_note_mode_ignores_silence_and_flushes_open_segment_on_end():
    r = Rig("note")
    res = go(r, [tone(500), quiet(5000), "q", tone(400), "end"])
    assert r.s._reason == "end" and res.text == "seg1 seg2"
    assert not r.turn_calls


def test_end_in_conversation_commits_at_once():
    r = Rig(p=0.1)
    res = go(r, [tone(800), "end"])
    assert res.text == "seg1" and r.s._reason == "end"


def test_cancel_throws_it_away():
    r = Rig()
    res = go(r, [tone(800), "cancel"])
    assert res.outcome == "cancel" and not r.heard()


def test_no_speech_in_first_eight_seconds():
    r = Rig()
    res = go(r, [quiet(9000)])
    assert res.outcome == "no_speech" and not r.asr_lens


def test_note_mode_has_no_no_speech_and_empty_end_is_empty():
    r = Rig("note")
    assert go(r, [quiet(9000), "end"]).outcome == "empty"


def test_empty_transcript_is_empty():
    r = Rig()
    r.s._asr = lambda pcm: ""
    res = go(r, [tone(500), "end"])
    assert res.outcome == "empty" and not r.heard()


def test_total_cap_commits():
    r = Rig("note")
    res = go(r, [tone(1000)] * 125)
    assert r.s._reason == "cap" and res.outcome == "turn"
    assert r.s._n <= (live.MAX_TOTAL_S + 5) * SR


def test_odd_byte_counts_keep_the_leftover_byte():
    r = Rig("note")
    data = tone(1000)
    cuts = [0, 1, 3201, 3202, 9001, len(data)]
    go(r, [*[data[a:b] for a, b in zip(cuts, cuts[1:])], "end"])
    assert r.s._n == len(data) // 2 and len(r.asr_lens) == 1


def test_asr_failure_is_an_error():
    r = Rig("note")

    def boom(pcm):
        raise RuntimeError("model down")
    r.s._asr = boom
    res = go(r, [tone(500), "end"])
    assert res.outcome == "error" and "model down" in res.detail


def test_log_line_has_no_text(caplog):
    caplog.set_level("INFO", logger="backend.audio.live")
    r = Rig("note")
    go(r, [tone(500), "end"])
    line = [m for m in caplog.messages if m.startswith("mobile live voice:")][0]
    assert "seg1" not in line and '"segments": 1' in line and '"mode": "note"' in line
