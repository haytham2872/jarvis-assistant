"""A03 slice 8: compacted history cites when each exchange happened.

The roadmap asks compaction to "cite canonical exchanges" so the model can recover the
full wording. A date is the citation it can actually act on — `memory_timeline` takes
dates, and since A02 slice 1 the full exchange is durable in SQLite regardless of
significance. An opaque row id would be untypeable: no tool fetches an exchange by id.
"""
import time

import pytest

from backend.brain import history_bridge


def _pair(u, a):
    return [{"role": "user", "content": u}, {"role": "assistant", "content": a}]


def test_line_carries_the_timestamp_when_given():
    bridge = []
    ts = time.mktime((2026, 9, 21, 14, 3, 0, 0, 0, -1))
    history_bridge.record_dropped(bridge, _pair("hello", "hi"), [ts, ts])
    assert bridge[0].startswith("[2026-09-21 14:03] ")
    assert "User: hello | JARVIS: hi" in bridge[0]


def test_format_is_unchanged_without_timestamps():
    """A caller with no times must degrade to the previous behavior, not a wrong date."""
    bridge = []
    history_bridge.record_dropped(bridge, _pair("hello", "hi"))
    assert bridge == ["User: hello | JARVIS: hi"]


def test_unusable_timestamp_is_omitted_not_guessed():
    bridge = []
    history_bridge.record_dropped(bridge, _pair("a", "b"), [None, None])
    assert bridge[0] == "User: a | JARVIS: b"
    bridge2 = []
    history_bridge.record_dropped(bridge2, _pair("a", "b"), ["garbage", "garbage"])
    assert bridge2[0] == "User: a | JARVIS: b"


def test_short_times_list_does_not_raise():
    bridge = []
    history_bridge.record_dropped(bridge, _pair("a", "b") + _pair("c", "d"), [123.0])
    assert len(bridge) == 2


def test_each_pair_gets_its_own_timestamp():
    bridge = []
    t1 = time.mktime((2026, 9, 1, 9, 0, 0, 0, 0, -1))
    t2 = time.mktime((2026, 9, 2, 17, 30, 0, 0, 0, -1))
    history_bridge.record_dropped(
        bridge, _pair("first", "1") + _pair("second", "2"), [t1, t1, t2, t2])
    assert bridge[0].startswith("[2026-09-01 09:00]")
    assert bridge[1].startswith("[2026-09-02 17:30]")


def test_still_bounded():
    bridge = []
    now = time.time()
    for i in range(60):
        history_bridge.record_dropped(bridge, _pair(f"u{i}", f"a{i}"), [now, now])
    assert len(bridge) == history_bridge._MAX_LINES


# ------------------------------------------------------- wired into main.py
def test_append_history_keeps_times_parallel_to_history():
    import backend.main as m

    ch = "__compaction_test__"
    m.conversation_histories[ch] = []
    m.history_times[ch] = []
    try:
        for i in range(5):
            m._append_history(ch, f"user {i}", f"reply {i}")
        assert len(m.history_times[ch]) == len(m.conversation_histories[ch])
    finally:
        m.conversation_histories.pop(ch, None)
        m.history_times.pop(ch, None)
        m.history_bridges.pop(ch, None)


def test_dropped_turns_reach_the_bridge_with_dates():
    import backend.main as m

    ch = "__compaction_test2__"
    m.conversation_histories[ch] = []
    m.history_times[ch] = []
    m.history_bridges[ch] = []
    try:
        # Overflow the window so turns actually drop.
        for i in range(m._MAX_HISTORY_ENTRIES + 6):
            m._append_history(ch, f"user {i}", f"reply {i}")
        bridge = m.history_bridges[ch]
        assert bridge, "nothing was bridged"
        assert bridge[0].startswith("["), f"no date on the bridge line: {bridge[0]!r}"
        # history and its timestamps stay the same length after trimming
        assert len(m.history_times[ch]) == len(m.conversation_histories[ch])
        assert len(m.conversation_histories[ch]) == m._MAX_HISTORY_ENTRIES
    finally:
        m.conversation_histories.pop(ch, None)
        m.history_times.pop(ch, None)
        m.history_bridges.pop(ch, None)


def test_history_entries_sent_to_the_provider_carry_no_extra_keys():
    """Timestamps live in a PARALLEL list precisely because these dicts go to the
    provider verbatim; an unknown key is at best ignored and at worst rejected."""
    import backend.main as m

    ch = "__compaction_test3__"
    m.conversation_histories[ch] = []
    m.history_times[ch] = []
    try:
        m._append_history(ch, "hello", "hi")
        for entry in m.conversation_histories[ch]:
            assert set(entry) == {"role", "content"}
    finally:
        m.conversation_histories.pop(ch, None)
        m.history_times.pop(ch, None)
        m.history_bridges.pop(ch, None)


def test_new_session_clears_the_timestamps_too():
    """A desynced parallel list would stamp later turns with older dates."""
    import inspect

    import backend.main as m

    src = inspect.getsource(m.discord_new_session)
    assert 'history_times["discord"] = []' in src
