"""Shared local text embedder for JARVIS's memory layers.

One lazily-loaded `sentence-transformers` model — **thenlper/gte-small** (384-dim,
no query prefix required, materially stronger than all-MiniLM-L6-v2 on MTEB
retrieval, fully local). Reused by:
  - the semantic index (vault passages + significant exchanges)  — semindex.py
  - the fact dedup pass                                          — memory.py

mem0 shares this instance too: memory.py installs a thin adapter
(_install_shared_embedder) before Memory.from_config(), so only one copy of
the model is ever resident.

Silent degradation: if sentence-transformers / the model weights are missing,
the model load fails once, is cached as None, and every embed call returns an
empty list. Callers treat that as "no embedding available" and degrade.
"""

import logging
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

EMBED_MODEL = "thenlper/gte-small"
EMBED_DIM   = 384

_model = None
_load_lock = threading.Lock()
_load_attempted = False


def _get_model():
    """Load the sentence-transformers model once. Returns None on failure."""
    global _model, _load_attempted
    if _load_attempted:
        return _model
    with _load_lock:
        if _load_attempted:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            # transformers >= 5 loads the checkpoint dtype by default; gte-small
            # ships fp16 weights, which on CPU is slower and breaks with
            # "mat1 and mat2 must have the same dtype, Half and Float" on some
            # encode paths. Force float32 explicitly.
            _model = SentenceTransformer(EMBED_MODEL, model_kwargs={"dtype": "float32"})
            log.info("Embedder loaded: %s (%d-dim, float32)", EMBED_MODEL, EMBED_DIM)
        except Exception as e:
            log.warning("Embedder load failed: %s -- semantic index disabled", e)
            _model = None
        # Set the flag LAST: the unlocked fast path above must never observe
        # _load_attempted=True while _model is still unassigned, or concurrent
        # callers during the ~10s load silently get None (and their searches
        # return empty). With the flag set after assignment, they block on the
        # lock and receive the loaded model instead.
        _load_attempted = True
    return _model


# A08: derived work must not starve the live turn.
#
# Measured against the real embedder: the per-turn foreground embed (which every turn
# pays via `intent.classify`) costs ~35-54ms idle, but **873ms p50 with three
# unbounded background embeds running** -- a 16x tax paid by the NEXT turn for the
# previous turn's indexing. Capping background embeds at one at a time brings that to
# 170ms (3.1x). One, not zero: indexing still has to finish.
#
# **How the cap is enforced matters as much as the cap.** The first version used a
# threading.Semaphore acquired *inside* the shared default executor, so every queued
# job sat blocked while holding one of that executor's threads -- the same threads
# recall, search, intent classification and thread writes run on. It turned CPU
# contention into thread-pool starvation. Measured on a 6-worker pool (a 2-vCPU VM's
# default): a foreground call needing no CPU at all waited 445ms behind 6 queued
# embeds and 2,449ms behind 10. Consolidation alone writes ~7 notes at session end.
#
# Derived work now runs on its own single-worker executor. Same one-at-a-time bound,
# but the queue lives in that executor's work queue instead of in blocked shared
# threads: the same measurement gives 2ms at every backlog size.
_BACKGROUND_EMBED_CONCURRENCY = 1
_DERIVED_THREAD_PREFIX = "jarvis-derived"
_derived_executor = ThreadPoolExecutor(
    max_workers=_BACKGROUND_EMBED_CONCURRENCY, thread_name_prefix=_DERIVED_THREAD_PREFIX)

# Bounded is only half of it -- the roadmap asks for overload to be *observable*, so
# jobs submitted but not yet started are counted and surfaced in [Runtime status].
_background_waiting = 0
_background_waiting_lock = threading.Lock()
_misrouted_warned = False


def background_pressure() -> tuple[int, int]:
    """(derived jobs waiting to start, concurrency limit). Sync, no I/O -- safe to call
    from `runtime_status_text`, which must never block the event loop."""
    return _background_waiting, _BACKGROUND_EMBED_CONCURRENCY


def _adjust_waiting(delta: int) -> None:
    global _background_waiting
    with _background_waiting_lock:
        _background_waiting += delta


def on_derived_thread() -> bool:
    return threading.current_thread().name.startswith(_DERIVED_THREAD_PREFIX)


async def run_derived(fn, *args):
    """Run derived work (indexing, dedup, backfill) on the dedicated executor.

    Every async entry point to background-embedding work goes through here; a guard in
    `test_latency_budgets.py` fails if one goes back to the shared executor.
    """
    _adjust_waiting(+1)
    pending = True
    pending_lock = threading.Lock()

    def _leave_queue():
        nonlocal pending
        # Cancellation can finish the coroutine while a worker has already
        # claimed the future but has not entered _run yet. Count that race once.
        with pending_lock:
            if pending:
                pending = False
                _adjust_waiting(-1)

    def _run():
        _leave_queue()
        return fn(*args)

    try:
        return await asyncio.get_running_loop().run_in_executor(_derived_executor, _run)
    finally:
        _leave_queue()                 # cancelled or rejected before it ever ran


def shutdown_derived() -> None:
    """Drop queued derived work at shutdown rather than holding the process open.
    Indexing is recoverable -- the next startup's reconcile pass redoes anything lost."""
    _derived_executor.shutdown(wait=False, cancel_futures=True)


def _embed_many_sync(texts: list[str], background: bool = False) -> list[list[float]]:
    """Embed texts. `background=True` marks derived work (indexing, dedup).

    The bound is enforced by *where* derived work runs (`run_derived`), not by a lock
    here -- a lock here is what starved the shared pool. The marker is kept so a
    misrouted caller is noticed instead of silently competing with live retrieval.
    """
    global _misrouted_warned
    if background and not on_derived_thread() and not _misrouted_warned:
        _misrouted_warned = True
        log.warning("embeddings: background embed ran outside the derived executor "
                    "-- route its caller through embeddings.run_derived()")
    model = _get_model()
    if model is None or not texts:
        return []
    try:
        vecs = model.encode(
            texts,
            normalize_embeddings=True,   # cosine == dot once normalised
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]
    except Exception as e:
        log.warning("Embedding failed: %s", e)
        return []


async def embed_many(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns [] on any failure."""
    if not texts:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _embed_many_sync, texts)


def warm_embedder() -> None:
    """Load the model in a background daemon thread at startup (mirror warm_mem)."""
    threading.Thread(target=_get_model, daemon=True, name="embedder-warmup").start()
