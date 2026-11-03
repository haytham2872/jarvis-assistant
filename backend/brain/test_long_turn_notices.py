"""H4: a long Discord turn is never silent.

Nothing is posted for a turn that finishes within a minute; after that, one plain progress
line at most every two minutes (and not within two minutes of the plan notice), stopping
when the turn ends. A turn cut off by a restart is announced after the restart.
"""
import asyncio
import time
import types

import pytest

from backend.brain import engine, discord_bridge, router
from backend.brain.router import OPENROUTER


def run(coro):
    return asyncio.run(coro)


# --- progress_state -------------------------------------------------------------------------

def test_progress_lines_describe_the_running_turn(monkeypatch):
    since = time.time() - 185
    monkeypatch.setitem(engine.turn_progress, "discord", {
        "started": since + 1, "plan": ["Search", "Read the filings", "Conclude"],
        "step": 1, "tool_calls": 4, "synthesizing": False, "announced_at": since + 2})
    line, announced = engine.progress_state("discord", since)
    assert line == "Still on it: step 2 of 3, Read the filings (3 min in)."
    assert announced == since + 2
    engine.turn_progress["discord"]["synthesizing"] = True
    assert engine.progress_state("discord", since)[0] == "Writing it up now (3 min in)."
    engine.turn_progress["discord"].update(synthesizing=False, plan=[])
    assert engine.progress_state("discord", since)[0] == "Still on it: 4 lookups done so far (3 min in)."


def test_a_record_from_an_earlier_turn_is_ignored(monkeypatch):
    monkeypatch.setitem(engine.turn_progress, "discord", {
        "started": time.time() - 3600, "plan": ["Old step"], "step": 0, "tool_calls": 9,
        "synthesizing": False, "announced_at": None})
    line, announced = engine.progress_state("discord", time.time() - 70)
    assert line == "Still working on it (1 min in)." and announced is None


def test_the_loop_keeps_the_record_current(monkeypatch):
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())

    async def ok_exec(tc, send):
        return tc, "result", True
    monkeypatch.setattr(engine, "_exec_tool", ok_exec)
    snapshots = []

    async def fake(client, model, messages, send, sq, temperature=None, extra_body=None,
                   tools=None, *, purpose="chat", iteration=None, plan_active=False,
                   max_tokens=None):
        snapshots.append(dict(engine.turn_progress["local"]))
        if len(snapshots) < 3:
            return engine._StreamResult(("", "", [{"id": str(len(snapshots)),
                                                   "name": "web_browse", "input": {}}]), "stop", 10)
        return engine._StreamResult(("done", "", []), "stop", 10)
    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    run(engine._chat_compat(OPENROUTER.thinking, "s", [{"role": "user", "content": "x"}],
                            lambda m: asyncio.sleep(0), None, use_thinking=True))
    assert [s["tool_calls"] for s in snapshots] == [0, 1, 2]


# --- the watchdog -------------------------------------------------------------------------

class _Channel:
    id = 7

    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append((time.time(), content))
        return types.SimpleNamespace(id=len(self.sent), channel=self)


def _watch(monkeypatch, turn_seconds, *, first=0.05, every=0.2, announced=None, lines=None):
    monkeypatch.setattr(discord_bridge, "_PROGRESS_FIRST_S", first)
    monkeypatch.setattr(discord_bridge, "_PROGRESS_EVERY_S", every)
    counter = {"n": 0}

    def handler(since):
        counter["n"] += 1
        text = (lines or [f"line {counter['n']}"])[min(counter["n"], len(lines or [1])) - 1] \
            if lines else f"line {counter['n']}"
        return text, announced
    monkeypatch.setattr(discord_bridge, "_progress_handler", handler)

    async def turn(text):
        await asyncio.sleep(turn_seconds)
        return "answer"
    monkeypatch.setattr(discord_bridge, "_turn_handler", turn)
    channel = _Channel()
    message = types.SimpleNamespace(id=99, channel=channel)
    async def scenario():
        result = await discord_bridge._run_turn_with_progress(message, "go")
        count = len(channel.sent)
        await asyncio.sleep(every * 2)          # the loop stays alive after the turn
        assert len(channel.sent) == count, "the watcher kept posting after the turn ended"
        return result
    out = run(scenario())
    return out, channel.sent


def test_a_quick_turn_posts_nothing(monkeypatch):
    out, sent = _watch(monkeypatch, 0.02)
    assert out == "answer" and sent == []


def test_a_long_turn_posts_spaced_lines_and_stops_when_it_ends(monkeypatch):
    out, sent = _watch(monkeypatch, 0.5)
    assert out == "answer" and 2 <= len(sent) <= 3
    gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:])]
    assert all(g >= 0.18 for g in gaps)


def test_an_unchanged_line_is_not_repeated(monkeypatch):
    _out, sent = _watch(monkeypatch, 0.5, lines=["Still thinking (1 min in)."])
    assert [c for _, c in sent] == ["Still thinking (1 min in)."]


def test_no_line_within_the_gap_after_the_plan_notice(monkeypatch):
    started = time.time()
    _out, sent = _watch(monkeypatch, 0.3, first=0.02, every=0.2, announced=started)
    assert sent and sent[0][0] - started >= 0.19


# --- after a restart ------------------------------------------------------------------------

def test_a_cut_off_discord_turn_is_announced_after_restart(monkeypatch):
    from backend import main
    sent = []

    async def owner(text):
        sent.append(text)
        return True
    monkeypatch.setattr(main.discord_bridge, "send_to_owner", owner)

    async def scenario():
        started = asyncio.get_running_loop().create_future()
        started.set_result(None)
        await main._announce_cut_off_turns(started, [
            {"user_preview": "compile the NVDA research into a conclusion"},
            {"user_preview": ""}])
    run(scenario())
    assert len(sent) == 1 and "compile the NVDA research" in sent[0]
    assert "restarted" in sent[0]


def test_open_discord_runs_are_read_before_they_are_reconciled():
    """Reading after reconcile_interrupted() would find nothing: it marks them first."""
    import inspect
    from backend import main
    src = inspect.getsource(main.lifespan)
    read = src.index('_runs.open_runs(["discord"])')
    assert read < src.index("_runs.reconcile_interrupted()")
    assert "_announce_cut_off_turns(" in src
