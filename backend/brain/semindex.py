"""Semantic index for JARVIS's long-term memory — one Qdrant collection.

`jarvis_semantic` (384-dim, COSINE) holds two kinds of retrievable passages,
distinguished by the payload `kind` field:

  - kind="vault"    — chunks of vault notes (topics, letters, people, …), so
                      the richest long-term store becomes searchable by meaning
                      rather than by filename keyword matching.
  - kind="exchange" — significant past user/assistant exchanges, so JARVIS can
                      recall *what was actually said* in a prior session, which
                      extracted mem0 facts cannot give.

One collection with kind-filtered searches — simpler ops than two collections.
Embeddings come from the shared `embeddings` module (thenlper/gte-small).

Silent degradation: every entry point returns harmlessly (or [] / None) when
Qdrant or the embedder is unavailable. The pipeline never crashes on a failure
here, exactly like the other memory layers.
"""

import os
import time
import uuid
import asyncio
import hashlib
import pathlib
import logging
import threading

from backend.brain import embeddings

log = logging.getLogger(__name__)

_COLLECTION = "jarvis_semantic"

# Chunking — paragraph-aware, ~1200 chars with ~150 overlap
_CHUNK_CHARS   = 1200
_CHUNK_OVERLAP = 150

# Default search gates -- used whenever a caller doesn't pass its own score_min
# (i.e. every PASSIVE recall path: memory.py's _semantic_vault_passage and
# threads.py's exchange_candidates, both called every turn to build the
# [Relevant vault note]/[past exchange] system-prompt blocks; also vault.py's
# read_relevant_topic).
#
# Hardening (2026-09-09) -- these were a stale, never-calibrated placeholder
# and the root cause of a live incident: mid-conversation about stock
# positions (Take Two, NVDA), JARVIS repeatedly derailed into narrating
# self/evolution.md and an unrelated same-session exchange about its own
# codebase, because that exchange kept clearing this floor and out-ranking
# genuinely relevant content on recency+significance. The 2026-06-11
# measurement documented in CLAUDE.md (Vault section) already established
# that gte-small's UNRELATED-query cosine baseline on this exact collection
# sits at 0.72-0.85 -- "an intuitive-looking floor like 0.5 filters nothing".
# That measurement produced _SEARCH_SCORE_MIN = {"vault": 0.80, "exchange":
# 0.75} in tools/vault.py for the on-demand memory_search TOOL, but these two
# passive-recall defaults were left at the old pre-calibration 0.30 -- three
# times below the noise floor of the same index, so passive recall was
# effectively ungated: whatever scored highest locally always got injected,
# relevant or not. Raised to match the tool's already-measured, already-
# proven floors -- same collection, same embedder, same noise ceiling, no
# reason passive injection should tolerate weaker evidence than an on-demand
# tool call does.
_VAULT_SCORE_MIN    = 0.80
_EXCHANGE_SCORE_MIN = 0.75
# Skill descriptions are short, so a slightly lower gate than topic recall.
_SKILL_SCORE_MIN    = 0.68


# ---------------------------------------------------------------------------
# Qdrant helpers (direct client; mirrors threads.py)
# ---------------------------------------------------------------------------

def _qdrant():
    try:
        from backend.storage import qdrant as _qdrant_store
        return _qdrant_store.get_client(timeout=3)
    except Exception as e:
        log.debug("semindex: qdrant unavailable: %s", e)
        return None


