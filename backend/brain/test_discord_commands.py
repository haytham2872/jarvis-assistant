"""Tests for the "/commands" Discord command and its self-model reachability.

Real production incident (2026-09-06): asked in plain conversation "what
commands do you have?" (not by typing /commands), JARVIS had no way to
answer -- every command was documented *somewhere* in CLAUDE.md, but
scattered under whichever module happened to implement it, and even the
ones grouped under "Discord Bridge" were unreachable in practice because
that heading's body is far longer than self_model.py's per-heading
truncation cap. Fixed with a single _COMMAND_HELP list (the live,
authoritative source /commands itself reads from) and a short, dedicated
CLAUDE.md heading built to survive that same cap.

This file pins three things: /commands' own output is complete, the
CLAUDE.md quick-reference list actually matches _COMMAND_HELP (so the two
can't silently drift apart), and the self-model digest that JARVIS actually
sees in conversation still contains every command after going through the
real truncation pipeline -- the exact class of bug this all exists to catch,
run against the real CLAUDE.md, not a fake one.
"""

import re

import pytest

from backend.brain import discord_bridge as db
from backend.brain import self_model as sm


def test_command_help_entries_are_well_formed():
    assert db._COMMAND_HELP, "_COMMAND_HELP must not be empty"
    for cmd, desc in db._COMMAND_HELP:
        assert cmd.startswith("/"), f"{cmd!r} should start with /"
        assert desc.strip(), f"{cmd!r} has an empty description"


def test_commands_text_lists_every_command():
    text = db._commands_text()
    for cmd, desc in db._COMMAND_HELP:
        # Match just the command token (before any [args]) -- e.g. "/pace"
        # out of "/pace [off|low|normal|high]", since that's what a reader
        # actually scans for.
        token = cmd.split(" ", 1)[0]
        assert token in text, f"{token!r} missing from /commands output"
        assert desc in text, f"description for {cmd!r} missing from /commands output"


def test_commands_text_has_a_friendly_intro():
    text = db._commands_text()
    assert text.split("\n", 1)[0].strip(), "should not start with a bare bullet list"


def _claude_md_command_tokens() -> list[str]:
    """Extract the /command tokens from the self-model's own quick-reference bullet
    list (docs/SELF_MODEL.md), in the order they appear -- independent of
    self_model.py's parsing, so this test catches a hand-edit typo in either file."""
    text = sm._CLAUDE_MD.read_text(encoding="utf-8")
    match = re.search(
        r"### Discord Commands — quick reference\n(.*?)(?:\n#{1,3} |\Z)",
        text, re.DOTALL,
    )
    assert match, "could not find the 'Discord Commands' quick-reference section in docs/SELF_MODEL.md"
    body = match.group(1)
    # Each bullet is "- `/cmd [args]` — description"; take just the /cmd token.
    return [full.split(" ", 1)[0] for full in re.findall(r"^- `(/[^`]+)`", body, re.MULTILINE)]


def test_engine_md_quick_reference_matches_command_help():
    """The two lists are maintained by hand in two different files -- this is
    the regression guard that catches them drifting apart, which is exactly
    the class of silent staleness the whole feature exists to prevent."""
    claude_md_tokens = _claude_md_command_tokens()
    code_tokens = [cmd.split(" ", 1)[0] for cmd, _ in db._COMMAND_HELP]
    assert claude_md_tokens == code_tokens, (
        "CLAUDE.md's 'Discord Commands' list and discord_bridge._COMMAND_HELP "
        "have drifted apart -- update both together"
    )


def test_self_model_digest_survives_truncation_with_full_command_list():
    """Run the REAL truncation pipeline against the REAL CLAUDE.md -- this is
    the actual bug from the live incident: content can be technically present
    in CLAUDE.md yet never reach the digest JARVIS sees, because each mapped
    heading is independently capped at _HEADING_CHAR_CAP chars. No LLM call;
    this is the same pure, instant, hash-keyed builder self_model.py always
    uses."""
    digest = sm._build_digest_from_claudemd()
    tools_section = digest.get("tools", "")
    for cmd, _ in db._COMMAND_HELP:
        token = cmd.split(" ", 1)[0]
        assert token in tools_section, (
            f"{token!r} did not survive into the 'tools' self-model section -- "
            f"the quick-reference heading may have grown past _HEADING_CHAR_CAP"
        )


@pytest.mark.parametrize("name,pronouns", [
    ("Sam", "he"), ("", "they"), ("Alexandra Montgomery-Whitfield", "they"), ("Alex", "she")])
def test_no_self_model_section_is_truncated_by_the_real_claude_md(caplog, monkeypatch,
                                                                  name, pronouns):
    """The overflow used to be only a log warning. On 2026-09-24 the tools section
    reached 1515 of 1500 chars and silently cut the end of the Discord command list,
    while the token check above still passed because '/commands' also appears earlier.
    Any section over the cap now fails the suite instead of degrading quietly.

    The length depends on the owner: "their" is longer than "his", and the name appears
    several times. The fresh-install test found a section at 2201 of 2200 with no name and
    "they", which a suite run only as "Sam", "he" could not see."""
    import logging
    from backend import owner
    monkeypatch.setattr(owner, "NAME", name)
    monkeypatch.setattr(owner, "PRONOUNS", pronouns)
    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        sm._build_digest_from_claudemd()
    over = [r.getMessage() for r in caplog.records
            if "over _SECTION_CHAR_CAP" in r.getMessage()
            or "over _HEADING_CHAR_CAP" in r.getMessage()]
    assert not over, over


def test_the_digest_carries_what_he_must_know_about_unattended_work_and_pausing():
    """Found while wiring P2: 'Active server and autonomous work' was 1270 chars against a
    1200-char heading cap, so its LAST sentence -- unattended work never sends email, it
    becomes an approval request -- had been silently missing from what JARVIS reads about
    himself since P1. Any sentence added to that heading is only real if it survives here."""
    main = sm._build_digest_from_claudemd()["main"]
    assert "never sends" in main and "approval request" in main
    assert "/pause" in main and "/resume" in main and "/activity" in main
    assert "reminders and the brief still arrive" in main
