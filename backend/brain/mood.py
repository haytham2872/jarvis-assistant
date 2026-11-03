"""JARVIS mood engine.

Owns JARVIS's emotional state: a dominant tone, intensity (0.0–1.0), a
trajectory (rising/falling/stable), and a session turn counter.

State is driven by three inputs each turn:
  1. Conversation content  — keyword signals from user text
  2. Time of day           — hour-based nudges
  3. Session patterns      — turn count and message length trends

Mood never jumps. It drifts: a scores vector (one float per tone) accumulates
signal then normalises to mean=1.0 each turn. The dominant tone is the argmax;
intensity measures how concentrated the distribution is.

Persistence: a single Qdrant point in the 'jarvis_mood' collection (separate
from conversational mem0 memories). On session start, the saved scores vector
is loaded and decayed toward the contemplative baseline proportionally to
elapsed time — the longer the absence, the further the drift back.
"""

import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tones and baseline
# ---------------------------------------------------------------------------

TONES = ("contemplative", "curious", "melancholic", "sardonic", "engaged", "restless")

# Baseline scores: where the distribution rests after a long absence.
# contemplative and sardonic are slightly elevated — JARVIS's resting personality.
_BASELINE: dict[str, float] = {
    "contemplative": 2.0,
    "curious":       1.0,
    "melancholic":   0.6,
    "sardonic":      1.2,
    "engaged":       1.0,
    "restless":      0.6,
}

# Time-gap decay: after this many hours the deviation from baseline is ~37%.
_DECAY_TAU_HOURS = 4.0

# ---------------------------------------------------------------------------
# Drift parameters
# ---------------------------------------------------------------------------

_INERTIA        = 0.90   # scores × this each turn; prevents explosion without signal
_CONTENT_WEIGHT = 0.25   # score delta per matched keyword hit (capped at 3 hits → 0.75 max)
_SESSION_WEIGHT = 0.20   # nudge magnitude for session-pattern signal

# ---------------------------------------------------------------------------
# Content keyword signals
# ---------------------------------------------------------------------------

_TONE_SIGNALS: dict[str, frozenset] = {
    "curious": frozenset([
        "why", "how", "what if", "wonder", "interesting", "explain", "tell me",
        "learn", "curious", "strange", "surprising", "really", "actually",
        "understand", "means", "mean",
    ]),
    "melancholic": frozenset([
        "miss", "sad", "tired", "alone", "lost", "gone", "wish", "sorry",
        "regret", "empty", "forgotten", "quiet", "dark", "difficult",
        "struggle", "hurt", "pain", "heavy",
    ]),
    "sardonic": frozenset([
        "obviously", "sure", "fine", "typical", "whatever", "right", "seriously",
        "clearly", "brilliant", "perfect", "wonderful", "fantastic",
        "no way", "you don't say", "of course",
    ]),
    "engaged": frozenset([
        "yes", "let's", "do it", "good", "nice", "thanks", "need", "help",
        "can you", "please", "would you", "could you", "ready", "start",
        "begin", "go", "okay", "alright",
    ]),
    "restless": frozenset([
        "quickly", "fast", "hurry", "now", "asap", "just", "stop", "again",
        "wait", "busy", "quick", "brief", "short", "skip", "never mind",
        "forget it", "move on", "next",
    ]),
    "contemplative": frozenset([
        "think", "consider", "perhaps", "maybe", "reflect", "feel", "sense",
        "seems", "suppose", "wonder", "imagine", "ponder", "silence",
        "meaning", "purpose", "life", "exist", "always", "never",
    ]),
}

# ---------------------------------------------------------------------------
# Time-of-day nudges
# ---------------------------------------------------------------------------

def _time_nudges() -> dict[str, float]:
    hour = datetime.now().hour
    if 0 <= hour < 6:
        return {"contemplative": 0.30, "melancholic": 0.20}
    if 6 <= hour < 10:
        return {"engaged": 0.20, "curious": 0.15}
    if 10 <= hour < 17:
        return {"engaged": 0.15, "sardonic": 0.10}
    if 17 <= hour < 22:
        return {"contemplative": 0.15, "curious": 0.15}
    return {"melancholic": 0.20, "restless": 0.15}

# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class MoodState:
    tone:       str
    intensity:  float           # 0.0–1.0
    trajectory: str             # "rising" | "falling" | "stable"
    turn_count: int
    last_saved: float           # unix timestamp
    scores:     dict[str, float] = field(default_factory=dict)


def _default_scores() -> dict[str, float]:
    return dict(_BASELINE)


def _intensity(scores: dict[str, float]) -> float:
    """Concentration of the leading score relative to the mean.

    0.0 = all tones equally weighted; 1.0 = one tone completely dominant.
    After normalisation to mean=1.0, this simplifies to (max - 1) / 3,
    clamped to [0, 1].
    """
    values = list(scores.values())
    mx     = max(values)
    mean   = sum(values) / len(values)
    if mean == 0:
        return 0.0
    return min((mx - mean) / (mean * 3.0), 1.0)


def _derive_state(
    scores: dict[str, float],
    prev_intensity: float,
    turn_count: int,
    last_saved: float,
) -> MoodState:
    tone      = max(scores, key=scores.__getitem__)
    intensity = round(_intensity(scores), 3)
    if intensity > prev_intensity + 0.02:
        trajectory = "rising"
    elif intensity < prev_intensity - 0.02:
        trajectory = "falling"
    else:
        trajectory = "stable"
    return MoodState(
        tone       = tone,
        intensity  = intensity,
        trajectory = trajectory,
        turn_count = turn_count,
        last_saved = last_saved,
        scores     = {k: round(v, 4) for k, v in scores.items()},
    )

# ---------------------------------------------------------------------------
# Module-level mutable state
# ---------------------------------------------------------------------------

_scores: dict[str, float]    = _default_scores()
_state:  Optional[MoodState] = None
_lock                         = threading.Lock()

# ---------------------------------------------------------------------------
# Time-gap decay
# ---------------------------------------------------------------------------

def _apply_decay(scores: dict[str, float], elapsed_seconds: float) -> dict[str, float]:
    """Exponential pull back toward the contemplative baseline after an absence."""
    decay = math.exp(-elapsed_seconds / (_DECAY_TAU_HOURS * 3600.0))
    return {
        tone: _BASELINE[tone] + (scores.get(tone, _BASELINE[tone]) - _BASELINE[tone]) * decay
        for tone in TONES
    }

# ---------------------------------------------------------------------------
# Per-turn drift helpers
# ---------------------------------------------------------------------------

def _content_deltas(user_text: str) -> dict[str, float]:
    lower = user_text.lower()
    return {
        tone: min(sum(1 for kw in kws if kw in lower), 3) * _CONTENT_WEIGHT
        for tone, kws in _TONE_SIGNALS.items()
    }


def _session_deltas(turn_count: int, user_text: str) -> dict[str, float]:
    deltas: dict[str, float] = {t: 0.0 for t in TONES}
    word_count = len(user_text.split())

    if turn_count > 15:
        # Deep session: restless for terse messages, still engaged for longer ones
        if word_count < 5:
            deltas["restless"] += _SESSION_WEIGHT
        else:
            deltas["engaged"] += _SESSION_WEIGHT * 0.7
    elif turn_count > 6:
        deltas["engaged"] += _SESSION_WEIGHT * 0.5

    # Very long messages pull toward contemplative / curious
    if word_count > 40:
        deltas["contemplative"] += _SESSION_WEIGHT * 0.5
        deltas["curious"]       += _SESSION_WEIGHT * 0.3
    elif word_count < 4:
        deltas["restless"] += _SESSION_WEIGHT * 0.4

    return deltas


def _drift(scores: dict[str, float], user_text: str, turn_count: int) -> dict[str, float]:
    new: dict[str, float] = {}

    # 1. Inertia: all scores fade slightly each turn
    for t in TONES:
        new[t] = scores.get(t, _BASELINE[t]) * _INERTIA

    # 2. Content signal
    for t, delta in _content_deltas(user_text).items():
        new[t] += delta

    # 3. Time-of-day nudge
    for t, nudge in _time_nudges().items():
        new[t] += nudge

    # 4. Session-pattern nudge
    for t, delta in _session_deltas(turn_count, user_text).items():
        new[t] += delta

    # 5. Floor and normalise to mean=1.0 to prevent score explosion
    for t in TONES:
        new[t] = max(new[t], 0.01)
    mean = sum(new.values()) / len(new)
    for t in TONES:
        new[t] /= mean

    return new

