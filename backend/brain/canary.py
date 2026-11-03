"""backend/brain/canary.py — memory-layer self-diagnosis (the "canary").

CLAUDE.md documents, twice, a lesson learned the hard way: silent degradation
can hide REAL breakage indefinitely. The qdrant-client >= 1.17 removal of the
deprecated client.search() broke semantic recall across the entire app
(vault passages, exchanges, skills) and nothing noticed until a calibration
script that happened to expect hits exposed it. This module turns that
lesson into an active mechanism instead of hoping the next regression gets
caught by luck.

At startup and every _CHECK_INTERVAL_HOURS, write ONE fresh sentinel into
each of the three memory layers named in the failure, then read it back
through that layer's REAL production retrieval path (not a raw point-exists
check):
  - mem0        — Memory.add(..., infer=False) + Memory.search() (the same
                  call recall()/search_facts() use)
  - jarvis_semantic — semindex.index_file() + semindex.search(kind="vault")
                  (the exact function whose client.search() -> query_points
                  migration caused the historical bug)
  - jarvis_threads  — threads.record_thread() + threads.recent_threads()

Any layer that comes back empty gets DMed via the existing Discord notifier
-- the alert channel research.py already built, pointed at self-diagnosis.
Each probe cleans up its own prior sentinel before writing a new one (mem0
by tracked memory id, threads by a dedicated "canary" channel, semantic by
index_file's own per-filename overwrite) so this never accumulates.

Same DI pattern, same silent-degradation contract as background.py/
research.py/backup_loop.py: never imports backend.main. A probe that fails
to even RUN (Qdrant down, mem0 not loaded) counts as a failure for that
layer -- "couldn't even check" is exactly the kind of silence this exists
to catch, not something to shrug off.
"""

import time
import asyncio
import logging
import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from backend.brain import pacing

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_STATE_FILE   = _PROJECT_ROOT / ".jarvis_canary.json"

_CHECK_INTERVAL_HOURS = 4.0
_LOOP_SLEEP_S         = 3600.0   # re-check hourly against the state file's last_check
_STARTUP_DELAY_S      = 60.0    # let the real startup warmup (mem0/embedder) settle first

_SENTINEL_PREFIX  = "CANARY-SENTINEL-jarvis-memory-self-check"
_CANARY_CHANNEL   = "canary"     # dedicated threads.py channel, never a real conversation
_CANARY_FOLDER    = "self"
_CANARY_FILENAME  = "canary.md"
_RECENCY_WINDOW_S = 300.0        # threads probe: was the readback point written just now?

_notifier: Optional[Callable[[str], Awaitable[bool]]] = None


