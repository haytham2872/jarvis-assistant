"""Tests for discord_bridge.configure() — the bulk DI-wiring convenience.

Scoped narrowly to configure() itself, since the individual set_*_handler()
functions it wraps have no dedicated test file and their own behavior
(a plain `global _x; _x = fn` assignment) is exercised indirectly wherever
they're already called (main.py at startup). This file only needs to prove
configure() calls the right setter for the right kwarg, and that omitted
kwargs don't clobber handlers set elsewhere.
"""

import pytest

from backend.brain import discord_bridge as db


@pytest.fixture(autouse=True)
def _reset_handlers():
    """Every handler global reset to None before and after each test, so
    tests can't leak state into each other or into a real running process."""
    names = [
        "_turn_handler", "_new_session_handler", "_transcribe_handler",
        "_pace_handler", "_status_handler", "_cost_handler",
        "_brief_handler", "_brief_now_handler", "_pitch_handler", "_rules_handler",
        "_record_handler", "_pause_handler", "_resume_handler", "_activity_handler",
    ]
    saved = {n: getattr(db, n) for n in names}
    for n in names:
        setattr(db, n, None)
    yield
    for n in names:
        setattr(db, n, saved[n])


def test_configure_wires_every_handler():
    handlers = {
        "turn_handler": lambda text: "reply",
        "new_session_handler": lambda: "new",
        "transcribe_handler": lambda path: "text",
        "pace_handler": lambda arg: "pace",
        "status_handler": lambda: "status",
        "cost_handler": lambda: "cost",
        "brief_handler": lambda arg: "brief",
        "brief_now_handler": lambda: "brief now",
        "pitch_handler": lambda: "pitch",
        "rules_handler": lambda arg: "rules",
        "record_handler": lambda: "record",
        "pause_handler": lambda arg: "pause",
        "resume_handler": lambda arg: "resume",
        "activity_handler": lambda: "activity",
    }
    db.configure(**handlers)
    assert db._turn_handler is handlers["turn_handler"]
    assert db._new_session_handler is handlers["new_session_handler"]
    assert db._transcribe_handler is handlers["transcribe_handler"]
    assert db._pace_handler is handlers["pace_handler"]
    assert db._status_handler is handlers["status_handler"]
    assert db._cost_handler is handlers["cost_handler"]
    assert db._brief_handler is handlers["brief_handler"]
    assert db._brief_now_handler is handlers["brief_now_handler"]
    assert db._pitch_handler is handlers["pitch_handler"]
    assert db._rules_handler is handlers["rules_handler"]
    assert db._record_handler is handlers["record_handler"]
    assert db._pause_handler is handlers["pause_handler"]
    assert db._resume_handler is handlers["resume_handler"]
    assert db._activity_handler is handlers["activity_handler"]


def test_configure_partial_call_only_sets_given_handlers():
    fn = lambda: "pace"
    db.configure(pace_handler=fn)
    assert db._pace_handler is fn
    assert db._turn_handler is None
    assert db._status_handler is None


def test_configure_omitted_kwargs_do_not_clear_existing_handlers():
    """configure(pace_handler=fn) must not reset the other eight to None --
    the docstring's explicit guarantee."""
    first = lambda text: "reply"
    db.configure(turn_handler=first)
    assert db._turn_handler is first

    second = lambda: "pace"
    db.configure(pace_handler=second)
    assert db._turn_handler is first, "turn_handler must survive an unrelated configure() call"
    assert db._pace_handler is second


def test_configure_no_args_is_a_noop():
    db.configure()
    assert db._turn_handler is None
    assert db._pace_handler is None


def test_configure_equivalent_to_calling_setters_directly():
    fn = lambda: "status"
    db.set_status_handler(fn)
    direct = db._status_handler

    db._status_handler = None
    db.configure(status_handler=fn)
    via_configure = db._status_handler

    assert direct is via_configure is fn
