from backend import owner as _owner
import os
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass
from threading import Lock
from typing import Optional
from backend.storage import fact_corrections as _fact_corrections


from backend.brain.sleep import latest_sleep
from backend.brain.embeddings import EMBED_MODEL as _EMBED_MODEL, EMBED_DIM as _EMBED_DIM
from backend.tasks import spawn


def _qdrant_host() -> str:
    # mem0 builds its own client from this config, so it needs the same `localhost` ->
    # 127.0.0.1 pinning as everything else -- see backend/storage/qdrant.py.
    from backend.storage import qdrant as _qdrant_store
    return _qdrant_store.host()

log = logging.getLogger(__name__)

# mem0 sends usage events to its own analytics service (PostHog) unless told not to, read
# once when mem0.memory.telemetry is first imported. Every mem0 import in this project comes
# after this module, so this is the one place to turn it off. MEM0_TELEMETRY=True opts in.
os.environ.setdefault("MEM0_TELEMETRY", "False")

USER_ID           = os.getenv("JARVIS_USER_NAME", "user")

# Single shared budget for all injected memory (vault passage + exchange + sleep),
# filled in relevance order. ~700 tokens — strong recall without bloat.
_MEMORY_BUDGET_CHARS = 2800

# Embedder-identity sentinel: mixing embedders corrupts vector similarity, so on
# an embedder swap the stale mem0 collection must be dropped and rebuilt.
_EMBEDDER_SENTINEL = Path(__file__).parent.parent.parent / ".embedder"

_mem         = None
_mem_lock    = Lock()
_initialized = False

_MEM0_CONFIG = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host":                  _qdrant_host(),
            "port":                  6333,
            "embedding_model_dims":  _EMBED_DIM,  # must match embedder; default is 1536 (OpenAI)
        },
    },
    # Filled by _resolve_llm_config() at init -- see it for why this is not static.
    "llm": None,
    "embedder": {
        "provider": "huggingface",
        "config": {
            "model": _EMBED_MODEL,   # shared embedder identity (thenlper/gte-small, 384-dim)
            # Only used if the shared-instance install fails and mem0 loads its
            # own copy: force float32 (transformers >= 5 defaults to checkpoint
            # dtype — fp16 — which breaks encode on CPU).
            "model_kwargs": {"model_kwargs": {"dtype": "float32"}},
        },
    },
}


def _recorded_embedder() -> str:
    """The embedder identity the stored vectors were built with. "" if unknown."""
    try:
        if _EMBEDDER_SENTINEL.exists():
            return _EMBEDDER_SENTINEL.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _record_embedder_identity() -> None:
    """Record the active embedder as the one the stored vectors were built with.

    A05: called ONLY when that is actually true — a fresh/empty collection, or one
    deliberately rebuilt. See `_embedder_changed` for why this is separate.
    """
    try:
        _EMBEDDER_SENTINEL.write_text(_EMBED_MODEL, encoding="utf-8")
    except Exception as e:
        log.debug("Could not write embedder sentinel: %s", e)


def _embedder_changed() -> bool:
    """True if the active embedder differs from the one the mem0 vectors were built with.

    **Pure detection — it does not write the sentinel.** A05 found that it used to:
    the mismatch branch wrote the new identity as a side effect of *noticing* the
    change, so the incompatibility was reported once and then vanished. Rehearsed
    against a simulated embedder swap: startup 1 correctly flagged 1,128 points as
    incompatible and refused to delete them (A00 working); **startup 2 reported
    nothing at all**, while the collection still held vectors built by the old model.
    Old facts survived, exactly as A00 intends — and then became silently unsearchable,
    because queries embedded with the new model score meaninglessly against them.

    Conflating "detect" with "record" is what caused that. Recording now happens only
    when the collection genuinely matches the active embedder (see
    `_record_embedder_identity`), so the warning persists across restarts until a real
    migration happens.

    A first run with no sentinel is not a change — there is nothing to be incompatible
    with.
    """
    previous = _recorded_embedder()
    return bool(previous) and previous != _EMBED_MODEL


# A00 (2026-09-21) -- destructive collection migration is opt-in, never automatic.
# Set to "1" only for a deliberate, backed-up migration. See _drop_stale_collections().
_ALLOW_COLLECTION_DELETION = os.getenv("JARVIS_ALLOW_COLLECTION_DELETION", "") == "1"

# Populated by _drop_stale_collections() when a collection is incompatible but was NOT
# deleted. Read by runtime_status so "memory health" cannot look fine while fact recall
# is silently degraded. List of (collection, reason, points).
_INCOMPATIBLE_COLLECTIONS: list[tuple[str, str, int]] = []


