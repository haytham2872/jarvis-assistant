"""A03: per-source retrieval deadlines, so one slow store cannot stall a turn.

The roadmap's requirement — "Fetch independent candidates concurrently with per-source
deadlines and a total context deadline" — and its acceptance criterion, "a slow optional
source respects a deadline". Today `chat()` gathers eight retrievals with no bound at
all: a degraded Qdrant, a cold embedder or a slow mem0 hybrid search holds the whole turn
for as long as it takes.

**The numbers below are measured, not guessed -- and the first measurement was wrong.**
It was taken with every Qdrant client pointed at `localhost`, which on the Windows desk
resolves `::1` first while compose binds `127.0.0.1` only, so each connect stalled ~2s
(see backend/loopback.py). Those figures measured the misconfiguration, not the stores:

                          first measurement          after the loopback fix
    recall (mem0 hybrid)  p50 6.409s  max 24.091s    p50 0.255s  max 0.400s
    read_relevant_skill   p50 2.768s  max  3.019s    p50 0.060s  max 0.101s
    read_relevant_topic   p50 2.750s  max  2.791s    p50 0.054s  max 0.056s
    fast vault reads      p50 ~0.001s                unchanged

The bounds were tightened to match: a 12s recall bound over a 0.4s worst case let a
genuinely sick store hold a turn for twelve seconds. The new values still leave 15-40x
headroom over the corrected worst case, deliberately -- only the desk could be measured,
and the always-on VM is smaller and shares its CPU with background indexing (A08).

**Only optional sources are bounded.** `assign_session()` decides which session a turn
belongs to; dropping it would not degrade the turn, it would corrupt continuity. It is
left unbounded deliberately.

Since the sources run concurrently under one `gather`, the longest per-source deadline is
also the effective total context deadline — no separate mechanism needed.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

# Plain file reads: milliseconds in practice. 3s means "the disk is not responding".
FAST = 3.0
# Semantic search (embed + Qdrant query): measured p50 0.054-0.060s, max 0.101s.
SEMANTIC = 4.0
# mem0 hybrid recall: measured p50 0.255s, max 0.400s. Recall degrades to "no facts this
# turn" rather than to an error, so a miss costs context, never the turn.
RECALL = 6.0

# Recorded for the turn, read by the caller for logging. Reset per turn.
_timeouts: list[tuple[str, float]] = []
_durations: dict[str, float] = {}


def reset() -> None:
    _timeouts.clear()
    _durations.clear()


def timed_out() -> list[tuple[str, float]]:
    """(source_id, seconds) for sources that missed their deadline this turn."""
    return list(_timeouts)


def durations() -> dict[str, float]:
    """source_id -> seconds each bounded source took this turn (a timeout counts its
    deadline). Read by chat()'s per-turn timing line."""
    return dict(_durations)


async def bounded(coro, *, source_id: str, timeout: float, default=""):
    """Await `coro` with a deadline, degrading to `default` instead of raising.

    A timeout is a real event worth seeing — it means a store is unwell — so it is
    logged at warning and recorded, not swallowed. Other exceptions are left alone: the
    individual read functions already degrade internally and their own handling is
    better informed than anything this wrapper could do.
    """
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - started
        _timeouts.append((source_id, elapsed))
        log.warning(
            "context: %s exceeded its %.1fs deadline -- omitted from this turn",
            source_id, timeout)
        return default
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("context: %s failed (%s) -- omitted", source_id, exc)
        return default
    finally:
        _durations[source_id] = time.perf_counter() - started

