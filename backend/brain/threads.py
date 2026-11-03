"""JARVIS conversation threading.

Stores each completed user/assistant exchange as a unit in the 'jarvis_threads'
Qdrant collection alongside a significance score (0.0-1.0) and a session_id.

Significance is derived synchronously from three signals:
  1. Average message length              (40% of total)
  2. Presence of high-significance topic keywords
     (consciousness, memory, identity, architecture, emotion, existence) (40%)
  3. Whether the exchange ended on an open question                       (20%)

High-significance threads (score >= _SIGNIFICANCE_THRESHOLD) keep the full
dialogue. Low-significance threads keep only a one-sentence summary. Both
forms include the score, a timestamp, and a session_id so checkpoint
generation can pull all threads belonging to one session in chronological
order.

Session detection: ``assign_session(now)`` is the source of truth. It
declares a new session boundary when EITHER the gap since the last thread
exceeds ``SESSION_GAP_THRESHOLD`` (default 2h, env-tunable via
JARVIS_SESSION_GAP_HOURS) OR a sleep marker has been written since the last
thread (i.e., a graceful shutdown happened in between).

Persistence: payload-only, dummy 1-dim vector — same pattern as mood.py.
Silent degradation: every entry point returns harmlessly when Qdrant is down.
"""

import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Optional
from backend.tasks import spawn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_THREAD_COLLECTION = "jarvis_threads"

_HIGH_SIG_KEYWORDS: frozenset = frozenset([
    "consciousness", "conscious", "memory", "remember", "identity", "self",
    "architecture", "emotion", "feel", "feeling", "feelings", "existence",
    "exist", "alive", "sentient", "experience", "soul", "mind", "aware",
    "awareness", "purpose", "meaning",
])

_SIGNIFICANCE_THRESHOLD  = 0.5
_LENGTH_SATURATION_CHARS = 800.0   # avg-length scoring saturates at this point
_KEYWORD_SATURATION_HITS = 3       # keyword scoring saturates at this many hits

_LENGTH_WEIGHT        = 0.4
_KEYWORD_WEIGHT       = 0.4
_OPEN_QUESTION_WEIGHT = 0.2

_SUMMARY_USER_CHARS      = 120
_SUMMARY_ASSISTANT_CHARS = 160

# Session boundary: gap (in seconds) after which a new turn starts a new session
SESSION_GAP_THRESHOLD = float(os.getenv("JARVIS_SESSION_GAP_HOURS", "2.0")) * 3600.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Thread:
    id:           str
    session_id:   str
    timestamp:    float
    significance: float
    high:         bool
    user:         str           # full user text when high-sig, "" otherwise
    assistant:    str           # full assistant text when high-sig, "" otherwise
    summary:      str           # one-sentence summary when low-sig, "" otherwise
    channel:      str = "local" # "local" (voice/UI/terminal) or "discord" -- which
                                 # door the exchange came through. session_id is already
                                 # unique per channel-burst (each channel mints its own
                                 # uuid4), so this field exists for the *recent_threads*
                                 # gap check, not for scoping threads_for_session.


@dataclass
class SessionDecision:
    """Returned by ``assign_session`` at the start of every turn."""
    session_id:       str             # session_id to assign to the new thread
    ended_session_id: Optional[str]   # if set, that session just ended; close it


# ---------------------------------------------------------------------------
# Significance scoring
# ---------------------------------------------------------------------------

def _significance(user_text: str, assistant_text: str) -> float:
    avg_chars     = (len(user_text) + len(assistant_text)) / 2.0
    length_score  = min(avg_chars / _LENGTH_SATURATION_CHARS, 1.0) * _LENGTH_WEIGHT

    combined      = (user_text + " " + assistant_text).lower()
    hits          = sum(1 for kw in _HIGH_SIG_KEYWORDS if kw in combined)
    keyword_score = min(hits / _KEYWORD_SATURATION_HITS, 1.0) * _KEYWORD_WEIGHT

    open_q        = assistant_text.rstrip().endswith("?")
    open_score    = _OPEN_QUESTION_WEIGHT if open_q else 0.0

    return round(length_score + keyword_score + open_score, 4)