def incompatible_collections() -> list[tuple[str, str, int]]:
    """Collections found incompatible and deliberately left in place. Read-only."""
    return list(_INCOMPATIBLE_COLLECTIONS)


def _drop_stale_collections() -> None:
    """Check mem0 Qdrant collections for compatibility. Delete ONLY when it is safe.

    A collection is incompatible when the embedder identity changed (mixing embedders
    corrupts similarity), the vector dimension is wrong, or fastembed is available but
    the collection has no sparse vectors (missing BM25 index).

    **This used to call `client.delete_collection()` on any of those conditions, at
    startup, with no confirmation and no backup** -- silently destroying every extracted
    fact for the user. The docstring justified it as "facts re-accumulate from new turns;
    the vault retains long-form context, so no user-visible loss", but that is only true
    for facts a later session happens to re-derive; anything else was gone. A00 of the
    architecture roadmap calls this out directly: "No startup deletion of a fact
    collection because the embedder, dimension or sparse configuration differs. Report
    incompatibility and require a deliberate versioned migration."

    The rule now:
      - **Empty collection (0 points): still dropped automatically.** There is no data to
        lose, and a fresh install can legitimately create a wrong-dimension collection
        that must be recreated before first use. Blocking this would break setup for no
        benefit.
      - **Non-empty collection: never dropped.** Logged as an error naming the collection,
        the reason and how many points are at stake, and recorded in
        `incompatible_collections()` so status reporting can surface the degradation
        rather than letting recall quietly return nothing. Recall against an incompatible
        collection degrades exactly as it always did on failure -- it does not crash.
      - `JARVIS_ALLOW_COLLECTION_DELETION=1` restores the old destructive behavior for a
        deliberate, backed-up migration, and says so loudly in the log when it fires.
    """
    _INCOMPATIBLE_COLLECTIONS.clear()
    try:
        import importlib.util
        from backend.storage import qdrant as _qdrant_store
        client       = _qdrant_store.get_client(timeout=3)
        fastembed_ok = importlib.util.find_spec("fastembed") is not None
        embedder_changed = _embedder_changed()

        all_cols  = client.get_collections().collections
        mem0_cols = [c.name for c in all_cols if c.name.startswith("mem0")]

        for col in mem0_cols:
            try:
                info    = client.get_collection(col)
                vectors = info.config.params.vectors
                size    = getattr(vectors, "size", None)
                sparse  = getattr(info.config.params, "sparse_vectors", None)

                wrong_dim    = size is not None and size != _EMBED_DIM
                missing_bm25 = fastembed_ok and not sparse

                if not (embedder_changed or wrong_dim or missing_bm25):
                    continue

                reason = (
                    "embedder changed" if embedder_changed
                    else f"wrong dim {size}" if wrong_dim
                    else "no BM25 index"
                )
                points = _collection_point_count(client, col)

                if points == 0:
                    client.delete_collection(col)
                    log.info("Dropped EMPTY collection '%s' (%s) -- will recreate", col, reason)
                    continue

                if _ALLOW_COLLECTION_DELETION:
                    log.warning(
                        "JARVIS_ALLOW_COLLECTION_DELETION=1 -- DELETING collection '%s' "
                        "(%s) with %d stored points. This is irreversible.",
                        col, reason, points,
                    )
                    client.delete_collection(col)
                    continue

                _INCOMPATIBLE_COLLECTIONS.append((col, reason, points))
                log.error(
                    "Collection '%s' is incompatible (%s) and holds %d points -- NOT "
                    "deleting. Fact recall from it will be degraded until this is "
                    "migrated deliberately. Back up first (python -m backend.tools.backup), "
                    "then re-run with JARVIS_ALLOW_COLLECTION_DELETION=1 to rebuild it.",
                    col, reason, points,
                )
            except Exception:
                pass

        # A05: record the active embedder as the one the stored vectors were built
        # with ONLY when that is now true -- i.e. nothing was left flagged as
        # embedder-incompatible. If a collection is still holding old-embedder
        # vectors, the sentinel must keep naming the OLD model so the warning fires
        # again on every startup until the migration is actually performed. Writing
        # it merely because the mismatch was noticed is what made the warning vanish
        # after one restart.
        if not any(reason == "embedder changed" for _c, reason, _p in _INCOMPATIBLE_COLLECTIONS):
            if _recorded_embedder() != _EMBED_MODEL:
                log.info("Embedder identity recorded: %s", _EMBED_MODEL)
            _record_embedder_identity()
    except Exception as e:
        log.debug("Stale-collection check skipped: %s", e)


