"""backend/brain/rerank.py — a second, better judge of relevance for passive recall.

Passive recall selects context with gte-small cosine similarity, which barely separates
relevant from irrelevant here. Measured on 60 real past messages replayed against the
real index, with every candidate labelled by a strict model judge:

    median cosine, relevant 0.847 vs irrelevant 0.834
    production floors: precision 0.38, 49/60 turns carried an irrelevant block

A cross-encoder reads the query and each candidate together. Used as a gate over a
wider cosine shortlist (ms-marco-MiniLM-L6-v2, frontmatter stripped, passages cut to
128 tokens, two-tier threshold -- see gate_sync): precision 0.69, 16/60 turns with an
irrelevant block. The price is recall -- turns where some relevant context surfaced
fell from 43/50 to 32/50 -- the right trade for PASSIVE injection: an irrelevant block
derails a reply (the 2026-09-09 incident), while a missed one is one memory_search away.

Also measured and rejected: a single -3 threshold (8/60 irrelevant but 26/50 surfaced,
and it dropped both NVDA notes for "how is my NVDA position doing"), 256 tokens
(2.5x slower, twice the junk at the same threshold), and ms-marco-MiniLM-L12-v2
(twice the latency, no better). Only deep turns
run these paths; the three concurrent passive searches go from ~20ms to ~0.6s on the
desk CPU. Loaded lazily from the local
Hugging Face cache only (HF_HUB_OFFLINE is set at startup). Absent or failing, every
caller falls back to exactly today's cosine behaviour -- the same silent-degradation
contract as everything else.

To enable on a host that lacks the model, download it once:
    python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L6-v2')"
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

MODEL_NAME = os.getenv("JARVIS_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L6-v2")
THRESHOLD = -3.0             # measured, see module docstring
LEAD_THRESHOLD = -5.0        # the best hit, when its cosine also clears the old floor
PREFILTER = 0.72             # loose cosine floor: bottom of gte-small's unrelated band
POOL = 10                    # candidates per call; more costs latency for little gain
_MAX_LENGTH = 128            # tokens per pair; 256 was 2.5x slower and let twice the junk through

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)

_model = None
_load_attempted = False
_load_lock = threading.Lock()

# What scoring cost this turn, for chat()'s timing line. The searches run in executor
# threads, so the counters are guarded; reset at the start of each turn.
_turn_lock = threading.Lock()
_turn = {"calls": 0, "pairs": 0, "seconds": 0.0}


def reset_turn() -> None:
    with _turn_lock:
        _turn.update(calls=0, pairs=0, seconds=0.0)


def turn_stats() -> dict:
    """{"calls", "pairs", "seconds"} scored since the last reset_turn(). The calls run
    concurrently, so seconds is CPU-thread time summed, not wall time."""
    with _turn_lock:
        return dict(_turn)


def enabled() -> bool:
    return os.getenv("JARVIS_RERANK", "1") != "0"


def _get_model():
    """The cross-encoder, or None. Flag set LAST, after the model is assigned -- the
    same ordering embeddings._get_model() uses so a concurrent caller never sees a
    'loaded' flag with no model behind it."""
    global _model, _load_attempted
    if _load_attempted:
        return _model
    with _load_lock:
        if _load_attempted:
            return _model
        try:
            from sentence_transformers import CrossEncoder
            _model = CrossEncoder(MODEL_NAME, device="cpu", max_length=_MAX_LENGTH,
                                  local_files_only=True)
            log.info("rerank: %s loaded", MODEL_NAME)
        except Exception as e:
            _model = None
            log.warning("rerank: %s unavailable (%s) -- passive recall uses cosine only",
                        MODEL_NAME, e)
        _load_attempted = True
    return _model


def warm() -> None:
    """Load in a background thread at startup so the first turn does not pay for it."""
    if enabled():
        threading.Thread(target=_get_model, daemon=True, name="rerank-warmup").start()


def _clean(text: str) -> str:
    return _FRONTMATTER.sub("", text or "").strip()


def gate_sync(query: str, hits: list[dict], *, threshold: float = THRESHOLD,
              lead_threshold: float = LEAD_THRESHOLD,
              lead_cosine: float | None = None) -> list[dict] | None:
    """Score `hits` against `query`; return the relevant ones best first, each carrying
    a `rerank` score. None means the reranker is unavailable and the caller must use its
    cosine path. Blocking -- call from an executor thread.

    Two tiers. Every hit needs `threshold`, except the single best one, which is kept at
    `lead_threshold` when its cosine also clears `lead_cosine` (the caller's old floor).
    MS MARCO scores "does this passage ANSWER the question", so useful background for a
    personal question scores low: "how is my NVDA position doing, should I trim it?"
    against the NVDA technical-setup note is -3.9. When both judges agree the top hit
    is on topic it stays."""
    if not enabled():
        return None
    model = _get_model()
    if model is None:
        return None
    if not hits:
        return []
    started = time.perf_counter()
    try:
        scores = model.predict([(query, _clean(h.get("text", ""))) for h in hits],
                               batch_size=16, show_progress_bar=False)
    except Exception as e:
        log.warning("rerank: scoring failed (%s) -- falling back to cosine", e)
        return None
    finally:
        with _turn_lock:
            _turn["calls"] += 1
            _turn["pairs"] += len(hits)
            _turn["seconds"] += time.perf_counter() - started
    scored = sorted((dict(h, rerank=float(s)) for h, s in zip(hits, scores)),
                    key=lambda h: -h["rerank"])
    kept = []
    for i, h in enumerate(scored):
        lead_ok = (i == 0 and lead_cosine is not None and h["rerank"] >= lead_threshold
                   and h.get("score", 0.0) >= lead_cosine)
        if lead_ok or h["rerank"] >= threshold:
            kept.append(h)
    return kept