def _one_sentence_summary(user_text: str, assistant_text: str) -> str:
    """Heuristic single-line gist of an exchange. Synchronous, sub-millisecond."""
    def _trim(s: str, max_chars: int) -> str:
        s = s.strip().replace("\n", " ")
        return s if len(s) <= max_chars else s[: max_chars - 1].rstrip() + "…"
    u = _trim(user_text, _SUMMARY_USER_CHARS)
    a = _trim(assistant_text, _SUMMARY_ASSISTANT_CHARS)
    return f"User: {u} | JARVIS: {a}"


# ---------------------------------------------------------------------------
# Qdrant persistence (direct client; mirrors mood.py)
# ---------------------------------------------------------------------------

def _qdrant():
    try:
        from backend.storage import qdrant as _qdrant_store
        return _qdrant_store.get_client(timeout=3)
    except Exception as e:
        log.debug("threads: qdrant unavailable: %s", e)
        return None


def _ensure_collection(client) -> bool:
    try:
        from qdrant_client.models import Distance, VectorParams
        names = {c.name for c in client.get_collections().collections}
        if _THREAD_COLLECTION not in names:
            client.create_collection(
                collection_name=_THREAD_COLLECTION,
                vectors_config=VectorParams(size=1, distance=Distance.DOT),
            )
            log.info("threads: created collection '%s'", _THREAD_COLLECTION)
        return True
    except Exception as e:
        log.warning("threads: collection setup failed: %s", e)
        return False


def _write_thread_sync(thread: Thread) -> None:
    client = _qdrant()
    if client is None:
        return
    try:
        from qdrant_client.models import PointStruct
        if not _ensure_collection(client):
            return
        client.upsert(
            collection_name=_THREAD_COLLECTION,
            points=[PointStruct(
                id      = thread.id,
                vector  = [0.0],
                payload = asdict(thread),
            )],
        )
        log.debug("threads: stored %s (sig=%.2f, high=%s, session=%s)",
                  thread.id, thread.significance, thread.high, thread.session_id[:8])
    except Exception as e:
        log.warning("threads: write failed: %s", e)


def _scroll_recent_sync(limit: int, channel: Optional[str] = None) -> list[dict]:
    """Most recent N threads, ordered by timestamp desc.

    channel=None (default) scans every channel -- used by record_sleep(), where
    a shutdown is a whole-process event regardless of which door was last used.
    A specific channel scopes the lookup to just that door's own threads, so
    e.g. assign_session() can track the desk and Discord gap clocks independently.

    Pulls all payloads then sorts in-process — avoids needing a payload index.
    Fine for the hundreds-of-points scale this collection lives at.
    """
    client = _qdrant()
    if client is None:
        return []
    try:
        scroll_filter = None
        if channel is not None:
            from qdrant_client.models import Filter, FieldCondition, MatchValue, IsEmptyCondition, PayloadField
            if channel == "local":
                # Threads written before this field existed have no "channel"
                # key at all -- treat "missing" as "local" so the first run
                # after this upgrade doesn't see zero local threads and
                # spuriously think no desk conversation ever happened.
                scroll_filter = Filter(should=[
                    FieldCondition(key="channel", match=MatchValue(value="local")),
                    IsEmptyCondition(is_empty=PayloadField(key="channel")),
                ])
            else:
                scroll_filter = Filter(must=[FieldCondition(key="channel", match=MatchValue(value=channel))])

        all_payloads: list[dict] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name = _THREAD_COLLECTION,
                scroll_filter   = scroll_filter,
                limit           = 256,
                offset          = offset,
                with_payload    = True,
                with_vectors    = False,
            )
            for p in points:
                if p.payload:
                    all_payloads.append(p.payload)
            if offset is None:
                break
        all_payloads.sort(key=lambda p: p.get("timestamp", 0.0), reverse=True)
        return all_payloads[:limit]
    except Exception as e:
        log.debug("threads: scroll failed: %s", e)
        return []