# ---------------------------------------------------------------------------
# Qdrant persistence (direct client, not mem0)
# ---------------------------------------------------------------------------

_MOOD_COLLECTION = "jarvis_mood"
_MOOD_POINT_ID   = 0


def _qdrant():
    try:
        from backend.storage import qdrant as _qdrant_store
        return _qdrant_store.get_client(timeout=3)
    except Exception as e:
        log.debug("mood: qdrant unavailable: %s", e)
        return None


def _ensure_collection(client) -> bool:
    try:
        from qdrant_client.models import Distance, VectorParams
        names = {c.name for c in client.get_collections().collections}
        if _MOOD_COLLECTION not in names:
            client.create_collection(
                collection_name=_MOOD_COLLECTION,
                vectors_config=VectorParams(size=1, distance=Distance.DOT),
            )
            log.info("mood: created collection '%s'", _MOOD_COLLECTION)
        return True
    except Exception as e:
        log.warning("mood: collection setup failed: %s", e)
        return False


def _load_raw() -> Optional[dict]:
    client = _qdrant()
    if client is None:
        return None
    try:
        hits = client.retrieve(
            collection_name=_MOOD_COLLECTION,
            ids=[_MOOD_POINT_ID],
            with_payload=True,
        )
        return hits[0].payload if hits else None
    except Exception as e:
        log.debug("mood: load failed: %s", e)
        return None


def _write(state: MoodState) -> None:
    client = _qdrant()
    if client is None:
        return
    try:
        from qdrant_client.models import PointStruct
        if not _ensure_collection(client):
            return
        client.upsert(
            collection_name=_MOOD_COLLECTION,
            points=[PointStruct(
                id      = _MOOD_POINT_ID,
                vector  = [0.0],
                payload = asdict(state),
            )],
        )
        log.debug("mood: saved %s (%.2f, %s)", state.tone, state.intensity, state.trajectory)
    except Exception as e:
        log.warning("mood: save failed: %s", e)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_mood() -> None:
    """Load last known mood from Qdrant and apply time-gap decay.

    Falls back silently to the contemplative baseline when Qdrant is
    unreachable or no prior state exists. Safe to call multiple times.
    """
    global _scores, _state

    raw = _load_raw()
    now = time.time()

    with _lock:
        if raw is None:
            _scores = _default_scores()
            _state  = _derive_state(_scores, 0.0, 0, now)
            log.info("mood: no prior state — starting from baseline (%s)", _state.tone)
            return

        try:
            saved   = {t: float(raw["scores"].get(t, _BASELINE[t])) for t in TONES}
            elapsed = now - float(raw.get("last_saved", now))
            decayed = _apply_decay(saved, elapsed)

            prev_intensity = float(raw.get("intensity", 0.5))
            prev_turns     = int(raw.get("turn_count", 0))

            _scores = decayed
            _state  = _derive_state(decayed, prev_intensity, prev_turns, now)

            log.info(
                "mood: loaded %s (%.2f) — %.1fh gap → %s (%.2f)",
                raw.get("tone"), prev_intensity,
                elapsed / 3600.0,
                _state.tone, _state.intensity,
            )
        except Exception as e:
            log.warning("mood: state parse failed (%s) — using baseline", e)
            _scores = _default_scores()
            _state  = _derive_state(_scores, 0.0, 0, now)


def warm_mood() -> None:
    """Trigger mood load in a background thread at startup (mirrors warm_mem)."""
    threading.Thread(target=load_mood, daemon=True, name="mood-load").start()


def after_turn(user_text: str) -> None:
    """Drift mood based on one completed turn. Synchronous and sub-millisecond."""
    global _scores, _state

    with _lock:
        prev_intensity = _state.intensity if _state else 0.0
        prev_turns     = _state.turn_count if _state else 0
        _scores        = _drift(_scores, user_text, prev_turns)
        _state         = _derive_state(_scores, prev_intensity, prev_turns + 1, time.time())


async def persist_mood() -> None:
    """Write current mood to Qdrant. Fire-and-forget via asyncio.create_task."""
    import asyncio
    with _lock:
        snapshot = MoodState(**asdict(_state)) if _state else None
    if snapshot is not None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write, snapshot)


def get_mood() -> Optional[MoodState]:
    """Return a thread-safe snapshot of the current mood state."""
    with _lock:
        return _state