def _ensure_collection(client) -> bool:
    try:
        from qdrant_client.models import Distance, VectorParams
        names = {c.name for c in client.get_collections().collections}
        if _COLLECTION not in names:
            client.create_collection(
                collection_name=_COLLECTION,
                vectors_config=VectorParams(size=embeddings.EMBED_DIM, distance=Distance.COSINE),
            )
            log.info("semindex: created collection '%s' (%d-dim COSINE)",
                     _COLLECTION, embeddings.EMBED_DIM)
        return True
    except Exception as e:
        log.warning("semindex: collection setup failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk(text: str) -> list[str]:
    """Split text into ~_CHUNK_CHARS chunks with overlap, preferring paragraph breaks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= _CHUNK_CHARS:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + _CHUNK_CHARS, n)
        if end < n:
            # Prefer to break at a paragraph or sentence boundary within the window
            window = text[start:end]
            brk = window.rfind("\n\n")
            if brk == -1:
                brk = window.rfind(". ")
            if brk > _CHUNK_CHARS // 2:
                end = start + brk + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _delete_file_points_sync(client, folder: str, filename: str) -> None:
    """Remove any existing chunks for a (folder, filename) before re-indexing."""
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
        flt = Filter(must=[
            FieldCondition(key="kind",     match=MatchValue(value="vault")),
            FieldCondition(key="folder",   match=MatchValue(value=folder)),
            FieldCondition(key="filename", match=MatchValue(value=filename)),
        ])
        client.delete(collection_name=_COLLECTION, points_selector=FilterSelector(filter=flt))
    except Exception as e:
        log.debug("semindex: delete prior chunks failed: %s", e)


def _point_ids_sync(client, flt) -> list:
    """Every point id matching a filter. [] on failure (never raises).

    A05: the first half of "retire the old generation only after the new one is live".
    Returning [] on failure is deliberate — it means the retire step deletes nothing,
    leaving duplicates, which recall already collapses (`_dedupe_by_file` for vault,
    `exchange_id` for exchanges, single-best for skills). Duplicated is recoverable;
    deleted is not.
    """
    ids: list = []
    try:
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=_COLLECTION, scroll_filter=flt, limit=512,
                offset=offset, with_payload=False, with_vectors=False,
            )
            ids.extend(p.id for p in points)
            if offset is None:
                break
    except Exception as e:
        log.debug("semindex: could not list prior points: %s", e)
    return ids


def _delete_point_ids_sync(client, ids: list) -> None:
    """Retire an explicit set of points. Never raises."""
    if not ids:
        return
    try:
        from qdrant_client.models import PointIdsList
        client.delete(collection_name=_COLLECTION,
                      points_selector=PointIdsList(points=list(ids)))
    except Exception as e:
        # The new generation is already live, so this only leaves duplicates behind.
        log.warning("semindex: retiring the previous generation failed: %s", e)


def _vault_file_filter(folder: str, filename: str):
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    return Filter(must=[
        FieldCondition(key="kind",     match=MatchValue(value="vault")),
        FieldCondition(key="folder",   match=MatchValue(value=folder)),
        FieldCondition(key="filename", match=MatchValue(value=filename)),
    ])


def _index_file_sync(folder: str, filename: str, content: str) -> int:
    client = _qdrant()
    if client is None:
        return 0
    if not _ensure_collection(client):
        return 0
    chunks = _chunk(content)
    if not chunks:
        _delete_file_points_sync(client, folder, filename)
        return 0
    vecs = embeddings._embed_many_sync(chunks, background=True)
    if not vecs:
        return 0
    try:
        from qdrant_client.models import PointStruct
        # A05: capture the previous generation, write the new one, retire the old one
        # only once the new is live. Deleting first meant an upsert that failed (Qdrant
        # restart, disk, process killed mid-teardown) left the note with NO points at
        # all -- invisible to semantic recall until the next startup backfill, which on
        # the always-on deployment can be weeks. Reproduced before this change.
        previous_ids = _point_ids_sync(client, _vault_file_filter(folder, filename))
        mtime = time.time()
        content_hash = _content_hash(content)
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "kind":     "vault",
                    "folder":   folder,
                    "filename": filename,
                    "text":     chunk,
                    "mtime":    mtime,
                    "content_hash": content_hash,
                },
            )
            for chunk, vec in zip(chunks, vecs)
        ]
        client.upsert(collection_name=_COLLECTION, points=points)
        _delete_point_ids_sync(client, previous_ids)   # new generation is live
        log.debug("semindex: indexed %s/%s (%d chunks)", folder, filename, len(points))
        return len(points)
    except Exception as e:
        log.warning("semindex: index_file failed for %s/%s: %s", folder, filename, e)
        return 0


async def index_file(folder: str, filename: str, content: str) -> int:
    """Index (or re-index) one vault file as kind='vault' chunks. Fire-and-forget friendly."""
    return await embeddings.run_derived(_index_file_sync, folder, filename, content)


def _index_exchange_sync(
    text: str, session_id: str, significance: float, timestamp: float,
    channel: str = "local", summary: str = "", exchange_id: str = "",
) -> bool:
    client = _qdrant()
    if client is None:
        return False
    if not _ensure_collection(client):
        return False
    # Step 2 (2026-09-08) -- gte-small's max_seq_length is 512 tokens (a hard
    # architectural limit: max_position_embeddings is also 512, confirmed by
    # reading the cached model config directly, not assumed) and
    # SentenceTransformer silently truncates past it with no error -- live-
    # verified: encoding ~2000 words produced a vector with 0.9999 cosine
    # similarity to encoding just the first ~505 words, i.e. everything past
    # the cutoff was completely ignored. An exchange longer than one chunk
    # (~1200 chars, same threshold the vault path already uses) is chunked
    # the same way, tagged with a shared exchange_id so search-time dedup
    # (threads._dedupe_exchange_hits) can collapse multiple chunks of one
    # exchange back down to its single best-scoring hit.
    chunks = _chunk(text)
    if not chunks:
        return False
    vecs = embeddings._embed_many_sync(chunks, background=True)
    if not vecs:
        return False
    try:
        from qdrant_client.models import PointStruct
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "kind":         "exchange",
                    "text":         chunk,
                    "summary":      summary,
                    "session_id":   session_id,
                    "channel":      channel,
                    "exchange_id":  exchange_id,
                    "significance": float(significance),
                    "timestamp":    float(timestamp),
                },
            )
            for chunk, vec in zip(chunks, vecs)
        ]
        client.upsert(collection_name=_COLLECTION, points=points)
        _note_points_indexed(len(points))
        return True
    except Exception as e:
        log.warning("semindex: index_exchange failed: %s", e)
        return False


async def index_exchange(
    text: str, session_id: str, significance: float, timestamp: float,
    channel: str = "local", summary: str = "", exchange_id: str = "",
) -> bool:
    """Index one exchange as kind='exchange' (chunked if long). Fire-and-forget friendly.

    `summary` (the heuristic one-liner, always computed by threads.py regardless
    of significance) rides along on every chunk's payload for display purposes
    (memory inspector, timeline output) — the significance score still decides
    nothing about indexing now (Step 2, 2026-09-08: every exchange is indexed in
    full), it's purely a ranking signal in rank_exchange. `exchange_id` should be
    the owning Thread's id, so multiple chunks of one long exchange dedupe back
    to one candidate at search time.
    """
    return await embeddings.run_derived(
        _index_exchange_sync, text, session_id, significance, timestamp,
        channel, summary, exchange_id,
    )


# ---------------------------------------------------------------------------
# Skills — kind='skill': indexed so memory_search can find text inside them.
# Which skill a turn uses is decided by the model from the list (skills.py).
# ---------------------------------------------------------------------------

def _frontmatter_fields(content: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Read selected fields from a SKILL.md YAML frontmatter block.

    Tiny inline scan — avoids a YAML dependency. Returns only the keys that
    are present; {} on malformed/absent frontmatter.
    """
    out: dict[str, str] = {}
    try:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return out
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, val = line.partition(":")
            k = key.strip()
            if sep and k in keys:
                out[k] = val.strip().strip("\"'")
    except Exception:
        pass
    return out


def _frontmatter_status(content: str) -> str:
    """The `status:` frontmatter field, defaulting to 'active'."""
    return (_frontmatter_fields(content, ("status",)).get("status") or "active").lower()


def _delete_skill_points_sync(client, name: str) -> bool:
    """Remove any existing chunks for a skill before re-indexing (overwrite support)."""
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
        flt = Filter(must=[
            FieldCondition(key="kind",       match=MatchValue(value="skill")),
            FieldCondition(key="skill_name", match=MatchValue(value=name)),
        ])
        client.delete(collection_name=_COLLECTION, points_selector=FilterSelector(filter=flt))
        return True
    except Exception as e:
        log.debug("semindex: delete prior skill chunks failed: %s", e)
        return False


# Serializes skill delete+upsert pairs — concurrent re-indexes of the same
# skill would otherwise interleave and can leave stale or duplicate points.
_skill_index_lock = threading.Lock()


def _index_skill_sync(name: str, content: str) -> int:
    with _skill_index_lock:
        return _index_skill_locked(name, content)


def _index_skill_locked(name: str, content: str) -> int:
    client = _qdrant()
    if client is None:
        return 0
    if not _ensure_collection(client):
        return 0
    chunks = _chunk(content)
    if not chunks:
        _delete_skill_points_sync(client, name)
        return 0
    vecs = embeddings._embed_many_sync(chunks, background=True)
    if not vecs:
        return 0
    status = _frontmatter_status(content)
    try:
        from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
        # A05: same write-new-then-retire-old ordering as the vault path above.
        previous_ids = _point_ids_sync(client, Filter(must=[
            FieldCondition(key="kind",       match=MatchValue(value="skill")),
            FieldCondition(key="skill_name", match=MatchValue(value=name)),
        ]))
        mtime = time.time()
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "kind":       "skill",
                    "folder":     "skills",
                    "filename":   f"{name}.md",
                    "skill_name": name,
                    "status":     status,
                    "text":       chunk,
                    "mtime":      mtime,
                    "content_hash": _content_hash(content),
                },
            )
            for chunk, vec in zip(chunks, vecs)
        ]
        client.upsert(collection_name=_COLLECTION, points=points)
        _delete_point_ids_sync(client, previous_ids)   # new generation is live
        log.debug("semindex: indexed skill %s (%d chunks, status=%s)", name, len(points), status)
        return len(points)
    except Exception as e:
        log.warning("semindex: index_skill failed for %s: %s", name, e)
        return 0