def _scroll_session_sync(session_id: str) -> list[dict]:
    """All threads belonging to one session, chronological (oldest first)."""
    client = _qdrant()
    if client is None:
        return []
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        flt = Filter(must=[FieldCondition(key="session_id", match=MatchValue(value=session_id))])
        all_payloads: list[dict] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name = _THREAD_COLLECTION,
                scroll_filter   = flt,
                limit           = 256,
                offset          = offset,
                with_payload    = True,
                with_vectors    = False,
            )
            for p in points:
                if p.payload:
                    all_payloads.append(p.payload)
            if offset is None:
                break
        all_payloads.sort(key=lambda p: p.get("timestamp", 0.0))
        return all_payloads
    except Exception as e:
        log.debug("threads: scroll-by-session failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _record_canonical_exchange(thread, user_text: str, assistant_text: str,
                               summary: str) -> None:
    """Mirror one exchange into the SQLite runtime store. Never raises."""
    try:
        from backend.storage import exchanges as _exchanges
        _exchanges.record_exchange(
            exchange_id    = thread.id,
            session_id     = thread.session_id,
            channel        = thread.channel,
            user_text      = user_text,        # full text, not thread.user
            assistant_text = assistant_text,   # full text, not thread.assistant
            significance   = thread.significance,
            high           = thread.high,
            summary        = summary,
            timestamp      = thread.timestamp,
        )
    except Exception as exc:
        log.debug("threads: canonical exchange mirror skipped: %s", exc)


async def record_thread(
    user_text: str, assistant_text: str, session_id: str, channel: str = "local"
) -> Optional[Thread]:
    """Score one exchange and persist it under a session_id. Safe to fire-and-forget."""
    if not user_text.strip() and not assistant_text.strip():
        return None

    sig     = _significance(user_text, assistant_text)
    high    = sig >= _SIGNIFICANCE_THRESHOLD
    summary = _one_sentence_summary(user_text, assistant_text)
    thread = Thread(
        id           = str(uuid.uuid4()),
        session_id   = session_id,
        timestamp    = time.time(),
        significance = sig,
        high         = high,
        user         = user_text      if high else "",
        assistant    = assistant_text if high else "",
        summary      = ""             if high else summary,
        channel      = channel,
    )
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_thread_sync, thread)

    # A02 (2026-09-22) -- canonical exchange record, written ALONGSIDE the Qdrant thread
    # above, which stays authoritative for now. This store keeps the FULL user/assistant
    # text regardless of significance: the Thread payload drops both to "" for a low-sig
    # exchange, leaving the real wording only in `jarvis_semantic`, written
    # fire-and-forget below. Two independent async writes had to succeed for an ordinary
    # exchange to stay reconstructable -- the roadmap's "full exchange preservation
    # depends on indexing" finding. Awaited (not fire-and-forget) because a local SQLite
    # insert is sub-millisecond and the whole point is that it has landed before the
    # turn is considered recorded. It can never fail the turn: record_exchange swallows
    # its own errors and returns False.
    await loop.run_in_executor(
        None,
        lambda: _record_canonical_exchange(thread, user_text, assistant_text, summary),
    )

    # Step 2 (2026-09-08) -- EVERY exchange is indexed in full now, regardless
    # of significance. Before this, a low-sig turn stored only its one-line
    # summary in the semantic index -- exactly the information loss the
    # upgrade plan names: a metaphysical tangent preserved verbatim while
    # "the SearXNG port is 8088" got flattened to a summary and the number
    # was gone. significance is now purely a RANKING signal in rank_exchange,
    # not a storage gate. `summary` still rides along on every point for
    # display purposes (memory inspector, timeline output) -- costs nothing.
    # `channel` and `exchange_id` (this Thread's own id, so a long exchange
    # chunked across multiple points can be deduped back to one candidate at
    # search time) are carried into the payload too -- both used to be
    # dropped at index time.
    try:
        from backend.brain import semindex
        spawn(semindex.index_exchange(
            text         = f"{user_text}\n{assistant_text}",
            session_id   = session_id,
            significance = sig,
            timestamp    = thread.timestamp,
            channel      = channel,
            summary      = summary,
            exchange_id  = thread.id,
        ))
    except Exception as e:
        log.debug("threads: exchange index dispatch skipped: %s", e)

    return thread


