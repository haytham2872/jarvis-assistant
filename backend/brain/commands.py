"""The slash commands: one list and one dispatcher, used by Discord and the phone app.

`COMMANDS` is the single list (discord_bridge._COMMAND_HELP is derived from it, so /commands,
the parity snapshot and CLAUDE.md's quick reference keep reading the same thing). `dispatch`
runs one command through the handlers main.py injected into discord_bridge, read at call
time. The reply text of each command belongs to its handler; the error texts here are the ones
the Discord branches always had.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DISCORD_ONLY = "That command is only available on Discord."


@dataclass(frozen=True)
class Command:
    name: str                 # "/pace"
    usage: str                # "/pace [off|low|normal|high]"
    description: str
    choices: list[str] = field(default_factory=list)   # what the phone offers as quick picks
    phone: bool = True        # False: not offered by the phone app


COMMANDS: list[Command] = [
    Command("/new", "/new", "start a brand-new session from the next message"),
    Command("/stop", "/stop", "stop the answer I'm writing right now"),
    Command("/pace", "/pace [off|low|normal|high]", "show or set the research loop's pace",
            ["off", "low", "normal", "high"]),
    Command("/status", "/status", "uptime, the model, background tasks, memory health, cost"),
    Command("/cost", "/cost", "session cost breakdown by model"),
    Command("/about", "/about", "the showcase pitch (who I am, what I do)", phone=False),
    Command("/brief", "/brief [HH:MM|on|off|now]", "show/set the morning brief, or send it now",
            ["now", "on", "off"]),
    Command("/rules", "/rules [drop N]", "the standing rules I hold myself to, or drop one"),
    Command("/record", "/record", "my forecasting track record: score, calibration, recent calls"),
    Command("/pause", "/pause [hours] [all]",
            "stop my own work for a while (all: reminders and the brief too)", ["3", "24", "all"]),
    Command("/resume", "/resume", "end the pause and list what it skipped"),
    Command("/activity", "/activity", "what I'm running, waiting on and have scheduled"),
    Command("/plan", "/plan [week|stats]", "today's plan and the week, and how each block went",
            ["week", "stats"]),
    Command("/goals", "/goals [id]", "the goals I'm working on, or one in detail"),
    Command("/goal", "/goal <id> pause|resume|cancel", "pause, resume or stop one goal"),
    Command("/portfolio", "/portfolio [ticker|stats]",
            "recorded holdings, dated value/P&L, theses and their history", ["stats"]),
    Command("/trace", "/trace", "what my tools did in our conversation"),
    Command("/skills", "/skills", "my procedures for tasks that come back"),
    Command("/skill", "/skill <name> [details]",
            "run one now; /skill export <name> sends its file, /skill undo takes back a change",
            phone=False),
    Command("/commands", "/commands", "this list"),
]

COMMAND_HELP: list[tuple[str, str]] = [(c.usage, c.description) for c in COMMANDS]

# Commands about one conversation (/new, /trace) act on the channel they were sent from. The
# Discord handlers live on discord_bridge; another channel needs its own, set by main.py.
# Without one the command is refused rather than run on Discord's conversation.
_PER_CHANNEL = {"/new", "/trace"}
_channel_handlers: dict[tuple[str, str], object] = {}


def set_channel_handler(name: str, channel: str, fn) -> None:
    _channel_handlers[(name, channel)] = fn


def _takes_argument(c: Command) -> bool:
    return c.usage != c.name


def commands_text(channel: str) -> str:
    shown = [c for c in COMMANDS if channel == "discord" or c.phone]
    lines = ["Here's what you can send me directly:"]
    lines += [f"**{c.usage}** — {c.description}" for c in shown]
    return "\n".join(lines)

# name -> (handler attribute on discord_bridge, passes the argument, error reply)
_SYNC = {
    "/new": ("_new_session_handler", False, "Couldn't start a new conversation -- check the backend log."),
    "/pace": ("_pace_handler", True, "Couldn't change the pace -- check the backend log."),
    "/status": ("_status_handler", False, "Couldn't get runtime status -- check the backend log."),
    "/cost": ("_cost_handler", False, "Couldn't get session cost -- check the backend log."),
    "/about": ("_pitch_handler", False, "Couldn't get the pitch -- check the backend log."),
    "/rules": ("_rules_handler", True, "Couldn't read the rules -- check the backend log."),
    "/record": ("_record_handler", False, "Couldn't read the forecast record -- check the backend log."),
    "/pause": ("_pause_handler", True, "Couldn't pause -- check the backend log."),
    "/resume": ("_resume_handler", True, "Couldn't resume -- check the backend log."),
    "/portfolio": ("_portfolio_handler", True, "Couldn't read the portfolio -- check the backend log."),
    "/trace": ("_trace_handler", False, "Couldn't read the tool receipts -- check the backend log."),
    "/plan": ("_plan_handler", True, "Couldn't read the plan -- check the backend log."),
    "/goals": ("_goals_handler", True, "Couldn't read the goals -- check the backend log."),
    "/skills": ("_skills_handler", True, "Couldn't read my skills -- check the backend log."),
    "/activity": ("_activity_handler", False, "Couldn't read my activity -- check the backend log."),
}


async def dispatch(text: str, *, channel: str, deliver=None) -> str | None:
    """Run one slash command. None: not a command (an ordinary message). "": a command whose
    handler is not wired, so say nothing. `channel` is "discord" or "mobile". `deliver`
    (Discord) is how "/brief now" sends its messages as they are composed, so the handler sees
    a failed send; without it the messages are collected and returned joined."""
    from backend.brain import discord_bridge as db

    text = (text or "").strip()
    if not text:
        return None
    stopped = db.stop_reply(text)
    if stopped is not None:
        return stopped
    head, _, rest = text.partition(" ")
    name = head.lower()
    arg = rest.strip()
    command = next((c for c in COMMANDS if c.name == name), None)
    if command is None:
        return None
    if arg and not _takes_argument(command):
        return None   # "/new please", "/stop it now": an ordinary message, as before

    if name == "/commands":
        return db._commands_text() if channel == "discord" else commands_text(channel)

    if name == "/stop":
        return None   # stop_reply above answers it; nothing else to run here

    if name in _PER_CHANNEL and channel != "discord":
        fn = _channel_handlers.get((name, channel))
        if fn is None:
            return DISCORD_ONLY
        try:
            return fn()
        except Exception as e:
            log.error("command %s on %s failed: %s", name, channel, e)
            return _SYNC[name][2]

    if name == "/skill":
        sub, _, sub_arg = arg.partition(" ")
        sub = sub.lower()
        if sub == "undo":
            if db._skill_undo_handler is None:
                return ""
            try:
                return db._skill_undo_handler(sub_arg.strip())
            except Exception as e:
                log.error("command /skill undo failed: %s", e)
                return "Couldn't undo that -- check the backend log."
        if sub == "export":
            if channel != "discord":
                return DISCORD_ONLY
            if db._skill_export_handler is None:
                return ""
            try:
                out = db._skill_export_handler(sub_arg.strip())
            except Exception as e:
                log.error("command /skill export failed: %s", e)
                return "Couldn't export that skill -- check the backend log."
            return out.removeprefix("Error: ") if isinstance(out, str) else ""
        return None   # "/skill <name>" is an ordinary turn

    if name == "/about" and channel != "discord":
        return DISCORD_ONLY

    if name == "/brief":
        if arg.lower() == "now":
            if db._brief_now_handler is None:
                return ""
            sent: list[str] = []

            async def _collect(message: str) -> bool:
                sent.append(message)
                return True

            try:
                await db._brief_now_handler(deliver or _collect)
            except Exception as e:
                log.error("command /brief now failed: %s", e)
                sent.append("Couldn't compose the brief -- check the backend log.")
            return "\n\n".join(sent)
        if db._brief_handler is None:
            return ""
        try:
            return db._brief_handler(arg)
        except Exception as e:
            log.error("command /brief failed: %s", e)
            return "Couldn't update the brief config -- check the backend log."

    if name == "/goal":
        if db._goal_control_handler is None:
            return ""
        try:
            return await db._goal_control_handler(arg)
        except Exception as e:
            log.error("command /goal failed: %s", e)
            return "Couldn't change that goal -- check the backend log."

    attr, with_arg, failed = _SYNC[name]
    handler = getattr(db, attr)
    if handler is None:
        return ""
    try:
        return handler(arg) if with_arg else handler()
    except Exception as e:
        log.error("command %s failed: %s", name, e)
        return failed
