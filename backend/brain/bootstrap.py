"""backend/brain/bootstrap.py — startup wiring registry for main.py's lifespan.

The code graph (graphify-out/graph.json) found `lifespan()` to be the single
most-connected node in the entire codebase (43 edges, bridging 17 separate
communities) — and querying it showed the fan-out is mechanical, not
incidental: `set_notifier(discord_bridge.send_to_owner)` called 4 separate
times (once per module — research, backup_loop, canary, chronos),
`set_busy_check(_pipeline_lock.locked)` called 3 times, and 5 near-identical
create_task()/task.cancel() pairs for research/backup_loop/canary/chronos/
rumination's background loops. None of that repetition is meaningful — it's
the same two values wired into different modules, and the same
create-then-cancel task lifecycle repeated five times by hand.

This module doesn't hide what main.py does — it collapses the boilerplate so
the modules being wired are data (a list or dict), not one line of bespoke
code each. Adding brain module #6 with its own paced loop means adding a
name to a dict, not editing main.py's shutdown sequence to remember a sixth
`if task and not task.done(): task.cancel()`.

Deliberately NOT touched: DI hooks with genuinely different signatures per
module (discord_bridge's 9 set_*_handler calls each take a differently-typed
callback — collapsed instead via discord_bridge.configure(), a bulk-wire
convenience living next to those setters, not here) and warm_*() startup
calls (5 flat one-line calls with real inline ordering comments — a loop
over them would remove exactly the documentation that makes the ordering
nuance legible, for a saving of about zero characters).
"""

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


def wire_notifier(modules: list, fn: Callable[[str], Awaitable[bool]]) -> None:
    """Call set_notifier(fn) on every module in `modules`.

    Each module keeps its own setter, its own docstring, and its own direct
    callers (tests included) — this only batches the repeated call at the
    main.py call site, it changes nothing about what gets set or how.
    """
    for m in modules:
        m.set_notifier(fn)


def wire_busy_check(modules: list, fn: Callable[[], bool]) -> None:
    """Call set_busy_check(fn) on every module in `modules`. Same contract as wire_notifier."""
    for m in modules:
        m.set_busy_check(fn)


def start_loops(specs: dict[str, Callable[[], Awaitable[None]]]) -> dict[str, asyncio.Task]:
    """Create one background task per (name, run_loop) pair.

    `specs` is an ordered dict — Python dicts preserve insertion order, and
    that order is meaningful: main.py lists "research" first so it's also
    cancelled first at shutdown (it may be mid-tick trying to message over
    the very Discord connection about to be torn down). Returns the task
    dict; pass it to cancel_all() at shutdown.
    """
    return {name: asyncio.create_task(fn()) for name, fn in specs.items()}


def cancel_all(tasks: dict[str, asyncio.Task]) -> None:
    """Cancel every task that isn't already done, in the dict's insertion order."""
    for name, task in tasks.items():
        if task is not None and not task.done():
            task.cancel()