def _collection_point_count(client, collection: str) -> int:
    """Points in a collection; -1 when it cannot be determined.

    -1 is deliberately NOT 0: an unknown count must never be mistaken for "empty and
    therefore safe to delete", which is exactly the direction this guard exists to
    prevent.
    """
    try:
        return int(client.count(collection_name=collection, exact=True).count)
    except Exception:
        pass
    try:
        raw = client.get_collection(collection).points_count
        # `or 0` here would turn None (unknown) into 0 (empty) and hand the caller a
        # licence to delete -- the precise confusion this function exists to avoid.
        return -1 if raw is None else int(raw)
    except Exception:
        return -1


def _resolve_llm_config() -> dict:
    """The LLM mem0 uses for fact extraction: the provider's standard tier, through the
    project's own adapter (providers/mem0_llm.py), which turns reasoning off, pools the
    client and records usage. Extraction is a high-volume background workload, so it runs
    on the cheap tier like research and consolidation.
    """
    from backend.brain.router import provider as _provider
    from backend.providers import mem0_llm

    provider = _provider()
    if not mem0_llm.register():
        log.error("mem0: the extraction adapter could not be registered -- facts will not "
                  "be extracted")
    log.info("mem0: fact extraction via %s (%s)", provider.name, provider.standard)
    return {
        "provider": mem0_llm.PROVIDER_NAME,
        "config": {"model": provider.standard, "api_key": provider.api_key},
    }


def _install_shared_embedder() -> None:
    """Make mem0 reuse the shared gte-small instance from embeddings.py.

    Without this, mem0 loads a second copy of the same model (~120 MB extra,
    a duplicate HF download burst at startup) — and with transformers >= 5 the
    duplicate loads as fp16, which breaks encode on CPU ("Half and Float").
    mem0's factory resolves the class via getattr at creation time, so
    replacing it on the module before Memory.from_config() is sufficient.
    """
    from backend.brain import embeddings as _emb
    from mem0.embeddings import huggingface as _hf_mod
    from mem0.embeddings.base import EmbeddingBase

    class _SharedEmbedding(EmbeddingBase):
        def __init__(self, config=None):
            super().__init__(config)
            self.config.model = _EMBED_MODEL
            self.config.embedding_dims = _EMBED_DIM
            self.model = _emb._get_model()
            if self.model is None:
                raise RuntimeError("shared embedder unavailable")

        def embed(self, text, memory_action=None):
            return self.model.encode(text, convert_to_numpy=True).tolist()

    _hf_mod.HuggingFaceEmbedding = _SharedEmbedding


def _get_mem():
    global _mem, _initialized
    if _initialized:
        return _mem
    with _mem_lock:
        if _initialized:
            return _mem
        try:
            # Load the shared embedder first, while HF_HUB_OFFLINE is still in
            # effect — avoids the network HEAD-request burst, and serializes
            # the two largest startup allocations instead of racing them.
            from backend.brain import embeddings as _emb
            _emb._get_model()
            try:
                _install_shared_embedder()
            except Exception as e:
                log.warning("shared embedder install failed (%s) — mem0 will load its own", e)
            _drop_stale_collections()
            # huggingface_hub (imported transitively by faster-whisper at startup)
            # caches HF_HUB_OFFLINE as a module-level constant at import time.
            # Popping os.environ after the fact has no effect — we must patch the
            # constant directly so fastembed can download the BM25 model on first
            # run. Both the constant and os.environ are restored in the finally block.
            _hf_env_bak = os.environ.pop("HF_HUB_OFFLINE", None)
            try:
                import huggingface_hub.constants as _hfc
                _hf_const_bak = _hfc.HF_HUB_OFFLINE
                _hfc.HF_HUB_OFFLINE = False
            except Exception:
                _hfc = None
                _hf_const_bak = None
            try:
                from mem0 import Memory
                config = dict(_MEM0_CONFIG)
                config["llm"] = _resolve_llm_config()
                _mem = Memory.from_config(config)
                log.info("Mem0 initialized (qdrant=%s:6333, user=%s)",
                         _qdrant_host(), USER_ID)
                # spaCy and BM25 encoder load lazily on the first search() call,
                # not during Memory.from_config(). Do a dummy search now so they're
                # hot before the first user request instead of adding 4s to it.
                try:
                    # top_k, not limit -- mem0 2.0.1 renamed the param and
                    # swallows unknown kwargs silently (**kwargs), so limit=
                    # was being ignored and every search returned top_k=20.
                    _mem.search("warmup", filters={"user_id": USER_ID}, top_k=1)
                    log.info("Mem0 warmup complete (spaCy + BM25 ready)")
                except Exception as e:
                    log.debug("Mem0 warmup search skipped: %s", e)
            finally:
                if _hf_env_bak is not None:
                    os.environ["HF_HUB_OFFLINE"] = _hf_env_bak
                if _hfc is not None:
                    _hfc.HF_HUB_OFFLINE = _hf_const_bak
        except Exception as e:
            log.warning("Mem0 init failed: %s -- memory disabled", e)
            _mem = None
        # Set LAST (same pattern as embeddings._get_model): the unlocked fast
        # path must never see _initialized=True while _mem is still unassigned,
        # or concurrent callers during the ~15s init silently get None.
        _initialized = True
    return _mem


