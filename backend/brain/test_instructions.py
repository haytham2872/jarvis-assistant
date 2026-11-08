"""Tell him once: standing instructions that apply to every turn in their scope.

Runs against the real SQLite store (conftest gives each test its own database) and the
real prompt assembler.
"""
import asyncio
import inspect

import pytest

from backend.brain import instructions as ins
from backend.brain import personality
from backend.storage import instructions as store


def run(coro):
    return asyncio.run(coro)


def add(rule, scope="always", **kw):
    return run(ins.standing_instruction("add", rule=rule, scope=scope, **kw))


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    # Prompts are byte-compared below; the [Now] line has minute granularity.
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "now_line", lambda: "[Now] pinned")


# ------------------------------------------------------------------ the tool
def test_add_confirms_after_the_rule_is_stored():
    out = add("Keep replies short -- he reads on his phone", "discord")
    assert out.startswith("Confirmed") and "r1" in out
    assert [r["rule"] for r in store.active()] == ["Keep replies short -- he reads on his phone"]


@pytest.mark.parametrize("rule, scope, needle", [
    ("", "always", "needs its text"),
    ("x" * 400, "always", "under 300"),
    ("be brief", "email", "scope must be"),
])
def test_bad_rules_are_refused_and_not_stored(rule, scope, needle):
    out = add(rule, scope)
    assert out.startswith("Error") and needle in out
    assert store.active() == []


def test_saying_a_rule_again_counts_as_a_repeat_not_a_new_rule():
    """The measurement: he had to repeat himself, so the rule was not followed."""
    add("Stop narrating what you are about to search")
    out = add("stop narrating what you're about to search for")
    assert "repeat it 1x" in out
    rules = store.active()
    assert len(rules) == 1 and rules[0]["times_restated"] == 1


def test_a_paraphrased_repeat_is_counted_when_the_model_names_it():
    add("Keep replies short on Discord", "discord")
    out = add("He wants brevity when he messages from his phone", "discord", restates="r1")
    assert "repeat it 1x" in out
    rules = store.active()
    assert len(rules) == 1 and rules[0]["times_restated"] == 1
    assert rules[0]["rule"] == "He wants brevity when he messages from his phone"


def test_restating_a_rule_that_is_not_held_is_an_error():
    assert add("Keep it short", restates="r4").startswith("Error")
    assert store.active() == []


def test_a_related_but_different_rule_is_kept_separately():
    add("Keep replies short on Discord", "discord")
    add("Keep replies in English", "discord")
    assert len(store.active()) == 2


def test_the_same_words_in_another_scope_are_a_different_rule():
    add("Keep replies short", "discord")
    add("Keep replies short", "desk")
    assert len(store.active()) == 2


def test_replacing_a_rule_supersedes_it_and_keeps_the_history():
    add("Reply in French")
    out = add("Reply in English", replaces="r1")
    assert "replaces r1" in out
    old = store.get(1)
    assert old["state"] == store.SUPERSEDED and old["superseded_by"] == 2
    assert [r["rule"] for r in store.active()] == ["Reply in English"]


def test_replacing_a_rule_that_does_not_exist_is_an_error():
    assert add("Reply in English", replaces="r9").startswith("Error")
    assert store.active() == []


def test_retire_removes_a_rule_from_every_future_turn():
    add("Always end with a question")
    assert "Retired" in run(ins.standing_instruction("retire", rule_id="r1"))
    assert store.active() == [] and ins.render_block("discord") == ""
    assert store.get(1)["state"] == store.RETIRED     # kept, not deleted


def test_active_rules_are_capped(monkeypatch):
    monkeypatch.setattr(ins, "_MAX_ACTIVE", 2)
    add("one rule about apples")
    add("two rule about oranges")
    assert "already 2" in add("three rule about pears")


def test_storage_unavailable_is_reported_not_claimed(monkeypatch):
    monkeypatch.setattr(store, "_conn", lambda: None)
    assert add("Keep it short").startswith("Error")


def test_a_write_that_does_not_read_back_is_not_confirmed(monkeypatch):
    monkeypatch.setattr(store, "get", lambda rule_id: None)
    assert add("Keep it short").startswith("Error: the rule was NOT saved")