async def index_skill(name: str, content: str) -> int:
    """Index (or re-index) one skill as kind='skill' chunks. Fire-and-forget friendly."""
    return await embeddings.run_derived(_index_skill_sync, name, content)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _search_sync(
    query: str,
    kind: str,
    k: int,
    score_min: float,
    folder: str | None,
    query_vec: list[float] | None = None,
    session_id: str | None = None,
    rerank: bool = False,
) -> list[dict]:
    if rerank:
        return _search_reranked_sync(query, kind, k, score_min, folder, query_vec, session_id)
    client = _qdrant()
    if client is None:
        return []
    if query_vec is not None:
        # Caller already embedded this turn's text (usually intent.classify()'s
        # own embed, reused here) -- skip re-embedding the same string.
        vec = query_vec
    else:
        qvec = embeddings._embed_many_sync([query])
        if not qvec:
            return []
        vec = qvec[0]
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        conds = [FieldCondition(key="kind", match=MatchValue(value=kind))]
        if folder is not None:
            conds.append(FieldCondition(key="folder", match=MatchValue(value=folder)))
        if session_id is not None:
            # Step 2 (2026-09-08) -- session-scoped recall. Only meaningful for
            # kind="exchange" in practice, but not restricted to it here; the
            # filter is a plain payload match, harmless (and matches nothing)
            # on kinds that never carry a session_id field.
            conds.append(FieldCondition(key="session_id", match=MatchValue(value=session_id)))
        flt = Filter(must=conds)
        # qdrant-client >= 1.17 removed the deprecated search(); query_points
        # is the replacement (returns QueryResponse with .points).
        hits = client.query_points(
            collection_name=_COLLECTION,
            query=vec,
            query_filter=flt,
            limit=k,
            with_payload=True,
        ).points
        out: list[dict] = []
        for h in hits:
            if h.score is None or h.score < score_min:
                continue
            payload = dict(h.payload or {})
            payload["score"] = float(h.score)
            out.append(payload)
        return out
    except Exception as e:
        log.debug("semindex: search failed: %s", e)
        return []


