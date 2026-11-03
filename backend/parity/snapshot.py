"""Build the A00 parity snapshot of Jarvis's capability surface.

One generator, used by both the fixture file and the test that compares against it, so
the two cannot drift apart. Run as a module to regenerate after a deliberate change:

    python -m backend.parity.snapshot
"""
from __future__ import annotations

import json
from pathlib import Path

_FIXTURE = Path(__file__).parent / "tool_registry.json"


def _tool_entry(tool: dict) -> dict:
    schema = tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    return {
        "name": tool["name"],
        # (type, sorted enum) per parameter: action variants are part of the contract,
        # not an implementation detail -- preserving only a tool's name is insufficient.
        "params": {
            key: [spec.get("type"),
                  sorted(spec["enum"]) if isinstance(spec.get("enum"), list) else None]
            for key, spec in sorted(props.items())
        },
        "required": sorted(schema.get("required") or []),
    }


def _discord_commands() -> list[str]:
    """Commands taken from `_COMMAND_HELP`, the bridge's own authoritative list.

    An earlier version scraped `"/[a-z]+"` string literals out of the source. That was a
    weak proxy and provably missed a real regression: renaming a command to `/aboutXX`
    stopped matching the lowercase-only pattern instead of showing up as a change, and a
    duplicate literal elsewhere kept the scraped set looking intact. `_COMMAND_HELP` is
    what `/commands` actually replies with, so locking it also locks the answer Jarvis
    gives when asked what he can do -- which is the thing CLAUDE.md's 2026-09-06
    hardening note exists to keep honest.

    The bare verb is captured (`/brief`, not `/brief [HH:MM|on|off|now]`) alongside the
    full help string, so re-wording help text is not a false alarm while removing or
    renaming a command is caught.
    """
    from backend.brain import discord_bridge

    return sorted(entry[0].split()[0] for entry in discord_bridge._COMMAND_HELP)


def _discord_command_help() -> list[list[str]]:
    """The exact `/commands` reply content, locked so a command cannot quietly lose its
    documented behavior while keeping its name."""
    from backend.brain import discord_bridge

    return [list(entry) for entry in discord_bridge._COMMAND_HELP]


def build_snapshot() -> dict:
    from backend.brain import background
    from backend.tools.registry import TOOLS

    return {
        "tool_count": len(TOOLS),
        "tools": [_tool_entry(t) for t in sorted(TOOLS, key=lambda t: t["name"])],
        "background_allowed_tools": sorted(background._ALLOWED_TOOLS),
        "discord_commands": _discord_commands(),
        "discord_command_help": _discord_command_help(),
    }


def write_snapshot() -> Path:
    _FIXTURE.write_text(
        json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _FIXTURE


if __name__ == "__main__":
    path = write_snapshot()
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"wrote {path} — {data['tool_count']} tools, "
          f"{len(data['background_allowed_tools'])} background-allowed, "
          f"{len(data['discord_commands'])} commands")