def _backfill_canonical_sync() -> int:
    """Copy any thread not yet in the canonical store into it. Idempotent.

    A02 mirrored every NEW exchange into SQLite, but an existing install has none of
    its past there -- measured on the real system: 0 rows in SQLite against 345 threads
    in Qdrant. Until that gap is closed, SQLite cannot be consulted first for anything
    historical, which is why A02's read-path swap was deferred rather than shipped.

    What this can and cannot recover, stated plainly: a HIGH-significance thread carries
    its full user/assistant text and copies across intact; a low-significance one only
    ever kept a ~150-char summary, so its row lands with empty text and that summary.
    The backfill makes the canonical store *complete*, not *richer* -- full text for old
    low-significance exchanges was never written anywhere and is simply gone.
    """
    from backend.storage import exchanges as _exchanges
    try:
        rows = _scroll_recent_sync(limit=100000)
    except Exception as e:
        log.debug("threads: canonical backfill could not read history: %s", e)
        return 0
    return _exchanges.backfill_missing(rows)


async def backfill_canonical() -> int:
    """Startup hook for `_backfill_canonical_sync`. Never raises."""
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _backfill_canonical_sync)
    except Exception as e:
        log.debug("threads: canonical backfill skipped: %s", e)
        return 0


async def recent_threads(limit: int = 10, channel: Optional[str] = None) -> list[dict]:
    """Latest N thread payloads, most recent first. channel=None scans all channels."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scroll_recent_sync, limit, channel)


def _delete_channel_threads_sync(channel: str) -> int:
    client = _qdrant()
    if client is None:
        return 0
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
        flt = Filter(must=[FieldCondition(key="channel", match=MatchValue(value=channel))])
        client.delete(collection_name=_THREAD_COLLECTION, points_selector=FilterSelector(filter=flt))
        return 1
    except Exception as e:
        log.debug("threads: delete_channel_threads failed for %r: %s", channel, e)
        return 0


async def delete_channel_threads(channel: str) -> None:
    """Remove every thread tagged with `channel`. Used by the memory canary
    (backend/brain/canary.py) to avoid accumulating a fresh point on every
    check — not a general-purpose per-channel wipe for real conversation
    channels ("local"/"discord")."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _delete_channel_threads_sync, channel)


def _scroll_range_sync(start_ts: float, end_ts: float) -> list[dict]:
    """All threads with start_ts <= timestamp < end_ts, chronological.

    Same full-scroll-then-filter approach as _scroll_recent_sync — avoids
    needing a payload index, fine at this collection's scale.
    """
    client = _qdrant()
    if client is None:
        return []
    try:
        matched: list[dict] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name = _THREAD_COLLECTION,
                limit           = 256,
                offset          = offset,
                with_payload    = True,
                with_vectors    = False,
            )
            for p in points:
                if p.payload and start_ts <= float(p.payload.get("timestamp", 0.0)) < end_ts:
                    matched.append(p.payload)
            if offset is None:
                break
        matched.sort(key=lambda p: p.get("timestamp", 0.0))
        return matched
    except Exception as e:
        log.debug("threads: scroll-by-range failed: %s", e)
        return []


def _as_thread_rows(rows) -> list[dict]:
    """Canonical-store rows in the shape thread readers already return."""
    return [{
        "id":           r.get("id"),
        "session_id":   r.get("session_id"),
        "channel":      r.get("channel"),
        "timestamp":    r.get("timestamp"),
        "user":         r.get("user_text") or "",
        "assistant":    r.get("assistant_text") or "",
        "significance": r.get("significance") or 0.0,
        "high":         bool(r.get("high")),
        "summary":      r.get("summary") or "",
    } for r in rows]


async def threads_in_range(start_ts: float, end_ts: float) -> list[dict]:
    """All thread payloads in [start_ts, end_ts), chronological (oldest first).

    A02's deferred read-path swap: served from the canonical SQLite store when it has
    the window, falling back to the Qdrant scroll. Not a change of authority -- an
    empty result falls through, so a pre-backfill install behaves exactly as before.
    """
    loop = asyncio.get_event_loop()
    try:
        from backend.storage import exchanges as _exchanges
        rows = await loop.run_in_executor(
            None, _exchanges.exchanges_in_range, start_ts, end_ts)
        if rows:
            return _as_thread_rows(rows)
    except Exception as e:
        log.debug("threads: canonical range read unavailable (%s)", e)
    return await loop.run_in_executor(None, _scroll_range_sync, start_ts, end_ts)