def _search_reranked_sync(query, kind, k, score_min, folder, query_vec, session_id):
    """Cosine shortlist, then the cross-encoder decides relevance (see rerank.py).

    Unavailable reranker -> exactly the plain search's answer: Qdrant returns hits
    best-first, so the first k of the wider pool that clear `score_min` are the same
    hits a limit-k query would have returned.
    """
    from backend.brain import rerank
    pool = _search_sync(query, kind, max(k, rerank.POOL), min(score_min, rerank.PREFILTER),
                        folder, query_vec, session_id)
    kept = rerank.gate_sync(query, pool, lead_cosine=score_min)
    if kept is None:
        return [h for h in pool if h["score"] >= score_min][:k]
    return kept[:k]


async def search(
    query: str,
    kind: str = "vault",
    k: int = 3,
    score_min: float | None = None,
    folder: str | None = None,
    query_vec: list[float] | None = None,
    session_id: str | None = None,
    rerank: bool = False,
) -> list[dict]:
    """Semantic search within one kind. Returns payloads (with 'score') above score_min.

    `query_vec`, when given, is used directly instead of re-embedding `query` --
    the caller (typically engine.py's chat(), via intent.classify()'s own embed)
    already paid for this turn's embedding once, so every kind='vault'/'exchange'/
    'skill' search this turn can share it instead of re-embedding the identical
    text 3-4 times. `query` is still required (used for the empty-string guard
    and by callers with no precomputed vector), but its text is otherwise ignored
    once a vector is supplied.

    `session_id`, when given, restricts results to points carrying that exact
    session_id (Step 2, 2026-09-08) -- the session-scoped recall path. None
    (default) searches across every session, unrestricted.

    `rerank=True` (passive recall only) widens the cosine shortlist and lets the
    cross-encoder in rerank.py decide which hits are actually relevant, best first.
    """
    if not query or not query.strip():
        return []
    if score_min is None:
        score_min = _VAULT_SCORE_MIN if kind == "vault" else _EXCHANGE_SCORE_MIN
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _search_sync, query, kind, k, score_min, folder, query_vec, session_id, rerank
    )


