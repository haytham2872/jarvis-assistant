"""Tests for engine.py's tool-loop-cap progress note (2026-09-03).

Live incident: a Discord research task hit _MAX_TOOL_ITERATIONS, the cap
message promised "ask me to continue and I'll pick it up from where I left
off," but nothing preserved what was actually tried across turns -- the
tool-call transcript lives only in that one _chat_compat call's
local state and is discarded once chat() returns (_append_history only keeps
the flat final (user_text, response) pair). Every "Continue" was a blind
restart that reproduced the identical failure. This pins the fix: a
ground-truth log of actual tool calls (not the plan's positional
step-tracking, which can't be trusted once the model gets stuck retrying one
step) is preserved per channel and delivered exactly once into the next
turn's system prompt. Run with:
    .venv\\Scripts\\pytest backend/brain/test_engine_progress_note.py -v
"""

from backend.brain import engine


# ---------------------------------------------------------------------------
# _summarize_tool_call
# ---------------------------------------------------------------------------

def test_summarize_tool_call_uses_query_when_present():
    assert engine._summarize_tool_call("web_browse", {"query": "kimi k3 pricing"}) == \
        "web_browse('kimi k3 pricing')"


def test_summarize_tool_call_uses_task_when_present():
    assert engine._summarize_tool_call("web_browse", {"task": "search for X"}) == \
        "web_browse('search for X')"


def test_summarize_tool_call_uses_url_when_present():
    assert engine._summarize_tool_call("fetch_page", {"url": "https://example.com"}) == \
        "fetch_page('https://example.com')"


def test_summarize_tool_call_truncates_long_values():
    long_query = "x" * 200
    result = engine._summarize_tool_call("web_browse", {"query": long_query})
    assert len(result) < 120


def test_summarize_tool_call_falls_back_to_bare_name():
    assert engine._summarize_tool_call("code_graph", {"action": "explain"}) == "code_graph"


def test_summarize_tool_call_handles_none_input():
    assert engine._summarize_tool_call("code_graph", None) == "code_graph"


# ---------------------------------------------------------------------------
# _build_progress_note — the ground-truth summary
# ---------------------------------------------------------------------------

def test_build_progress_note_empty_call_log_returns_empty():
    assert engine._build_progress_note([], []) == ""


def test_build_progress_note_tallies_repeated_tool_calls():
    call_log = [f"web_browse('query {i}')" for i in range(11)]
    note = engine._build_progress_note([], call_log)
    assert "web_browse x11" in note


def test_build_progress_note_names_untouched_planned_tools():
    """The exact live scenario: the plan called for web_browse, code_graph,
    and memory_search, but the model only ever called web_browse -- the note
    must say so, since the plan's own step_pointer bookkeeping would
    (wrongly) have marked all four steps done."""
    plan = [
        {"id": 1, "label": "Research pricing", "tool": "web_browse"},
        {"id": 2, "label": "Analyze codebase", "tool": "code_graph"},
        {"id": 3, "label": "Check memory", "tool": "memory_search"},
        {"id": 4, "label": "Synthesize", "tool": None},
    ]
    call_log = [f"web_browse('query {i}')" for i in range(8)]
    note = engine._build_progress_note(plan, call_log)
    assert "code_graph" in note
    assert "memory_search" in note
    untouched_sentence = note.split("You never got to:")[1].split(".")[0]
    assert "web_browse" not in untouched_sentence  # web_browse WAS touched, must not appear here


def test_build_progress_note_no_untouched_line_when_plan_empty():
    call_log = ["code_graph"]
    note = engine._build_progress_note([], call_log)
    assert "You never got to" not in note


def test_build_progress_note_includes_recent_attempts():
    call_log = [f"web_browse('query {i}')" for i in range(10)]
    note = engine._build_progress_note([], call_log)
    assert "query 9" in note   # most recent must survive
    assert "query 0" not in note.split("Most recent attempts:")[1]  # oldest trimmed from the tail


# ---------------------------------------------------------------------------
# pop_pending_progress — delivered exactly once, framed
# ---------------------------------------------------------------------------

def test_pop_pending_progress_empty_when_nothing_pending():
    assert engine.pop_pending_progress("nonexistent-channel") == ""


def test_pop_pending_progress_returns_framed_note_and_clears_it():
    engine._pending_progress["local"] = "you called web_browse x5."
    try:
        first = engine.pop_pending_progress("local")
        assert "you called web_browse x5." in first
        assert engine._PROGRESS_FRAME in first
        # delivered exactly once
        second = engine.pop_pending_progress("local")
        assert second == ""
    finally:
        engine._pending_progress.pop("local", None)


def test_pop_pending_progress_is_per_channel():
    engine._pending_progress["discord"] = "discord progress"
    try:
        assert engine.pop_pending_progress("local") == ""
        assert "discord progress" in engine.pop_pending_progress("discord")
    finally:
        engine._pending_progress.pop("discord", None)
        engine._pending_progress.pop("local", None)


# ---------------------------------------------------------------------------
# Wiring — chat() must actually consume the progress note (static check,
# same style as the KIMI_NO_THINKING_BODY wiring tests: chat() has too large
# a dependency surface for a full integration test here)
# ---------------------------------------------------------------------------

def test_chat_consumes_pending_progress():
    import inspect
    src = inspect.getsource(engine.chat)
    assert "pop_pending_progress(channel)" in src


def test_chat_compat_stashes_progress_on_cap_hit():
    import inspect
    for fn in (engine._chat_compat,):
        src = inspect.getsource(fn)
        assert "_build_progress_note(" in src, f"{fn.__name__} never builds a progress note"
        assert "_pending_progress[channel]" in src, f"{fn.__name__} never stashes it"
