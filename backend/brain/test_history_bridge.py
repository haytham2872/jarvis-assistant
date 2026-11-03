"""Tests for backend/brain/history_bridge.py — the 40-turn history cliff bridge.

Pure-function focused, same reasoning as pacing.py/bootstrap.py's own tests: this logic
lives outside main.py specifically so it can be unit-tested without importing main.py
(which instantiates real audio hardware at import time). Run with:
    .venv\\Scripts\\pytest backend/brain/test_history_bridge.py -v
"""

from backend.brain import history_bridge as hb


def test_format_text_empty_bridge():
    assert hb.format_text([]) == ""


def test_format_text_joins_lines():
    assert hb.format_text(["line one", "line two"]) == "line one\nline two"


def test_record_dropped_formats_one_line_per_pair():
    bridge: list[str] = []
    dropped = [
        {"role": "user", "content": "what's the weather"},
        {"role": "assistant", "content": "sunny and 20 degrees"},
    ]
    hb.record_dropped(bridge, dropped)
    assert bridge == ["User: what's the weather | JARVIS: sunny and 20 degrees"]


def test_record_dropped_handles_multiple_pairs_in_order():
    bridge: list[str] = []
    dropped = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    hb.record_dropped(bridge, dropped)
    assert bridge == [
        "User: first question | JARVIS: first answer",
        "User: second question | JARVIS: second answer",
    ]


def test_record_dropped_appends_onto_existing_bridge():
    bridge = ["User: old | JARVIS: old reply"]
    dropped = [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    hb.record_dropped(bridge, dropped)
    assert bridge == [
        "User: old | JARVIS: old reply",
        "User: new question | JARVIS: new answer",
    ]


def test_record_dropped_truncates_long_content():
    bridge: list[str] = []
    dropped = [
        {"role": "user", "content": "x" * 500},
        {"role": "assistant", "content": "y" * 500},
    ]
    hb.record_dropped(bridge, dropped)
    assert len(bridge) == 1
    # both sides capped at _PREVIEW_CHARS (100), plus the "User: "/" | JARVIS: " framing
    assert bridge[0].count("x") == hb._PREVIEW_CHARS
    assert bridge[0].count("y") == hb._PREVIEW_CHARS


def test_record_dropped_trims_to_max_lines():
    bridge = [f"line {i}" for i in range(hb._MAX_LINES)]   # already at the cap
    dropped = [
        {"role": "user", "content": "newest question"},
        {"role": "assistant", "content": "newest answer"},
    ]
    hb.record_dropped(bridge, dropped)
    assert len(bridge) == hb._MAX_LINES
    assert bridge[0] == "line 1"           # oldest ("line 0") trimmed
    assert "newest question" in bridge[-1]  # newest kept


def test_record_dropped_skips_stray_odd_trailing_entry():
    bridge: list[str] = []
    dropped = [
        {"role": "user", "content": "paired question"},
        {"role": "assistant", "content": "paired answer"},
        {"role": "user", "content": "orphaned, no assistant reply"},
    ]
    hb.record_dropped(bridge, dropped)
    assert bridge == ["User: paired question | JARVIS: paired answer"]


def test_record_dropped_empty_list_is_noop():
    bridge = ["existing line"]
    hb.record_dropped(bridge, [])
    assert bridge == ["existing line"]


def test_record_dropped_missing_content_key_degrades_to_empty_string():
    bridge: list[str] = []
    dropped = [{"role": "user"}, {"role": "assistant"}]
    hb.record_dropped(bridge, dropped)
    assert bridge == ["User:  | JARVIS: "]