def can_search(kind: str, folder: str | None = None) -> bool:
    """Whether there is an index to search for `kind` (and `folder`): Qdrant reachable and
    at least one point indexed. False on any failure. Lets a caller tell "the search found
    nothing relevant" from "there was nothing to search", which an empty result can't."""
    client = _qdrant()
    if client is None:
        return False
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        conds = [FieldCondition(key="kind", match=MatchValue(value=kind))]
        if folder is not None:
            conds.append(FieldCondition(key="folder", match=MatchValue(value=folder)))
        return client.count(collection_name=_COLLECTION, count_filter=Filter(must=conds),
                            exact=False).count > 0
    except Exception as e:
        log.debug("semindex: can_search failed: %s", e)
        return False


def interleave_by_rank(per_kind: list[list[dict]], top_k: int) -> list[dict]:
    """Merge per-kind result lists by rank position — best of each kind first.

    Scores across sources that aren't the same metric (e.g. mem0's hybrid
    score vs. this module's cosine similarity) aren't directly comparable, so
    position-wise interleave is used as the primary ordering signal (each
    kind's own #1 candidate is a fair "best shot", even if the exact numbers
    aren't on the same scale); within one rank position, raw score breaks
    ties. Shared by memory_search's kind='all' fan-out and memory.recall()'s
    facts/vault/exchange fusion.
    """
    merged: list[dict] = []
    pos = 0
    while len(merged) < top_k and any(pos < len(lst) for lst in per_kind):
        batch = [lst[pos] for lst in per_kind if pos < len(lst)]
        batch.sort(
            key=lambda h: h["score"] if isinstance(h.get("score"), (int, float)) else -1.0,
            reverse=True,
        )
        merged.extend(batch)
        pos += 1
    return merged[:top_k]


# ---------------------------------------------------------------------------
# Backfill — index vault files / high-sig threads not yet present
# ---------------------------------------------------------------------------

# The vault the reconcile pass reads. A module constant rather than a path built inside
# the function, so the pass can be tested against a temp vault instead of the real one.
_VAULT_ROOT = pathlib.Path(__file__).parent.parent.parent / "jarvis-vault"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _indexed_vault_hashes_sync(client) -> dict[tuple[str, str], str] | None:
    """{(folder, filename): content_hash} for every note present as kind='vault'.

    The hash is "" when the note's chunks predate hashes, or when its chunks disagree
    (two generations left side by side by a failed retire) -- both mean "re-index".
    Returns None if the scan itself failed: an empty answer would make every note look
    stale and turn one flaky scroll into a full re-index.
    """
    seen: dict[tuple[str, str], str] = {}
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        flt = Filter(must=[FieldCondition(key="kind", match=MatchValue(value="vault"))])
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=_COLLECTION,
                scroll_filter=flt,
                limit=256,
                offset=offset,
                with_payload=["folder", "filename", "content_hash"],
                with_vectors=False,
            )
            for pt in points:
                if not pt.payload:
                    continue
                key = (pt.payload.get("folder", ""), pt.payload.get("filename", ""))
                h = pt.payload.get("content_hash") or ""
                seen[key] = h if seen.get(key, h) == h else ""
            if offset is None:
                break
    except Exception as e:
        log.debug("semindex: scan indexed files failed: %s", e)
        return None
    return seen