def warm_mem() -> None:
    """Trigger Mem0 init in a background thread at startup so the first request is instant."""
    import threading
    threading.Thread(target=_get_mem, daemon=True, name="mem0-warmup").start()


@dataclass
class RecallResult:
    """Recall delivered to personality.py each turn.

    summary and letter are kept for signature compatibility but are always ""
    after checkpoints.py was replaced by consolidation.py — the vault letter
    and the owner portrait now carry that context.
    """
    facts:         str = ""   # unused since 2026-10-11 (passive fact recall removed)
    summary:       str = ""   # unused (kept for compat); was checkpoint summary
    letter:        str = ""   # unused (kept for compat); was checkpoint letter
    sleep_info:    str = ""   # last sleep marker formatted for system prompt
    vault_passage: str = ""   # best semantic vault passage for this query
    exchange:      str = ""   # best semantic past exchange for this query


# mem0 creates bookkeeping collections alongside the fact store, and they also start
# with "mem0". `mem0migrations` is migration state; `mem0_entities` is the entity graph.
# Neither holds facts.
_MEM0_FACTS_COLLECTION = "mem0"          # mem0's default; _MEM0_CONFIG sets no override
_MEM0_INTERNAL_COLLECTIONS = frozenset({"mem0migrations", "mem0_entities"})


def _find_mem0_collection() -> Optional[str]:
    """Return the name of the mem0 FACT collection, or None.

    Hardening (2026-09-22): this used to return the first collection whose name merely
    started with "mem0", which silently became wrong when a mem0 upgrade introduced
    `mem0migrations`. Qdrant listed it first, so every caller -- consolidated-id
    filtering, fact scrolling, dedup, and the consolidated marking itself -- was
    operating on an EMPTY bookkeeping collection instead of the 1,128-point fact store.

    Measured live at the time of the fix: `_get_consolidated_ids()` returned 0 while
    1,118 facts were genuinely marked `consolidated: true`. Two consequences, both
    silent, both the opposite of what the design intends -- `recall()` could exclude
    nothing, so long-since-consolidated facts stayed eligible for injection forever
    (the vault is supposed to carry that long-form context instead), and consolidation's
    own marking wrote to the wrong collection, so nothing new was ever marked either.

    Exact name first, then a prefix scan that skips the known internals, so an
    unusually-named deployment still resolves rather than returning None.
    """
    try:
        from backend.storage import qdrant as _qdrant_store
        qc = _qdrant_store.get_client(timeout=3)
        names = [col.name for col in qc.get_collections().collections]
        if _MEM0_FACTS_COLLECTION in names:
            return _MEM0_FACTS_COLLECTION
        for name in names:
            if name.startswith("mem0") and name not in _MEM0_INTERNAL_COLLECTIONS:
                return name
    except Exception:
        pass
    return None


# Consolidated-fact IDs, cached so a fact search doesn't pay an extra Qdrant
# round-trip each time. Facts only ever move unconsolidated → consolidated, and
# only at session boundaries, so a TTL plus an explicit invalidation hook from
# consolidation.py keeps this exact in practice.
_CONSOLIDATED_TTL_S = 300.0
_consolidated_ids: set[str] = set()
_consolidated_loaded_at: float = 0.0

# A05: provenance. mem0 2.0.1's additive extractor records `attributed_to` on every
# fact ("user" or "assistant") -- but its own search() does NOT return that field, so
# the value is written and then unreachable on the hot path. Measured on the live
# collection: 622 of 1,128 facts are assistant-attributed, i.e. things JARVIS said
# rather than things the owner told him, and all of them were being injected into
# [Memory] as if the owner had stated them.
#
# The dangerous shape, confirmed live against the real extractor: an assistant
# ASSERTION about the owner is stored unhedged -- "User holds 200 shares of NVDA at an
# average cost basis of $118" -- textually identical to the same fact when the owner
# actually says it. Only `attributed_to` separates them. (Speculation fares better:
# mem0 keeps the hedge, "User is LIKELY anxious about...". And plenty of
# assistant-attributed facts are legitimate world facts from a real web search.)
#
# Same cached-scroll shape as the consolidated set above, for the same reason: a
# per-turn Qdrant round-trip on the recall path is what that cache exists to avoid.
_ATTRIBUTION_TTL_S = 300.0
_assistant_ids: set[str] = set()
_assistant_loaded_at: float = 0.0


