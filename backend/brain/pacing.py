"""backend/brain/pacing.py — shared scaffolding for JARVIS's periodic loops.

Five modules (research, chronos, backup_loop, canary, rumination) each run
their own paced background loop from main.py's lifespan. The code graph
(graphify-out/graph.json) found part of this literally: backup_loop.due()
and research.should_tick() were separately-named, separately-tested
reimplementations of the same "has an interval elapsed since last run"
check, and all five modules carried byte-identical JSON load/save
boilerplate for their own state files.

This extracts exactly the parts that were duplicated without forcing a
shape that doesn't fit everywhere. Two things really are the same operation
across all five modules: interval-elapsed gating and JSON state I/O, so they
move here. The while-loop/sleep/exception-handling scaffold is also
identical in all five and moves here as run_periodic(). The tick BODIES stay
in each module — chronos checks three independent due-conditions per cycle
(reminders, night window, brief) and rumination's eligibility depends on
thread content, not just elapsed time; neither fits a single due()/tick()
split, so neither is forced into interval_elapsed(). Each module keeps its
own state file (different schemas — canary tracks mem0_memory_id/failed
layers, research tracks pace/deferred_notifies, etc.), just delegates the
read/write mechanics here.

Same silent-degradation contract as everywhere else in this codebase: state
I/O never raises, degrading to {} on read failure and logging (never
raising) on write failure.
"""

import json
import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


def load_state(path: Path, *, strict: bool = False) -> dict:
    """Read JSON state. Legacy callers get {}; strict readers log/raise, except if absent."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        if strict:
            log.warning("pacing: state could not be read (%s): %s", path.name, exc)
            raise
        return {}


def save_state(path: Path, state: dict, *, log_prefix: str = "pacing") -> None:
    """Write a JSON state file. Logs (never raises) on failure."""
    try:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("%s: state write failed: %s", log_prefix, e)


def update_state(path: Path, mutate, *, log_prefix: str = "pacing") -> dict:
    """Read-modify-write a JSON state file in ONE uninterruptible step.

    A04 slice 4: the loops all had the same shape —

        state = _load_state()
        ...                     # an LLM call, a web search, a Discord DM
        _save_state(state)

    — and the write at the end was derived from a read taken *before* the await, so it
    clobbered anything another writer had put in the file meanwhile. Confirmed live for
    `schedule.md`: a reminder scheduled while chronos was sending a DM was silently
    destroyed, after `schedule_reminder` had already told the owner it was saved and
    verified by read-back. The same window existed on three state files, each with a real
    second writer (`/pace` during a minutes-long research tick;
    `pop_rumination_mention`, which runs on every turn, during rumination's LLM call).

    `mutate` receives the freshly-read state and modifies it in place (or returns a
    replacement). Because nothing here awaits, the event loop cannot interleave another
    writer between the read and the write — which is the actual guarantee needed, and is
    why this is a plain function rather than a lock: a lock held across those awaits
    would serialize a Discord command behind a multi-minute research tick.

    The contract for callers is therefore: **do the slow work first, then call this with
    a small synchronous mutator that applies only your own delta.**
    """
    state = load_state(path)
    try:
        replacement = mutate(state)
    except Exception as e:
        log.debug("%s: state mutate failed: %s", log_prefix, e)
        return state
    if replacement is not None:
        state = replacement
    save_state(path, state, log_prefix=log_prefix)
    return state


def interval_elapsed(last_ts: float, now: float, interval_s: Optional[float]) -> bool:
    """Pure gate: has interval_s passed since last_ts?

    interval_s=None means never due — the convention research.py's pace
    table already used for its "off" level ({"off": None, ...}), kept here
    so callers with an optional/disable-able interval don't need a special
    case of their own.
    """
    if interval_s is None:
        return False
    return (now - last_ts) >= interval_s


async def run_periodic(
    *,
    name: str,
    check_interval_s: float,
    on_cycle: Callable[[], Awaitable[None]],
    pausable: str | None = None,
) -> None:
    """Own the while-True/sleep/try-except/CancelledError scaffold every
    loop module previously reimplemented identically (5 copies, ~10 lines
    each, differing only in log wording).

    `on_cycle` is awaited once per `check_interval_s` and owns everything
    module-specific: due-checking, ticking, busy-checking. A CancelledError
    from `on_cycle` propagates immediately (pipeline shutdown must not be
    swallowed); any other exception is logged and the loop continues to the
    next cycle — the same "one bad tick never kills the loop" contract every
    caller already had by hand.

    `pausable` (P2): "work" makes the loop do nothing while any /pause is in force; "all"
    only while a pause that also holds reminders and the brief is. Checked at the top of every
    cycle, so a paused loop does nothing within one cycle. Housekeeping loops (backups, the
    canary, indexing, the self-model, the evolution log) leave it None and never stop.
    """
    log.info("%s: loop started", name)
    while True:
        await asyncio.sleep(check_interval_s)
        if pausable:
            from backend.brain import pause      # lazy: pause imports storage only, but keep the edge one-way
            if pause.is_paused(pausable):
                continue
        try:
            await on_cycle()
        except asyncio.CancelledError:
            log.info("%s: loop stopped", name)
            raise
        except Exception as e:
            log.warning("%s: cycle failed: %s", name, e)