def _backfill_sync() -> int:
    """Bring the index in line with the vault: index any note that is missing OR whose
    content no longer matches what was indexed.

    Presence alone is what let the index drift. A note first indexed while small stayed
    indexed at that size forever as it grew through any writer that skipped
    `vault_write` -- measured: evolution.md 1 chunk / 6.6% of the file, mood-patterns.md
    29%. The same presence-only check is why an earlier audit reported "0 stale notes":
    it could only ask whether indexed text still existed in the file, which a stale
    prefix always does. Comparing a content hash repairs drift from any source --
    raw writes, Obsidian edits, a failed or cancelled index job -- on the next pass.

    Orphans (indexed notes with no file) are deliberately left alone: the memory canary
    indexes `self/canary.md` without ever writing it to disk, and removing it here could
    race the canary's own read-back into a false memory-failure alarm.
    """
    from pathlib import Path
    client = _qdrant()
    if client is None:
        return 0
    if not _ensure_collection(client):
        return 0

    indexed = _indexed_vault_hashes_sync(client)
    if indexed is None:
        return 0                     # could not see the index -- try again next pass
    root = _VAULT_ROOT
    if not root.exists():
        return 0

    # Folders worth indexing for retrieval. drafts/ and shared/ joined in G1b (D8): vault_write
    # indexes them on write, so leaving them out of reconcile only meant an edited draft stayed
    # indexed as first written and a deleted one stayed in recall. goals/ is deliberately absent:
    # a goal's folder is a generated mirror and only a done result is indexed (goal_files).
    folders = ("topics", "letters", "people", "conversations/significant", "self", "creative",
               "attachments", "drafts", "shared")
    # Orphans are removed only here: these folders hold job output, never an index-only
    # sentinel (the canary's lives in self/).
    orphan_folders = ("drafts", "shared")
    count = 0
    for folder in folders:
        fdir = root / folder
        if not fdir.exists():
            continue
        try:
            # A leading underscore marks a scaffold, not a note: people/_template.md
            # is full of generic questions that would otherwise surface in recall.
            files = [p for p in fdir.iterdir()
                     if p.is_file() and p.suffix == ".md" and not p.name.startswith("_")]
        except Exception:
            continue
        for fpath in files:
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            if indexed.get((folder, fpath.name)) == _content_hash(content):
                continue             # indexed, and indexed from exactly this content
            count += _index_file_sync(folder, fpath.name, content)
    for folder in orphan_folders:
        for (f, filename) in list(indexed):
            if f == folder and not (root / folder / filename).exists():
                _delete_file_points_sync(client, folder, filename)
                log.info("semindex: removed %s/%s from the index (file deleted)", folder, filename)
    if count:
        log.info("semindex: reconcile re-indexed %d chunks across the vault", count)
    return count


def _indexed_skill_hashes_sync(client) -> dict[str, str] | None:
    """Return indexed skill hashes, or None when the scan could not complete.

    A missing or conflicting hash marks a skill for repair. A failed scan must
    not trigger a full reindex during a transient Qdrant error.
    """
    seen: dict[str, str] = {}
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        flt = Filter(must=[FieldCondition(key="kind", match=MatchValue(value="skill"))])
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=_COLLECTION,
                scroll_filter=flt,
                limit=256,
                offset=offset,
                with_payload=["skill_name", "content_hash"],
                with_vectors=False,
            )
            for p in points:
                if p.payload and p.payload.get("skill_name"):
                    name = p.payload["skill_name"]
                    content_hash = p.payload.get("content_hash") or ""
                    seen[name] = (content_hash if seen.get(name, content_hash) == content_hash
                                  else "")
            if offset is None:
                break
    except Exception as e:
        log.debug("semindex: scan indexed skills failed: %s", e)
        return None
    return seen


def _backfill_skills_sync() -> int:
    """Reconcile skills with their files, including edits outside vault_write."""
    client = _qdrant()
    if client is None:
        return 0
    if not _ensure_collection(client):
        return 0

    indexed = _indexed_skill_hashes_sync(client)
    if indexed is None:
        return 0
    skills_dir = _VAULT_ROOT / "skills"
    if not skills_dir.exists():
        return 0
    try:
        files = [p for p in skills_dir.iterdir() if p.is_file() and p.suffix == ".md"]
    except Exception:
        return 0

    count = 0
    names_on_disk = {p.stem for p in files}
    for fpath in files:
        name = fpath.stem
        try:
            content = fpath.read_text(encoding="utf-8")
        except Exception:
            continue
        if indexed.get(name) == _content_hash(content):
            continue
        count += _index_skill_sync(name, content)
    # Unlike vault passages, skills have no index-only canary. A deleted skill
    # must stop being offered as a live procedure. Retire only after both the
    # Qdrant scan and directory listing completed successfully.
    removed = sum(_delete_skill_points_sync(client, name)
                  for name in indexed.keys() - names_on_disk)
    if count:
        log.info("semindex: reconciled %d skill chunks", count)
    if removed:
        log.info("semindex: retired %d deleted skills", removed)
    return count


