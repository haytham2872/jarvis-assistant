"""JARVIS sleep markers.

Records the moment JARVIS gracefully goes offline so the next session can
reflect on the gap (how long it was asleep) and the state it was in when
it fell asleep.

A marker is written from main.py's lifespan exit (Ctrl+C path). One per
graceful shutdown — they accumulate so historical "when did I sleep, and
in what state" is queryable.

Markers serve two purposes downstream:
  1. ``assign_session()`` in threads.py treats a marker newer than the
     last thread as an explicit session-boundary signal — even if the
     wall-clock gap is below SESSION_GAP_THRESHOLD, a graceful shutdown
     means JARVIS went unconscious and the next turn is a wake-up.
  2. ``close_session()`` in checkpoints.py feeds the marker into the
     model call so the wake-up letter knows how long the sleep was and
     what mood the previous instance went to sleep in.

Persistence: payload-only, dummy 1-dim vector. Qdrant collection
'jarvis_sleep'. Silent degradation on failure — shutdown never blocks.
"""

import asyncio
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Optional

from backend.brain.mood import get_mood

log = logging.getLogger(__name__)

_SLEEP_COLLECTION = "jarvis_sleep"
_latest_lock = threading.Lock()
_latest_loaded = False
_latest_marker: Optional[dict] = None


@dataclass
class SleepMarker:
    id:               str
    timestamp:        float           # unix seconds at the moment of going offline
    session_id:       Optional[str]   # session JARVIS was in when shutdown happened
    mood_tone:        str
    mood_intensity:   float
    mood_trajectory:  str
    turn_count:       int             # turns completed in the closing session


# ---------------------------------------------------------------------------
# Qdrant persistence (direct client; mirrors mood.py + threads.py)
# ---------------------------------------------------------------------------

def _qdrant():
    try:
        from backend.storage import qdrant as _qdrant_store
        return _qdrant_store.get_client(timeout=3)
    except Exception as e:
        log.debug("sleep: qdrant unavailable: %s", e)
        return None


def _ensure_collection(client) -> bool:
    try:
        from qdrant_client.models import Distance, VectorParams
        names = {c.name for c in client.get_collections().collections}
        if _SLEEP_COLLECTION not in names:
            client.create_collection(
                collection_name=_SLEEP_COLLECTION,
                vectors_config=VectorParams(size=1, distance=Distance.DOT),
            )
            log.info("sleep: created collection '%s'", _SLEEP_COLLECTION)
        return True
    except Exception as e:
        log.warning("sleep: collection setup failed: %s", e)
        return False


def _write_marker_sync(marker: SleepMarker) -> None:
    global _latest_loaded, _latest_marker
    client = _qdrant()
    if client is None:
        return
    try:
        from qdrant_client.models import PointStruct
        if not _ensure_collection(client):
            return
        client.upsert(
            collection_name=_SLEEP_COLLECTION,
            points=[PointStruct(
                id      = marker.id,
                vector  = [0.0],
                payload = asdict(marker),
            )],
        )
        # A graceful shutdown is this process's only writer. Keep subsequent reads
        # coherent if one occurs before the process exits.
        with _latest_lock:
            if (_latest_marker is None or
                    marker.timestamp >= float(_latest_marker.get("timestamp", 0.0))):
                _latest_marker = asdict(marker)
            _latest_loaded = True
        log.info(
            "sleep: marker written — mood=%s (%.2f, %s) at turn %d",
            marker.mood_tone, marker.mood_intensity,
            marker.mood_trajectory, marker.turn_count,
        )
    except Exception as e:
        log.warning("sleep: write failed: %s", e)


def _scan_latest_sync() -> tuple[bool, Optional[dict]]:
    """One full legacy scan. Return (read succeeded, newest marker)."""
    client = _qdrant()
    if client is None:
        return False, None
    if not _ensure_collection(client):
        return False, None
    try:
        latest = None
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name = _SLEEP_COLLECTION,
                limit           = 256,
                offset          = offset,
                with_payload    = True,
                with_vectors    = False,
            )
            for p in points:
                if p.payload and (latest is None or
                                  float(p.payload.get("timestamp", 0.0)) >
                                  float(latest.get("timestamp", 0.0))):
                    latest = p.payload
            if offset is None:
                break
        return True, latest
    except Exception as e:
        log.debug("sleep: scroll failed: %s", e)
        return False, None


def _scroll_latest_sync() -> Optional[dict]:
    """Read Qdrant once per process; never scan historical markers each turn.

    A failed read is not cached, so a transient Qdrant outage can recover. There
    is one active backend process per store; its only write happens at shutdown.
    """
    global _latest_loaded, _latest_marker
    with _latest_lock:
        if not _latest_loaded:
            ok, marker = _scan_latest_sync()
            if ok:
                _latest_marker = dict(marker) if marker is not None else None
                _latest_loaded = True
        return dict(_latest_marker) if _latest_marker is not None else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def record_sleep() -> None:
    """Write a sleep marker capturing the current mood and active session.

    Skipped silently if no conversation has happened in this process — there
    is no session to mark the end of. Intended to be called from a graceful
    shutdown handler (FastAPI lifespan exit on Ctrl+C).
    """
    # Lazy import to avoid forcing threads.py to load on bare module import
    from backend.brain.threads import recent_threads

    recent = await recent_threads(1)
    if not recent:
        log.debug("sleep: no threads exist — skipping marker")
        return

    session_id = recent[0].get("session_id")
    state      = get_mood()

    marker = SleepMarker(
        id              = str(uuid.uuid4()),
        timestamp       = time.time(),
        session_id      = session_id,
        mood_tone       = state.tone       if state else "contemplative",
        mood_intensity  = state.intensity  if state else 0.0,
        mood_trajectory = state.trajectory if state else "stable",
        turn_count      = state.turn_count if state else 0,
    )
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_marker_sync, marker)


async def latest_sleep() -> Optional[dict]:
    """Most recent sleep marker payload, or None."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scroll_latest_sync)