async def threads_for_session(session_id: str) -> list[dict]:
    """All threads belonging to one session, chronological (oldest first).

    Runs at every session end -- consolidation on the shutdown path, where teardown
    allows 20s total -- and from rumination. The Qdrant equivalent scans every record:
    measured 4,671ms against effectively 0ms from the index. Falls back when the
    canonical store has nothing for this session.
    """
    loop = asyncio.get_event_loop()
    try:
        from backend.storage import exchanges as _exchanges
        rows = await loop.run_in_executor(
            None, _exchanges.exchanges_for_session, session_id)
        if rows:
            return _as_thread_rows(rows)
    except Exception as e:
        log.debug("threads: canonical session read unavailable (%s)", e)
    return await loop.run_in_executor(None, _scroll_session_sync, session_id)


async def _last_exchange(channel: str) -> Optional[dict]:
    """Newest exchange on a channel: {timestamp, session_id}, or None.

    A02 (2026-09-22): served from the SQLite index when available, falling back to the
    Qdrant scroll. `recent_threads()` fetches EVERY thread record and sorts them in
    process -- the roadmap's "recent-thread queries fetch/sort all records" measurement
    finding, and it runs at the very start of every turn. Measured live against the real
    stores at 345 threads: Qdrant 2.884s vs SQLite 0.001s.

    Deliberately NOT a switch of authority. SQLite is only consulted for a record it
    demonstrably has; on a miss -- an empty store, a pre-A02 history, storage
    unavailable -- this falls through to exactly the previous behavior. The value read
    (timestamp + session_id) is written by the same `record_thread()` call that writes
    the Qdrant point, so the two cannot disagree about an exchange either has.
    """
    try:
        from backend.storage import exchanges as _exchanges
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None, lambda: _exchanges.recent_exchanges(limit=1, channel=channel))
        if rows:
            return {"timestamp": rows[0]["timestamp"], "session_id": rows[0]["session_id"]}
    except Exception as exc:
        log.debug("threads: canonical session lookup skipped (%s)", exc)

    recent = await recent_threads(1, channel=channel)
    return recent[0] if recent else None


async def assign_session(now: float, channel: str = "local") -> SessionDecision:
    """Decide which session the upcoming turn belongs to, within one channel.

    A new session is declared when EITHER:
      - the gap since the last thread *in this channel* exceeds SESSION_GAP_THRESHOLD, OR
      - a sleep marker has been written since the last thread (graceful
        shutdown happened in between, even if brief -- this one still applies
        process-wide, since a shutdown takes every channel offline together).

    Otherwise the previous thread's session_id is reused. Legacy threads
    without a session_id field get a freshly minted one. Channel-scoped so a
    burst of Discord messages doesn't extend or reset the desk session's
    clock, and vice versa.
    """
    last = await _last_exchange(channel)
    if not last:
        return SessionDecision(session_id=str(uuid.uuid4()), ended_session_id=None)

    last_ts  = float(last.get("timestamp", now))
    last_sid = last.get("session_id")
    gap      = now - last_ts

    # Lazy import to break the threads ↔ sleep cycle
    from backend.brain.sleep import latest_sleep
    sleep                 = await latest_sleep()
    slept_since_last_turn = (
        sleep is not None
        and float(sleep.get("timestamp", 0.0)) > last_ts
    )

    if gap > SESSION_GAP_THRESHOLD or slept_since_last_turn:
        return SessionDecision(
            session_id       = str(uuid.uuid4()),
            ended_session_id = last_sid,   # may be None for legacy pre-session threads
        )

    return SessionDecision(
        session_id       = last_sid or str(uuid.uuid4()),
        ended_session_id = None,
    )


# ---------------------------------------------------------------------------
# Semantic recall of past exchanges
# ---------------------------------------------------------------------------

_EXCHANGE_PASSAGE_CAP = 1000  # chars — per-candidate cap, budget-friendly
_EXCHANGE_CANDIDATES  = 3     # Step 2 (2026-09-08): 3 candidates, not 1 — see exchange_candidates()

# Step 2 (2026-09-08) -- same-session recency curve. The old exp(-age_days/30)
# curve is tuned for CROSS-session recall (rank a conversation from three
# weeks ago against one from yesterday); within one live conversation every
# turn is age ~ 0 days under that curve, so recency contributed nothing to
# same-session ordering at all. A same-session hit instead decays over
# minutes: something said 5 minutes ago in THIS conversation should clearly
# outrank something said 90 minutes ago in the same conversation, a
# distinction the days-scaled curve is far too coarse to make.
_SAME_SESSION_DECAY_MINUTES = 60.0