async def backfill_skills() -> int:
    """Index any not-yet-indexed skill files. Safe to run as a background task."""
    return await embeddings.run_derived(_backfill_skills_sync)


# How often the reconcile pass re-checks the vault against the index. A pass over an
# unchanged vault is one payload scroll and ~130 file hashes -- no embedding.
_RECONCILE_INTERVAL_S = 1800.0


async def run_reconcile_loop() -> None:
    """Re-run the hash-aware backfill periodically. The always-on deployment can go
    weeks between restarts, and startup was the only moment drift was ever repaired."""
    from backend.brain import pacing

    async def _cycle():
        await backfill()

    await pacing.run_periodic(name="semindex_reconcile",
                              check_interval_s=_RECONCILE_INTERVAL_S, on_cycle=_cycle)


async def backfill() -> int:
    """Index any not-yet-indexed vault files and skills. Safe to run as a background task."""
    count = await embeddings.run_derived(_backfill_sync)
    count += await backfill_skills()
    return count


# ---------------------------------------------------------------------------
# Growth visibility -- Step 2 (2026-09-08): retention is deliberately
# unbounded (nothing is pruned, matching the vault's own "files accumulate"
# philosophy, and the storage is genuinely small) -- but unbounded AND
# unmeasured is the bad combination. This surfaces the point count on
# [Runtime status] (see runtime_status.py) so growth is visible long before
# it becomes a problem, rather than discovered later as a surprise.
# ---------------------------------------------------------------------------

def _point_count_sync() -> int | None:
    """Total point count across the whole jarvis_semantic collection (every
    kind combined), or None if Qdrant is unreachable / the collection
    doesn't exist yet -- distinguishable from a genuine 0 so the caller can
    say "unknown" rather than falsely claiming an empty index."""
    client = _qdrant()
    if client is None:
        return None
    try:
        names = {c.name for c in client.get_collections().collections}
        if _COLLECTION not in names:
            return 0
        return client.count(collection_name=_COLLECTION, exact=False).count
    except Exception as e:
        log.debug("semindex: point count failed: %s", e)
        return None


async def point_count() -> int | None:
    """Total point count across jarvis_semantic. None on failure, distinct from 0."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _point_count_sync)


# runtime_status.py's [Runtime status] block is documented and relied upon as
# "fully synchronous, no network" -- it's called directly (not awaited) from
# personality.py's prompt assembly on the live conversational path, AND from
# discord_bridge.py's /status handler, which also calls it synchronously. A
# real Qdrant round-trip inline there would block the asyncio event loop for
# its duration -- exactly the hazard run_in_executor exists everywhere else
# in this codebase to avoid. So the point count runtime_status.py reads is a
# plain in-memory cache: seeded by ONE real count() at startup
# (warm_point_count(), called from main.py's lifespan the same way
# warm_mem()/backfill() already are) and kept approximately current by
# incrementing it in-memory as exchanges are indexed (the dominant, genuinely
# unbounded growth source this feature exists to make visible -- vault/skill
# re-indexing is bounded by file count and deletes-then-inserts, so it isn't
# worth the bookkeeping to track precisely; a restart re-syncs to ground
# truth regardless). This trades perfect precision for "visible long before
# it becomes a problem," which is exactly what was asked for.
_cached_point_count: int | None = None


async def warm_point_count() -> None:
    """Seed the in-memory point-count cache with one real query. Call once at
    startup, fire-and-forget (mirrors warm_mem()/backfill())."""
    global _cached_point_count
    _cached_point_count = await point_count()


def cached_point_count() -> int | None:
    """Synchronous, no I/O -- the value runtime_status.py reads. None until
    warm_point_count() has run at least once, or if it failed."""
    return _cached_point_count


def _note_points_indexed(n: int) -> None:
    """Bump the cache by `n` newly-upserted points. No-ops until the cache
    has been seeded at least once -- an increment onto an unknown baseline
    would just be a second kind of wrong."""
    global _cached_point_count
    if _cached_point_count is not None and n:
        _cached_point_count += n
