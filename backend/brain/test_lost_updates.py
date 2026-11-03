"""A04 slice 4: concurrent writes preserve changes.

Roadmap acceptance: *"concurrent writes preserve changes"*.

Every loop in the brain had the same shape — read a file, `await` something slow (an
LLM call, a web search, a Discord DM), then write back a value derived from the *stale*
read. Anything another writer put in the file during that window was silently destroyed.

This was not theoretical. The reproduced case: a reminder scheduled while chronos was
sending a reminder DM vanished, **after** `schedule_reminder` had already told Sam it
was saved and verified by read-back. `test_reminder_added_during_a_dm_survives` is that
repro, kept as a regression test.

The structural guard at the bottom is the durable half. "Do not await between reading a
file and writing it back" is an invariant nobody will remember six months from now, so it
is enforced statically over the whole backend rather than trusted.
"""
import ast
import asyncio
import datetime
import pathlib

import pytest

from backend.brain import canary, chronos, pacing, research, rumination


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- the primitive
def test_update_state_reads_fresh_and_applies_the_delta(tmp_path):
    f = tmp_path / "s.json"
    pacing.save_state(f, {"a": 1})

    # someone else writes between the caller's earlier read and this update
    pacing.save_state(f, {"a": 1, "added_by_someone_else": True})
    pacing.update_state(f, lambda st: st.update({"a": 2}))

    got = pacing.load_state(f)
    assert got["a"] == 2
    assert got["added_by_someone_else"] is True, "the other writer's change was clobbered"


def test_update_state_accepts_a_replacement_return(tmp_path):
    f = tmp_path / "s.json"
    pacing.save_state(f, {"a": 1})
    pacing.update_state(f, lambda st: {"b": 2})
    assert pacing.load_state(f) == {"b": 2}


def test_update_state_never_raises_on_a_bad_mutator(tmp_path):
    f = tmp_path / "s.json"
    pacing.save_state(f, {"a": 1})

    def boom(st):
        raise RuntimeError("mutator exploded")

    pacing.update_state(f, boom)
    assert pacing.load_state(f) == {"a": 1}, "a failing mutator must not corrupt state"


def test_update_state_does_not_await():
    """The whole guarantee rests on this: with no await between the read and the write,
    the event loop cannot interleave another writer into the window. A lock was
    deliberately not used -- one held across those awaits would stall a Discord command
    behind a multi-minute research tick."""
    import inspect
    assert not inspect.iscoroutinefunction(pacing.update_state)
    tree = ast.parse(inspect.getsource(pacing.update_state))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Await)]


# ------------------------------------------------------- the reproduced incident
def test_reminder_added_during_a_dm_survives(tmp_path, monkeypatch):
    """The live repro. chronos read schedule.md, awaited a Discord DM, then wrote back
    the stale list -- destroying a reminder scheduled in between."""
    sched = tmp_path / "schedule.md"
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", sched)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "state.json")

    now = datetime.datetime.now(chronos._TZ)
    due = now - datetime.timedelta(minutes=5)
    sched.write_text("\n".join(list(chronos._SCHEDULE_HEADER) + [
        chronos._format_reminder_line(due, "once", "take the bins out")]) + "\n",
        encoding="utf-8")

    async def notifier(text):
        later = now + datetime.timedelta(hours=3)
        await chronos.schedule_reminder(action="add",
                                        when=later.strftime("%Y-%m-%d %H:%M"),
                                        message="call the bank")
        return True

    monkeypatch.setattr(chronos, "_notifier", notifier)
    run(chronos._fire_due_reminders(now))

    final = sched.read_text(encoding="utf-8")
    assert "call the bank" in final, "a reminder scheduled during the DM was destroyed"
    assert "reminder fired: take the bins out" in final, "the fired reminder was not logged"


def test_research_keeps_a_finding_deferred_during_the_dm(tmp_path, monkeypatch):
    """The same class, second instance: clearing `deferred_notifies` wholesale discarded
    anything parked while the DM was in flight."""
    f = tmp_path / "pace.json"
    monkeypatch.setattr(research, "_PACE_FILE", f)
    pacing.save_state(f, {"deferred_notifies": [{"message": "older finding"}]})

    # the DM goes out carrying 1 piggybacked finding; a 2nd is parked meanwhile
    pacing.update_state(f, lambda st: st["deferred_notifies"].append(
        {"message": "parked during the DM"}))

    sent_count = 1
    def _mark_sent(st, _n=sent_count):
        st["deferred_notifies"] = list(st.get("deferred_notifies", []))[_n:]
        st["last_notify"] = 123.0
    research._update_state(_mark_sent)

    left = [d["message"] for d in pacing.load_state(f)["deferred_notifies"]]
    assert left == ["parked during the DM"]