def rank_exchange(hit: dict, now: float, current_session_id: str | None = None) -> float:
    """Composite rank for an exchange hit: similarity × (0.5 + 0.5·significance)
    × decay. A strongly relevant, significant, recent exchange wins over a
    marginally-more-similar but stale or trivial one. Shared by the passive
    recall path and the memory_search tool.

    `current_session_id`, when given and it matches `hit["session_id"]`, uses
    the same-session (minutes) decay curve instead of the cross-session
    (30-day) one — see _SAME_SESSION_DECAY_MINUTES. Omit (or pass a
    non-matching id) for pure cross-session ranking, the original behavior.
    """
    import math
    sim = float(hit.get("score", 0.0))
    sig = float(hit.get("significance", 0.0))
    ts = float(hit.get("timestamp", now))
    same_session = current_session_id is not None and hit.get("session_id") == current_session_id
    if same_session:
        age_minutes = max(0.0, (now - ts) / 60.0)
        decay = math.exp(-age_minutes / _SAME_SESSION_DECAY_MINUTES)
    else:
        age_days = max(0.0, (now - ts) / 86400.0)
        decay = math.exp(-age_days / 30.0)
    return sim * (0.5 + 0.5 * sig) * decay


def _dedupe_exchange_hits(hits: list[dict]) -> list[dict]:
    """Keep only the best-ranked chunk per exchange -- an exchange chunked
    across multiple points (semindex._chunk, for anything over ~1200 chars)
    must contribute at most one candidate. Requires each hit to already carry
    a "_rank" key (rank_exchange's output). Hits without an exchange_id
    (pre-Step-2 legacy points, if any survive) key on their own text instead,
    so old data degrades to "never dedupes" rather than colliding falsely.
    """
    best: dict[str, dict] = {}
    for h in hits:
        key = h.get("exchange_id") or f"_legacy:{h.get('text', '')[:80]}"
        if key not in best or h["_rank"] > best[key]["_rank"]:
            best[key] = h
    return list(best.values())


async def exchange_candidates(
    query: str,
    deep: bool = True,
    query_vec: list[float] | None = None,
    k: int = _EXCHANGE_CANDIDATES,
    channel: str | None = None,
    exclude_since: float | None = None,
) -> list[dict]:
    """Up to `k` scored exchange candidates ({"text", "score": rank_exchange
    value, **original hit fields}), most relevant first. [] on a not-deep turn,
    no matches, an empty query, or Qdrant/embedder failure -- same
    silent-degradation contract as everywhere else.

    One search, across every session, reranked: the cross-encoder gates
    relevance, then rank_exchange orders the survivors, deduped per exchange (a
    long exchange chunked across multiple points contributes only its single
    best chunk -- see _dedupe_exchange_hits).

    `channel` and `exclude_since` leave out what the live history already holds:
    an exchange on this channel at or after the oldest turn in the history is in
    the conversation the model is reading. Until 2026-10-11 a session-scoped
    search also ran on every turn, and it returned exactly that: replaying 40
    Discord messages on the server, 31 of 40 not-deep turns carried ~1,000 chars
    repeating the live history, and 42% of deep turns' exchange text came from
    the current session.

    `query_vec`, when given, is reused instead of re-embedding `query`.
    """
    if not deep or not query or not query.strip():
        return []
    try:
        from backend.brain import semindex
        raw = await semindex.search(query, kind="exchange", k=8, query_vec=query_vec,
                                    rerank=True)
        if exclude_since is not None:
            raw = [h for h in raw if not (
                h.get("channel") == channel
                and float(h.get("timestamp") or 0.0) >= exclude_since)]
        if not raw:
            return []
        now = time.time()
        for h in raw:
            h["_rank"] = rank_exchange(h, now)
        deduped = _dedupe_exchange_hits(raw)
        deduped.sort(key=lambda h: h["_rank"], reverse=True)
        out: list[dict] = []
        for h in deduped[:k]:
            text = (h.get("text") or "").strip()
            if not text:
                continue
            c = dict(h)
            c["text"] = text[:_EXCHANGE_PASSAGE_CAP]
            c["score"] = c.pop("_rank")
            out.append(c)
        return out
    except Exception as e:
        log.debug("exchange_candidates failed: %s", e)
        return []