def set_notifier(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Injected from main.py: async fn(text) -> bool that DMs the owner."""
    global _notifier
    _notifier = fn


def _sentinel_text(now: float) -> str:
    """Fresh each check -- a timestamp keeps this run's sentinel distinct
    from whatever a previous check left behind."""
    return f"{_SENTINEL_PREFIX} {datetime.datetime.fromtimestamp(now).isoformat()}"


def _load_state() -> dict:
    return pacing.load_state(_STATE_FILE)


def _save_state(state: dict) -> None:
    pacing.save_state(_STATE_FILE, state, log_prefix="canary")


def _update_state(mutate) -> dict:
    """Atomic read-modify-write -- see pacing.update_state."""
    return pacing.update_state(_STATE_FILE, mutate, log_prefix="canary")


def due(last_check: float, now: float) -> bool:
    """Pure gate: has a full interval passed since the last check?"""
    return pacing.interval_elapsed(last_check, now, _CHECK_INTERVAL_HOURS * 3600.0)


def last_check_info() -> tuple[Optional[float], list[str]]:
    """Public accessor for runtime_status.py: (last_check_timestamp_or_None,
    failed_layers_from_that_check). No probes run here — just reads back
    what the most recent run_check() already persisted."""
    state = _load_state()
    last_check = state.get("last_check")
    failed = state.get("failed", [])
    return (float(last_check) if last_check is not None else None, list(failed))


# ---------------------------------------------------------------------------
# Probes — one per memory layer named in the historical failure
# ---------------------------------------------------------------------------

def _check_mem0_sync(sentinel: str, prev_memory_id: Optional[str]) -> tuple[bool, Optional[str]]:
    """Write via infer=False (skips mem0's LLM fact-extraction -- this tests
    the retrieval infrastructure, not extraction quality, and stores the
    sentinel verbatim so the read-back match is exact), then verify TWO
    things through mem0's own production APIs:

      (a) mem.search() -- the same call recall()/search_facts() use -- runs
          end to end and returns SOMETHING. This is what catches the
          historical failure class (the qdrant-client search() removal made
          every search silently return empty).
      (b) mem.get(new_id) returns the just-written sentinel -- the write
          actually persisted and is readable back.

    The pass criterion is deliberately NOT "the sentinel appears in the
    search results" anymore. That criterion false-positived live
    (2026-09-01 21:23): mem0 2.0.1 scores hits as
    (semantic + bm25 + entity_boost) / 2.5, and an infer=False sentinel can
    never earn an entity boost -- its ceiling is 0.8 -- while REAL
    conversation memories *about the canary itself* (accumulated from
    sessions discussing this very feature) score 0.88+ on all three
    signals. Once ~20 such memories existed, the sentinel was mathematically
    locked out of the top-k and the probe reported a healthy layer as
    broken. Ranking position against an unboundedly-growing crowd of real
    memories is not a memory-health signal; pipeline-runs + write-readable
    is. (Same live pass also caught that `limit=` was silently swallowed by
    mem0 2.0.1's **kwargs -- the parameter is `top_k` now.)

    Returns (found, new_memory_id) so next check can delete this one first.
    """
    try:
        from backend.brain.memory import _get_mem, USER_ID
        mem = _get_mem()
        if mem is None:
            return False, prev_memory_id

        if prev_memory_id:
            try:
                mem.delete(prev_memory_id)
            except Exception as e:
                log.debug("canary: mem0 prior-sentinel cleanup failed: %s", e)

        add_result = mem.add([{"role": "user", "content": sentinel}], user_id=USER_ID, infer=False)
        results = add_result.get("results", []) if isinstance(add_result, dict) else []
        new_id = results[0]["id"] if results else None

        # (a) the production search pipeline runs end to end
        hits = mem.search(sentinel, filters={"user_id": USER_ID}, top_k=5)
        hit_results = hits.get("results", []) if isinstance(hits, dict) else (hits or [])
        search_alive = bool(hit_results)

        # (b) the just-written sentinel is readable back by id
        write_readable = False
        if new_id:
            try:
                got = mem.get(new_id)
                write_readable = sentinel in ((got or {}).get("memory") or "")
            except Exception as e:
                log.debug("canary: mem0 get-back failed: %s", e)

        return (search_alive and write_readable), new_id
    except Exception as e:
        log.warning("canary: mem0 probe failed: %s", e)
        return False, prev_memory_id


async def _check_semantic(sentinel: str) -> bool:
    """index_file()/search(kind='vault') -- the exact functions whose
    client.search() -> client.query_points() migration caused the historical
    silent-recall-failure. Self-cleaning: index_file overwrites the prior
    chunks for this (folder, filename) automatically."""
    try:
        from backend.brain import semindex
        indexed = await semindex.index_file(_CANARY_FOLDER, _CANARY_FILENAME, sentinel)
        if not indexed:
            return False
        hits = await semindex.search(sentinel, kind="vault", folder=_CANARY_FOLDER)
        return any(sentinel in h.get("text", "") for h in hits)
    except Exception as e:
        log.warning("canary: semantic-index probe failed: %s", e)
        return False


async def _check_threads(sentinel: str, now: float) -> bool:
    """record_thread()/recent_threads() on a dedicated 'canary' channel that
    never mixes with real conversation history. Checks recency rather than
    exact text, since a low-significance thread only keeps a heuristic
    summary -- existence-and-freshness is what this probe actually needs to
    confirm (the collection is reachable, writable, and readable)."""
    try:
        from backend.brain import threads
        await threads.delete_channel_threads(_CANARY_CHANNEL)
        await threads.record_thread(sentinel, sentinel, session_id="canary", channel=_CANARY_CHANNEL)
        recent = await threads.recent_threads(limit=5, channel=_CANARY_CHANNEL)
        return any((now - float(t.get("timestamp", 0.0))) < _RECENCY_WINDOW_S for t in recent)
    except Exception as e:
        log.warning("canary: threads probe failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_check() -> dict:
    """Run all three probes once. Returns {"mem0_ok", "semantic_ok",
    "threads_ok", "failed": [...]}. DMs an alert iff any layer failed."""
    now = time.time()
    sentinel = _sentinel_text(now)
    state = _load_state()

    loop = asyncio.get_event_loop()
    mem0_ok, new_mem0_id = await loop.run_in_executor(
        None, _check_mem0_sync, sentinel, state.get("mem0_memory_id")
    )
    semantic_ok = await _check_semantic(sentinel)
    threads_ok = await _check_threads(sentinel, now)

    layers = (("mem0", mem0_ok), ("jarvis_semantic", semantic_ok), ("jarvis_threads", threads_ok))
    failed = [name for name, ok in layers if not ok]
    result = {"mem0_ok": mem0_ok, "semantic_ok": semantic_ok, "threads_ok": threads_ok, "failed": failed}

    # A04 slice 4: apply the delta to FRESH state rather than writing back a dict read
    # before three awaited probes. run_check() is called both at startup and from the
    # 4h loop, so two checks can in principle overlap.
    def _record(st):
        st["mem0_memory_id"] = new_mem0_id
        st["last_check"] = now
        st["failed"] = failed
    _update_state(_record)

    if failed:
        log.warning("canary: memory self-check FAILED for: %s", failed)
        if _notifier is not None:
            plural = "s" if len(failed) > 1 else ""
            try:
                await _notifier(
                    f"Something's wrong with my memory — the {', '.join(failed)} "
                    f"layer{plural} just failed a self-check (wrote a test entry, "
                    "couldn't read it back). Worth looking into soon — this kind of "
                    "thing can go silently broken for a long time otherwise."
                )
            except Exception as e:
                log.debug("canary: alert failed: %s", e)
    else:
        log.info("canary: all memory layers healthy")
    return result


async def _cycle() -> None:
    state = _load_state()
    if not due(float(state.get("last_check", 0.0)), time.time()):
        return
    await run_check()


async def run_loop() -> None:
    """Started as a background task from main.py's lifespan.

    Unlike research.py/backup_loop.py, the FIRST check is not deferred a
    full interval -- the whole point is catching breakage early, and a
    silently-broken memory layer is worth knowing about from minute one, not
    four hours in. A short startup delay just lets the real warmup (mem0,
    embedder) settle first so the first check isn't needlessly slow. This
    startup check stays bespoke (outside pacing.run_periodic) precisely
    because it's the one thing that ISN'T shaped like every other loop here.
    """
    log.info("canary: memory self-check loop started (every %.0fh)", _CHECK_INTERVAL_HOURS)
    await asyncio.sleep(_STARTUP_DELAY_S)
    try:
        await run_check()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("canary: startup check failed: %s", e)

    await pacing.run_periodic(
        name="canary", check_interval_s=_LOOP_SLEEP_S, on_cycle=_cycle,
    )