def test_the_originating_request_is_kept_as_the_source(monkeypatch):
    from backend.context import origin
    origin.set_origin("On Discord, keep it short -- I'm reading on my phone.")
    try:
        add("Keep replies short", "discord")
    finally:
        origin.clear_origin()
    assert store.active()[0]["source_quote"].startswith("On Discord, keep it short")


# ------------------------------------------------------------------ the prompt
def test_rules_are_filtered_by_channel():
    add("Keep replies short", "discord")
    add("Speak slowly and clearly", "desk")
    add("Never use the word 'certainly'", "always")
    discord = ins.render_block("discord")
    desk = ins.render_block("local")
    assert "Keep replies short" in discord and "Speak slowly" not in discord
    assert "Speak slowly" in desk and "Keep replies short" not in desk
    assert "certainly" in discord and "certainly" in desk
    everything = ins.render_block(None)
    assert "On Discord: Keep replies short" in everything
    assert "At the desk: Speak slowly" in everything


def test_the_rules_ride_on_the_outgoing_user_message():
    """Measured live: the system prompt is where a rule is ignored; the latest user
    message is where it is followed. So that is where it goes."""
    from backend.brain import engine
    add("Keep replies short", "discord")
    msg = engine._outgoing_user_message("How do transformers work?", "discord")
    assert msg["role"] == "user"
    assert msg["content"].startswith("How do transformers work?\n\n[Standing instructions")
    assert "Keep replies short" in msg["content"]
    assert "Keep replies short" not in engine._outgoing_user_message("hi", "local")["content"]


def test_no_rules_leaves_the_message_untouched():
    from backend.brain import engine
    assert engine._outgoing_user_message("hello", "discord") == {"role": "user", "content": "hello"}


def test_a_storage_failure_never_breaks_the_turn(monkeypatch):
    from backend.brain import engine
    def boom(channel=None):
        raise RuntimeError("db gone")
    monkeypatch.setattr(ins, "render_block", boom)
    assert engine._outgoing_user_message("hello", "discord")["content"] == "hello"


def test_rules_never_leak_into_the_system_prompt_or_stored_records():
    """Only the outgoing request carries the note: history, memory ingestion and the
    thread record all keep Sam's actual words."""
    from backend.brain import engine
    add("Keep replies short", "discord")
    stable, volatile = personality.build_system_prompt("hello")
    assert "Keep replies short" not in stable + volatile
    src = inspect.getsource(engine.chat)
    assert "_compose_compat_request(" in src
    assert "_outgoing_user_message(user_text, channel," in inspect.getsource(
        engine._compose_compat_request)
    assert "await _ingest_mem(model, user_text, full_response)" in src
    assert "_record_thread_task(user_text, full_response," in src


# ------------------------------------------------------------------ the nudge
@pytest.mark.parametrize("text", [
    "From now on, keep it short on Discord",
    "Stop narrating what you're about to search",
    "I told you I sold the NVDA position",
    "How many times do I have to say this",
    "Going forward, reply in English",
])
def test_correction_wording_triggers_the_nudge(text):
    assert personality.is_correction_request(text)
    _, volatile = personality.build_system_prompt(text)
    assert personality._CORRECTION_FRAME in volatile


@pytest.mark.parametrize("text", [
    "I don't know what to cook tonight", "stop the music", "what's the time",
    "can you remember the port number", "tell me about Camus",
])
def test_ordinary_conversation_does_not(text):
    assert not personality.is_correction_request(text)


# ------------------------------------------------------------------ /rules
def test_rules_command_lists_and_drops():
    assert "not holding any" in ins.handle_rules_command("")
    add("Keep replies short", "discord")
    listing = ins.handle_rules_command("")
    assert "[r1] On Discord: Keep replies short" in listing
    assert ins.handle_rules_command("drop r1") == "Dropped rule r1."
    assert "No active rule" in ins.handle_rules_command("drop 1")
    assert ins.handle_rules_command("nonsense").startswith("Usage")


def test_rules_command_is_wired():
    from backend import main
    from backend.brain import discord_bridge
    assert "rules_handler       = instructions.handle_rules_command" in inspect.getsource(main)
    from backend.brain import commands
    assert '"/rules": ("_rules_handler"' in inspect.getsource(commands)
    assert any(cmd.startswith("/rules") for cmd, _ in discord_bridge._COMMAND_HELP)


def test_standing_rules_are_not_available_to_background_tasks():
    from backend.brain import background
    assert "standing_instruction" not in background._ALLOWED_TOOLS
