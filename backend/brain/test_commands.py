"""The shared command list and dispatcher (Discord and the phone app)."""
import asyncio

import pytest

from backend.brain import commands, discord_bridge as db


def run(coro):
    return asyncio.run(coro)


def _wire(monkeypatch):
    """Every handler answers 'ok <name> <arg>' so a dispatch proves which one ran."""
    def sync(name):
        return lambda arg="": f"ok {name} {arg}".strip()

    for attr in ("_new_session_handler", "_status_handler", "_cost_handler", "_pitch_handler",
                 "_record_handler", "_trace_handler", "_activity_handler"):
        monkeypatch.setattr(db, attr, (lambda a: lambda: f"ok {a}")(attr))
    for attr in ("_pace_handler", "_brief_handler", "_rules_handler", "_pause_handler",
                 "_resume_handler", "_portfolio_handler", "_plan_handler", "_goals_handler",
                 "_skills_handler", "_skill_undo_handler"):
        monkeypatch.setattr(db, attr, (lambda a: lambda arg: f"ok {a} {arg}".strip())(attr))

    async def goal(arg):
        return f"ok goal {arg}"

    async def brief_now(send):
        await send("first")
        await send("second")
        return "ack"

    monkeypatch.setattr(db, "_goal_control_handler", goal)
    monkeypatch.setattr(db, "_brief_now_handler", brief_now)
    monkeypatch.setattr(db, "_skill_export_handler", lambda name: f"Error: no skill {name}")
    monkeypatch.setattr(db, "_stop_handler", lambda: True)


def test_command_help_is_derived_from_the_list():
    assert db._COMMAND_HELP == commands.COMMAND_HELP
    assert [u for u, _ in db._COMMAND_HELP] == [c.usage for c in commands.COMMANDS]


def test_choices_and_phone_flags():
    by = {c.name: c for c in commands.COMMANDS}
    assert by["/pace"].choices == ["off", "low", "normal", "high"]
    assert by["/pause"].choices == ["3", "24", "all"]
    assert by["/brief"].choices == ["now", "on", "off"]
    assert by["/plan"].choices == ["week", "stats"]
    assert not by["/skill"].phone and not by["/about"].phone


@pytest.mark.parametrize("usage", [u for u, _ in commands.COMMAND_HELP])
def test_every_listed_command_dispatches(monkeypatch, usage):
    _wire(monkeypatch)
    text = usage.split()[0]
    if text == "/skill":
        text = "/skill undo"
    reply = run(commands.dispatch(text, channel="discord"))
    assert reply, f"{text} did not dispatch"


def test_status_returns_the_status_text(monkeypatch):
    monkeypatch.setattr(db, "_status_handler", lambda: "all fine")
    assert run(commands.dispatch("/status", channel="mobile")) == "all fine"


def test_a_plain_message_is_not_a_command():
    assert run(commands.dispatch("hello", channel="mobile")) is None
    assert run(commands.dispatch("/skill myskill do it", channel="mobile")) is None
    assert run(commands.dispatch("/nonsense", channel="mobile")) is None


def test_unwired_handler_stays_silent(monkeypatch):
    monkeypatch.setattr(db, "_status_handler", None)
    assert run(commands.dispatch("/status", channel="discord")) == ""


def test_a_failing_handler_gives_the_old_error_text(monkeypatch):
    def boom(arg):
        raise RuntimeError("x")
    monkeypatch.setattr(db, "_pace_handler", boom)
    assert run(commands.dispatch("/pace high", channel="discord")) == \
        "Couldn't change the pace -- check the backend log."


@pytest.mark.parametrize("text", ["/pace high", "/rules", "/activity"])
def test_discord_and_mobile_get_the_same_reply(monkeypatch, text):
    _wire(monkeypatch)
    assert run(commands.dispatch(text, channel="discord")) == \
        run(commands.dispatch(text, channel="mobile"))


def test_arguments_reach_the_handler(monkeypatch):
    _wire(monkeypatch)
    assert run(commands.dispatch("/pace high", channel="mobile")) == "ok _pace_handler high"
    assert run(commands.dispatch("/goal 3 pause", channel="mobile")) == "ok goal 3 pause"


def test_brief_now_returns_the_messages_joined(monkeypatch):
    _wire(monkeypatch)
    assert run(commands.dispatch("/brief now", channel="mobile")) == "first\n\nsecond"


def test_discord_only_commands_on_the_phone(monkeypatch):
    _wire(monkeypatch)
    for text in ("/about", "/skill export x"):
        assert run(commands.dispatch(text, channel="mobile")) == \
            "That command is only available on Discord."
    assert run(commands.dispatch("/about", channel="discord")) == "ok _pitch_handler"


# Review findings, 7 November 2026.

def test_the_phone_new_and_trace_never_act_on_discords_conversation(monkeypatch):
    from backend.brain import commands, discord_bridge
    discord_calls = []
    monkeypatch.setattr(discord_bridge, "_new_session_handler", lambda: discord_calls.append(1) or "discord new")
    monkeypatch.setattr(discord_bridge, "_trace_handler", lambda: "discord trace")
    monkeypatch.setattr(commands, "_channel_handlers", {})
    assert asyncio.run(commands.dispatch("/new", channel="mobile")) == commands.DISCORD_ONLY
    assert discord_calls == []
    commands.set_channel_handler("/new", "mobile", lambda: "phone new")
    commands.set_channel_handler("/trace", "mobile", lambda: "phone trace")
    assert asyncio.run(commands.dispatch("/new", channel="mobile")) == "phone new"
    assert asyncio.run(commands.dispatch("/trace", channel="mobile")) == "phone trace"
    assert discord_calls == []
    assert asyncio.run(commands.dispatch("/new", channel="discord")) == "discord new"


@pytest.mark.parametrize("text", ["/stop please", "/new foo", "/status of the bridge", "/cost x"])
def test_a_no_argument_command_with_words_after_it_is_an_ordinary_message(text, monkeypatch):
    from backend.brain import commands, discord_bridge
    monkeypatch.setattr(discord_bridge, "_stop_handler", lambda: False)
    assert asyncio.run(commands.dispatch(text, channel="discord")) is None


def test_the_phone_command_list_shows_only_what_the_phone_runs():
    from backend.brain import commands
    text = asyncio.run(commands.dispatch("/commands", channel="mobile"))
    assert "/pace" in text and "/about" not in text and "/skill <name>" not in text
