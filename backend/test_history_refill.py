"""The live history survives a restart (backend/brain/history_refill.py).

Measured on the server (2026-10-11): 46 restarts in 30 days, and 12 times a restart cut a
conversation that was still going, so his next message arrived with an empty history.
"""
import inspect
import time

from backend.brain import history_refill
from backend.storage import exchanges

GAP = 2 * 3600


def _row(ts, user="u", assistant="a"):
    return {"timestamp": ts, "user_text": user, "assistant_text": assistant}


def test_restores_the_conversation_still_going_oldest_first():
    now = 10_000.0
    rows = [_row(now - 60, "third"), _row(now - 600, "second"), _row(now - 1200, "first")]
    out = history_refill.conversation_to_restore(rows, now, GAP, None, 40)
    assert [r["user_text"] for r in out] == ["first", "second", "third"]


def test_stops_at_a_gap_of_a_session():
    now = 100_000.0
    rows = [_row(now - 60, "today"), _row(now - 60 - GAP, "yesterday")]
    out = history_refill.conversation_to_restore(rows, now, GAP, None, 40)
    assert [r["user_text"] for r in out] == ["today"]


def test_a_conversation_longer_than_the_gap_comes_back_whole():
    # Messages 50 minutes apart over 2.5 hours: the gap is between messages, not from now.
    now = 100_000.0
    rows = [_row(now - 60 - 3000 * i, f"m{i}") for i in range(4)]
    out = history_refill.conversation_to_restore(rows, now, GAP, None, 40)
    assert [r["user_text"] for r in out] == ["m3", "m2", "m1", "m0"]


def test_nothing_when_the_last_message_is_older_than_the_gap():
    now = 100_000.0
    assert history_refill.conversation_to_restore([_row(now - GAP - 1)], now, GAP, None, 40) == []


def test_never_crosses_new():
    now = 10_000.0
    rows = [_row(now - 60, "after"), _row(now - 600, "before")]
    out = history_refill.conversation_to_restore(rows, now, GAP, now - 300, 40)
    assert [r["user_text"] for r in out] == ["after"]


def test_stops_at_a_row_without_full_text():
    now = 10_000.0
    rows = [_row(now - 60, "full"), _row(now - 120, "", ""), _row(now - 180, "older")]
    out = history_refill.conversation_to_restore(rows, now, GAP, None, 40)
    assert [r["user_text"] for r in out] == ["full"]


def test_keeps_the_newest_when_capped():
    now = 10_000.0
    rows = [_row(now - 10 * i, f"m{i}") for i in range(1, 6)]     # newest first
    out = history_refill.conversation_to_restore(rows, now, GAP, None, 3)
    assert [r["user_text"] for r in out] == ["m3", "m2", "m1"]


def _record(i, ts, channel="discord", session="s1"):
    assert exchanges.record_exchange(
        exchange_id=f"x{channel}{i}", session_id=session, channel=channel,
        user_text=f"user {i}", assistant_text=f"reply {i}",
        significance=0.3, high=False, timestamp=ts)


def test_restore_reads_the_store_for_one_channel():
    now = time.time()
    _record(1, now - 900)
    _record(2, now - 300)
    _record(3, now - 200, channel="local")
    out = history_refill.restore("discord", 40)
    assert [r["user_text"] for r in out] == ["user 1", "user 2"]


def test_new_then_restart_restores_nothing_from_before():
    now = time.time()
    _record(1, now - 900)
    history_refill.mark_new("discord", when=now - 600)
    _record(2, now - 300, session="s2")
    assert [r["user_text"] for r in history_refill.restore("discord", 40)] == ["user 2"]


def test_main_refill_matches_what_the_live_history_held():
    import backend.main as m
    now = time.time()
    for i in range(3):
        _record(i, now - 600 + i * 60)
    saved = {k: dict(v) for k, v in (("h", m.conversation_histories), ("t", m.history_times))}
    try:
        m.conversation_histories.pop("discord", None)
        m.history_times.pop("discord", None)
        assert m._refill_history("discord") == 3
        refilled = m.conversation_histories["discord"]
        # The bridge dates a dropped turn from these; they are the stored times, not now.
        stamps = [now - 600 + i * 60 for i in range(3)]
        assert m.history_times["discord"] == [t for t in stamps for _ in (0, 1)]
        m.conversation_histories["discord"], m.history_times["discord"] = [], []
        for i in range(3):
            m._append_history("discord", f"user {i}", f"reply {i}")
        assert refilled == m.conversation_histories["discord"]
        m._refill_history("discord")
        assert len(m.history_times["discord"]) == len(m.conversation_histories["discord"]) == 6
    finally:
        m.conversation_histories.clear(); m.conversation_histories.update(saved["h"])
        m.history_times.clear(); m.history_times.update(saved["t"])


def test_discord_new_records_the_marker(monkeypatch):
    import backend.main as m
    now = time.time()
    _record(1, now - 60)
    monkeypatch.setattr(m, "force_new_session", lambda channel: None)   # not under test
    for d in (m.conversation_histories, m.history_bridges, m.history_times):
        monkeypatch.setitem(d, "discord", [])
    m.discord_new_session()
    assert history_refill.restore("discord", 40) == []


def test_lifespan_refills_before_discord_starts():
    import backend.main as m
    src = inspect.getsource(m.lifespan)
    assert "_refill_history(" in src
    first = src.index("_refill_history(")
    for later in ("discord_bridge.start()", "_admission_loop", "text_input_loop()"):
        assert first < src.index(later), later


def test_refill_failure_never_raises(monkeypatch):
    monkeypatch.setattr(exchanges, "recent_exchanges", lambda **kw: 1 / 0)
    assert history_refill.restore("discord", 40) == []
