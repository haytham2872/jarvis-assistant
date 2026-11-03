"""Fire-and-forget work that cannot be dropped mid-run.

The event loop keeps only a weak reference to a task, so one nobody holds can be garbage
collected before it finishes (the asyncio docs: "Save a reference to the result of this
function, to avoid a task disappearing mid-execution"). Memory ingest, consolidation,
indexing and DMs were all started that way. spawn() keeps each task until it is done.
"""
import asyncio
from typing import Coroutine

_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Start `coro` in the background and hold it until it finishes."""
    try:
        task = asyncio.create_task(coro, name=name)
    except RuntimeError:                 # no running loop: don't leave it never awaited
        coro.close()
        raise
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task