def invalidate_attribution_cache() -> None:
    global _assistant_loaded_at
    _assistant_loaded_at = 0.0


def _get_assistant_attributed_ids() -> set:
    """Fact IDs mem0 attributed to the assistant, for this user (cached).

    Degrades to the previously cached set (empty on a cold process) on any failure —
    an unavailable Qdrant must not stop recall, and an unlabelled fact reads exactly
    as it did before this existed.
    """
    global _assistant_ids, _assistant_loaded_at
    import time as _time
    now = _time.monotonic()
    if now - _assistant_loaded_at < _ATTRIBUTION_TTL_S and _assistant_loaded_at > 0:
        return _assistant_ids
    try:
        from backend.storage import qdrant as _qdrant_store
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        col_name = _find_mem0_collection()
        if not col_name:
            return _assistant_ids
        qc = _qdrant_store.get_client(timeout=3)
        flt = Filter(must=[
            FieldCondition(key="user_id",       match=MatchValue(value=USER_ID)),
            FieldCondition(key="attributed_to", match=MatchValue(value="assistant")),
        ])
        ids: set[str] = set()
        offset = None
        while True:
            points, offset = qc.scroll(
                collection_name=col_name, scroll_filter=flt, limit=512,
                offset=offset, with_payload=False, with_vectors=False,
            )
            ids.update(str(p.id) for p in points)
            if offset is None:
                break
        _assistant_ids = ids
        _assistant_loaded_at = now
    except Exception as e:
        log.debug("attribution cache refresh failed: %s", e)
    return _assistant_ids


def invalidate_consolidated_cache() -> None:
    """Force the next recall to re-read consolidated IDs (called after consolidation)."""
    global _consolidated_loaded_at
    _consolidated_loaded_at = 0.0


def _get_consolidated_ids() -> set:
    """Return all fact IDs marked consolidated:true for this user (cached)."""
    global _consolidated_ids, _consolidated_loaded_at
    import time as _time
    now = _time.monotonic()
    if now - _consolidated_loaded_at < _CONSOLIDATED_TTL_S and _consolidated_loaded_at > 0:
        return _consolidated_ids
    try:
        from backend.storage import qdrant as _qdrant_store
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        col_name = _find_mem0_collection()
        if not col_name:
            return _consolidated_ids
        qc = _qdrant_store.get_client(timeout=3)
        flt = Filter(must=[
            FieldCondition(key="user_id",      match=MatchValue(value=USER_ID)),
            FieldCondition(key="consolidated", match=MatchValue(value=True)),
        ])
        ids: set[str] = set()
        offset = None
        while True:
            points, offset = qc.scroll(
                collection_name=col_name,
                scroll_filter=flt,
                limit=512,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.update(str(p.id) for p in points)
            if offset is None:
                break
        _consolidated_ids = ids
        _consolidated_loaded_at = now
    except Exception as e:
        log.debug("consolidated check failed: %s", e)
    return _consolidated_ids


def _search_facts_sync(query: str, limit: int) -> list[dict]:
    """Active mem0 fact search for the memory_search tool.

    Unlike passive recall once did, consolidated facts are INCLUDED and
    labeled — exclusion exists to keep the per-turn prompt injection lean,
    which doesn't apply when JARVIS deliberately digs through his memory.
    mem0's search is already hybrid (BM25 + dense). Returns [] on any failure.
    """
    mem = _get_mem()
    if mem is None:
        return []
    try:
        # top_k, not limit -- see the warmup call's note on mem0 2.0.1
        raw   = mem.search(query, filters={"user_id": USER_ID}, top_k=limit)
        items = raw.get("results", raw) if isinstance(raw, dict) else (raw or [])
        if not items:
            return []
        consolidated  = _get_consolidated_ids()
        assistant_ids = _get_assistant_attributed_ids()   # A05: provenance
        corrections = _fact_corrections.list_for_user(USER_ID)
        out: list[dict] = []
        for item in items:
            text = item.get("memory") or item.get("text") or item.get("content") or ""
            if not text:
                continue
            if _fact_corrections.suppresses(str(text), corrections):
                continue
            out.append({
                "text":         str(text).strip(),
                "score":        float(item["score"]) if item.get("score") is not None else None,
                "consolidated": str(item.get("id", "")) in consolidated,
                # A05: whether JARVIS said this or the owner did. Deliberately a flag
                # rather than a prefix here -- the tool renders its own source label,
                # unlike the passive path which injects raw lines into [Memory].
                "from_assistant": str(item.get("id", "")) in assistant_ids,
                # Absence from assistant_ids is not proof of user attribution. Ledger
                # import candidates need an explicit extractor attribution.
                "attributed_to": item.get("attributed_to") or (item.get("metadata") or {}).get("attributed_to"),
            })
        return out
    except Exception as e:
        log.debug("search_facts failed: %s", e)
        return []


async def search_facts(query: str, top_k: int = 5) -> list[dict]:
    """Hybrid mem0 fact search including the consolidated archive. [] on failure."""
    if not query or not query.strip():
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _search_facts_sync, query, max(top_k * 2, 8))


