"""backend/brain/backup_loop.py — periodic in-process backup safety net.

teardown.ps1 already runs a backup on every graceful stop (backend.tools.
backup, invoked as its own process while Qdrant is still up, right before
`docker compose stop`). But a session that runs for days without ever being
stopped would otherwise go arbitrarily long without a backup ever happening.
This loop is the belt to that suspenders: every _BACKUP_INTERVAL_HOURS, run
the same create_backup() in a thread executor (it's fully synchronous) and
DM an alert only on TOTAL failure (vault zip AND every collection snapshot
both failed) -- a routine success stays silent, same philosophy as
everywhere else in this codebase; a completely failed backup is exactly the
kind of silent breakage worth surfacing instead of swallowing.

Same DI pattern as background.py/research.py: never imports backend.main.
Same silent-degradation contract: any failure logs and waits for the next
check.
"""

import time
import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from backend.tools.backup import create_backup
from backend.brain import pacing

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_STATE_FILE   = _PROJECT_ROOT / ".jarvis_backup.json"

_BACKUP_INTERVAL_HOURS = 24.0
_CHECK_INTERVAL_S      = 3600.0   # re-check hourly — cheap, avoids drift from a long single sleep

_notifier: Optional[Callable[[str], Awaitable[bool]]] = None


def set_notifier(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Injected from main.py: async fn(text) -> bool that DMs the owner."""
    global _notifier
    _notifier = fn


def _load_state() -> dict:
    return pacing.load_state(_STATE_FILE)


def _save_state(state: dict) -> None:
    pacing.save_state(_STATE_FILE, state, log_prefix="backup_loop")


def due(last_backup: float, now: float) -> bool:
    """Pure gate: has a full interval passed since the last periodic backup?"""
    return pacing.interval_elapsed(last_backup, now, _BACKUP_INTERVAL_HOURS * 3600.0)


async def _tick() -> None:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, create_backup)
    total_failure = not result["vault_ok"] and not result["collections_ok"]
    db_failed = result.get("database") == "failed"
    message = ""
    if total_failure:
        message = (
            "Heads up — my scheduled backup just failed completely (vault zip and "
            "every Qdrant collection snapshot). Worth checking Docker or disk space "
            "when you get a chance — this is the thing that protects all my memory."
        )
    elif db_failed:
        # The database holds the plan log, approvals, receipts and forecasts: a backup that
        # saved the vault but not this is not a good copy, and would otherwise look fine.
        message = (
            "Heads up — my scheduled backup could not copy the runtime database (the "
            "plans, approvals, receipts and forecasts). The vault and memory were saved. "
            "Worth checking disk space when you get a chance."
        )
    if message and _notifier is not None:
        try:
            await _notifier(message)
        except Exception as e:
            log.debug("backup_loop: failure alert failed: %s", e)


async def _cycle() -> None:
    state = _load_state()
    if not due(float(state.get("last_backup", 0.0)), time.time()):
        return
    state["last_backup"] = time.time()
    _save_state(state)
    await _tick()


async def run_loop() -> None:
    """Started as a background task from main.py's lifespan."""
    state = _load_state()
    if "last_backup" not in state:
        # Don't back up immediately at startup — startup is already the
        # busiest moment (model loads, warmup threads). teardown.ps1's
        # own backup already covers a normal stop/start cycle; this loop
        # only needs to catch sessions that run long past that.
        state["last_backup"] = time.time()
        _save_state(state)
    await pacing.run_periodic(
        name=f"backup (every {_BACKUP_INTERVAL_HOURS:.0f}h)",
        check_interval_s=_CHECK_INTERVAL_S,
        on_cycle=_cycle,
    )