def test_rumination_marker_survives_a_concurrent_pop(tmp_path, monkeypatch):
    """`pop_rumination_mention()` runs on EVERY turn and writes this same file."""
    f = tmp_path / "rum.json"
    monkeypatch.setattr(rumination, "_STATE_FILE", f)
    pacing.save_state(f, {"pending": {"sid": "old", "text": "t", "created": 1.0},
                          "ruminated": ["s1"]})

    rumination.pop_rumination_mention()                 # a turn delivers the old thought
    rumination._update_state(lambda st: rumination._mark_ruminated(st, "s2"))

    got = pacing.load_state(f)
    assert got["pending"] is None, "the delivered thought was resurrected"
    assert got["ruminated"] == ["s1", "s2"]


# ------------------------------------------------------------ the structural guard
_RESOURCES = {
    "schedule": ({"_read_schedule"}, {"_write_schedule", "_write_schedule_verified"}),
    "state":    ({"_load_state"},    {"_save_state"}),
    "file":     ({"read_text"},      {"write_text"}),
}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    return f.id if isinstance(f, ast.Name) else ""


def _windows_with_awaits(root: pathlib.Path | None = None) -> list[str]:
    """Every async function that reads a resource, awaits, then writes that SAME
    resource without re-reading it.

    Grouping by resource matters and is not cosmetic: an earlier version of this scan
    pooled all reads together, so an unrelated `_load_state()` call counted as a
    "re-read" of schedule.md and hid the very bug that had already been reproduced.
    """
    risky: list[str] = []
    root = root or pathlib.Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            awaits = sorted(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await))
            if not awaits:
                continue
            for res, (reads, writes) in _RESOURCES.items():
                events = sorted(
                    (n.lineno, "R" if _call_name(n) in reads else "W")
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and _call_name(n) in (reads | writes))
                for i, (line, kind) in enumerate(events):
                    if kind != "R":
                        continue
                    for line2, kind2 in events[i + 1:]:
                        if kind2 == "R":
                            break
                        if kind2 == "W":
                            if any(line < a < line2 for a in awaits):
                                risky.append(
                                    f"{path.name}::{fn.name} [{res}] "
                                    f"read line {line} -> await -> write line {line2}")
                            break
    return risky


def test_no_async_function_awaits_between_reading_and_writing_a_file():
    """The durable half of this slice.

    A write derived from a read taken before an await silently destroys whatever another
    writer did in between. If this fails, do the slow work first and then apply your
    delta with `pacing.update_state` (or re-read before writing, the way
    `chronos._run_night_task` already does).
    """
    risky = _windows_with_awaits()
    assert risky == [], (
        "lost-update window(s) reintroduced:\n  " + "\n  ".join(risky))


def test_the_guard_actually_detects_a_window(tmp_path):
    """A structural test that can never fail is worse than none -- prove it fires."""
    (tmp_path / "offender.py").write_text(
        "async def bad():\n"
        "    state = _load_state()\n"
        "    await something()\n"
        "    _save_state(state)\n", encoding="utf-8")
    found = _windows_with_awaits(tmp_path)
    assert found and "bad" in found[0], f"the guard missed an obvious window: {found}"


def test_the_guard_does_not_flag_the_correct_patterns(tmp_path):
    """Both accepted fixes must pass, or the guard would push people away from them:
    re-reading before the write (chronos._run_night_task) and applying a delta after
    the slow work (pacing.update_state)."""
    (tmp_path / "ok.py").write_text(
        "async def rereads():\n"
        "    state = _load_state()\n"
        "    _save_state(state)\n"
        "    await slow()\n"
        "    state = _load_state()\n"
        "    _save_state(state)\n"
        "\n"
        "async def delta():\n"
        "    await slow()\n"
        "    _update_state(lambda st: None)\n", encoding="utf-8")
    assert _windows_with_awaits(tmp_path) == []