async def _format_sleep_info() -> str:
    """Fetch the latest sleep marker and format it as a system-prompt block."""
    import time as _time, datetime as _dt
    try:
        marker = await latest_sleep()
        if not marker:
            return ""
        sleep_ts = float(marker.get("timestamp", 0))
        elapsed  = _time.time() - sleep_ts
        if elapsed < 60:
            dur = f"{int(elapsed)} seconds"
        elif elapsed < 3600:
            dur = f"{int(elapsed / 60)} minutes"
        elif elapsed < 86400:
            dur = f"{elapsed / 3600:.1f} hours"
        else:
            dur = f"{elapsed / 86400:.1f} days"
        when = _dt.datetime.fromtimestamp(sleep_ts).strftime("%Y-%m-%d %H:%M")
        # Says whose sleep it is. Live (5 October), four minutes after a restart: "Four minutes
        # since you went offline, so you have not read any of this yet", said to the owner.
        return (
            _owner.say(f"[Last sleep]\n"
            f"You (JARVIS, not «owner») last went offline -- your own restart or shutdown -- at "
            f"{when} ({dur} ago). "
            f"Mood at shutdown: {marker.get('mood_tone', 'unknown')} "
            f"(intensity {float(marker.get('mood_intensity', 0)):.2f}, "
            f"{marker.get('mood_trajectory', 'stable')}). "
            f"Session had {int(marker.get('turn_count', 0))} turns.")
        )
    except Exception as e:
        log.debug("sleep info format failed: %s", e)
        return ""


async def recall(
    query: str, deep: bool = True, query_vec: Optional[list] = None,
    channel: Optional[str] = None, history_since: Optional[float] = None,
) -> RecallResult:
    """Relevance-RANKED retrieval under a single shared memory budget.

    Gathers (concurrently): the best semantic vault passage, up to 3 semantic
    past-exchange candidates, and the last sleep marker. Vault and exchange are
    each capped to their own share first (1400 / 1000-per-candidate), then
    RANKED against each other via semindex.interleave_by_rank(), so whichever
    kind is most relevant THIS TURN fills the shared budget first, not
    whichever kind happens to run first in the code.

    No mem0 facts (removed 2026-10-11). Replaying 40 Discord messages on the
    server, passive fact recall injected nothing in 40 of 40 deep turns: facts
    are marked consolidated when their session closes, so only the open
    session's facts were eligible, and the live history already holds that
    session. It cost a mem0 search per deep turn, which slows sharply beside
    the cross-encoder on a 2-vCPU host. Extraction continues (consolidation reads
    the facts) and memory_search still searches them on request. `facts` stays
    on RecallResult, always "".

    The vault passage and past exchanges are deep-gated (skipped entirely on a
    simple turn). threads.exchange_candidates() returns up to 3
    scored exchanges from earlier conversations, leaving out those the live
    history already holds (`channel` and `history_since`, the time of the oldest
    turn in it); they compete in the SAME interleave_by_rank fusion as the
    vault passage, not a reserved slice. A session-scoped search used to
    run on every turn and returned the live history over again (2026-10-11).

    Sleep info is deliberately NOT part of that ranking — "were you asleep"
    isn't a relevance signal for the query, so it keeps its own fixed
    last-priority slot after the ranked candidates, same as before.

    `query_vec`, when given (chat() passes intent.classify()'s own per-turn
    embed), is reused for the vault/exchange searches instead of re-embedding
    `query` again for each.

    Every source degrades silently to "" (RecallResult fields) on failure.
    """
    from backend.brain import semindex
    from backend.brain.threads import exchange_candidates

    budget = _MEMORY_BUDGET_CHARS if deep else _MEMORY_BUDGET_CHARS // 2
    loop = asyncio.get_event_loop()

    vault_passage, exchanges, sleep_info = await asyncio.gather(
        _semantic_vault_passage(query, query_vec=query_vec) if deep else _async_none(),
        exchange_candidates(query, deep=deep, query_vec=query_vec, channel=channel,
                            exclude_since=history_since),
        _format_sleep_info(),
    )
    corrections = _fact_corrections.list_for_user(USER_ID)
    exchanges = [h for h in exchanges if not _fact_corrections.suppresses(
        h.get("text", ""), corrections)]

    # Build one "kind list" per candidate source that actually produced
    # something, then let interleave_by_rank decide the fill order. Exchange
    # contributes up to 3 items (Step 2) instead of 1 -- top_k 4 (1 vault + up
    # to 3 exchange) so interleave's position-wise merge doesn't cut off
    # exchange's 2nd/3rd candidate before the budget-based fill loop below
    # ever gets to consider them.
    per_kind: list[list[dict]] = []
    if vault_passage:
        per_kind.append([{"_field": "vault_passage", **vault_passage}])
    if exchanges:
        per_kind.append([{"_field": "exchange", **h} for h in exchanges])
    ranked = semindex.interleave_by_rank(per_kind, top_k=4) if per_kind else []

    remaining = budget
    out = {"vault_passage": ""}
    exchange_parts: list[str] = []
    for c in ranked:
        text = c.get("text", "")
        if not text or len(text) > remaining:
            continue
        if c["_field"] == "exchange":
            # Several exchange candidates can each independently win a slot in
            # the ranked fill order -- accumulate rather than overwrite, or a
            # second exchange candidate would silently clobber the first.
            exchange_parts.append(text)
        else:
            out[c["_field"]] = text
        remaining -= len(text)
    out["exchange"] = "\n\n---\n\n".join(exchange_parts)

    out_sleep = ""
    if sleep_info and len(sleep_info) <= remaining:
        out_sleep = sleep_info

    return RecallResult(
        sleep_info=out_sleep,
        vault_passage=out["vault_passage"],
        exchange=out["exchange"],
    )


async def _async_none() -> None:
    return None


async def _semantic_vault_passage(query: str, query_vec: Optional[list] = None) -> Optional[dict]:
    """Best semantic vault passage across all folders (not just topics).

    Returns {"text": ..., "score": top_hit_score} or None on no match/failure.
    `query_vec`, when given, is reused instead of re-embedding `query`.
    """
    try:
        from backend.brain import semindex
        hits = await semindex.search(query, kind="vault", k=2, query_vec=query_vec, rerank=True)
        if not hits:
            return None
        parts: list[str] = []
        total = 0
        corrections = _fact_corrections.list_for_user(USER_ID)
        for h in hits:
            text = (h.get("text") or "").strip()
            if not text or _fact_corrections.suppresses(text, corrections):
                continue
            if total + len(text) > 1400:
                break
            parts.append(text)
            total += len(text)
        if not parts:
            return None
        # hits arrive best-first (Qdrant returns by descending score), so the
        # first hit's score is this kind's single best shot.
        return {"text": "\n\n".join(parts), "score": float(hits[0].get("score", 0.0))}
    except Exception as e:
        log.debug("semantic vault passage failed: %s", e)
        return None


async def _do_ingest(user_text: str, assistant_text: str) -> None:
    mem = _get_mem()
    if mem is None:
        return
    # Discord bot output can contain unverified web material or its own
    # inference about the owner. Only his authored message may create facts.
    from backend.brain import discord_context
    messages = [{"role": "user", "content": user_text}]
    if discord_context.current.get() is None:
        messages.append({"role": "assistant", "content": assistant_text})
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: mem.add(messages, user_id=USER_ID))
        invalidate_attribution_cache()
        log.debug("Mem0 ingest OK")
    except Exception as e:
        log.warning("Mem0 ingest failed: %s", e)


async def ingest(model: str, user_text: str, assistant_text: str) -> None:
    """Fire-and-forget. model is unused -- Mem0 handles extraction internally."""
    spawn(_do_ingest(user_text, assistant_text))


async def correct_memory(claim: str, correction: str,
                         terms: list[str] | None = None) -> str:
    """Supersede a false personal assertion on every read path, without
    destructively editing Mem0 or the user's vault.
    """
    if len(claim.strip()) < 12 or len(correction.strip()) < 8:
        return _owner.say("Error: give the specific false claim and «owner's» correction.")
    cid = _fact_corrections.add(USER_ID, claim, correction, terms)
    if cid is None:
        return "Error: the correction could not be saved."
    return (f"Confirmed correction {cid}: the old claim is suppressed in future "
            "memory retrieval. Existing vault files are not changed.")


# ---------------------------------------------------------------------------
# Fact dedup — called at session end (off the hot path)
# ---------------------------------------------------------------------------

_DEDUP_SIM_THRESHOLD = 0.95


def _scroll_facts_sync() -> list[tuple[str, str]]:
    """Return [(id, text)] for all unconsolidated mem0 facts for this user."""
    try:
        from backend.storage import qdrant as _qdrant_store
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        col = _find_mem0_collection()
        if not col:
            return []
        qc = _qdrant_store.get_client(timeout=5)
        flt = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=USER_ID))])
        out: list[tuple[str, str]] = []
        offset = None
        while True:
            points, offset = qc.scroll(
                collection_name=col,
                scroll_filter=flt,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                if payload.get("consolidated", False):
                    continue
                text = payload.get("data") or payload.get("memory") or payload.get("text") or ""
                if text:
                    out.append((str(p.id), str(text).strip()))
            if offset is None:
                break
        return out
    except Exception as e:
        log.debug("dedup: scroll facts failed: %s", e)
        return []


def _dedup_facts_sync() -> int:
    """Embed unconsolidated facts and delete near-duplicates (cosine > threshold).

    Keeps the first of each duplicate cluster; removes the rest from the mem0
    collection. Returns the number of facts removed. Runs in an executor at
    session end so it never touches a live turn.

    A05: **source-aware**. Two facts with different `attributed_to` are never treated
    as duplicates, however similar their wording. The roadmap asks to "replace
    similarity-only destructive fact cleanup with conservative source-aware
    resolution", and the similarity-only rule -- keep whichever string is LONGER --
    could permanently delete something the owner said in favour of a longer claim JARVIS
    made. Measured on the real collection: of 10 near-duplicate pairs above the
    threshold, 1 was exactly that, and the fact it would have deleted ("has invested
    capital in NVIDIA and is monitoring its recent price decline") was substantively
    more valuable than the transient price report that would have survived.

    They are not redundant: one is what the owner established, the other is what JARVIS
    asserted, and slice 2 exists precisely to keep that distinction legible. Deduping
    only within a source keeps it.
    """
    from backend.brain import embeddings as _emb
    facts = _scroll_facts_sync()
    if len(facts) < 2:
        return 0
    vecs = _emb._embed_many_sync([t for _, t in facts], background=True)
    if not vecs or len(vecs) != len(facts):
        return 0
    try:
        import math
        def _cos(a, b):
            # vectors are normalised by the embedder, so dot == cosine
            return sum(x * y for x, y in zip(a, b))

        # Cached id set (see _get_assistant_attributed_ids) -- no extra scroll.
        assistant_ids = _get_assistant_attributed_ids()

        def _same_source(a_id: str, b_id: str) -> bool:
            return (str(a_id) in assistant_ids) == (str(b_id) in assistant_ids)

        to_remove: set[str] = set()
        for i in range(len(facts)):
            if facts[i][0] in to_remove:
                continue
            for j in range(i + 1, len(facts)):
                if facts[j][0] in to_remove:
                    continue
                # A05: different provenance -> not duplicates, whatever the similarity.
                if not _same_source(facts[i][0], facts[j][0]):
                    continue
                if _cos(vecs[i], vecs[j]) > _DEDUP_SIM_THRESHOLD:
                    # Keep the longer (more specific) fact, remove the other
                    keep_i = len(facts[i][1]) >= len(facts[j][1])
                    to_remove.add(facts[j][0] if keep_i else facts[i][0])
                    if not keep_i:
                        break

        if not to_remove:
            return 0
        from backend.storage import qdrant as _qdrant_store
        from qdrant_client.models import PointIdsList
        col = _find_mem0_collection()
        if not col:
            return 0
        qc = _qdrant_store.get_client(timeout=5)
        qc.delete(collection_name=col, points_selector=PointIdsList(points=list(to_remove)))
        log.info("dedup: removed %d near-duplicate facts", len(to_remove))
        return len(to_remove)
    except Exception as e:
        log.debug("dedup: failed: %s", e)
        return 0


async def dedup_facts() -> int:
    """Merge near-duplicate mem0 facts. Call at session end, off the hot path.

    Derived work, so it runs on the dedicated executor (A08) rather than holding a
    shared-pool thread for the length of an embed pass."""
    from backend.brain import embeddings as _emb
    return await _emb.run_derived(_dedup_facts_sync)
