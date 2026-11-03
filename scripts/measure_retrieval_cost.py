"""Measure what each per-turn context source actually costs, warm, against real stores.

Timeout values must come from measurement, not intuition -- this codebase has been
burned twice by guessed thresholds (the Qdrant 20s/40s healthcheck saga, and the 0.30
semantic floor that "filtered nothing").
"""
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

from backend.brain import intent, memory, personality, predictions, threads
from backend.brain.embeddings import warm_embedder
from backend.tools import vault

QUERY = "what did we decide about the stock positions?"
ROUNDS = 3


async def timed(label, factory):
    """factory() must return a fresh awaitable each call."""
    times = []
    size = 0
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        try:
            out = await factory()
            size = len(out) if isinstance(out, str) else size
        except Exception as exc:
            print("  %-26s ERROR %s" % (label, str(exc)[:60]))
            return
        times.append(time.perf_counter() - t0)
    print("  %-26s p50=%6.3fs  max=%6.3fs  (%d chars)" % (
        label, statistics.median(times), max(times), size))


async def main():
    print("warming embedder + mem0 (cold-start cost is excluded from the numbers)…")
    t0 = time.time()
    warm_embedder()
    await asyncio.get_event_loop().run_in_executor(None, memory._get_mem)
    await intent.classify("warmup")
    print("warm-up took %.1fs\n" % (time.time() - t0))

    ir = await intent.classify(QUERY)
    qv = ir.query_vec if ir else None

    print("per-source, warm, %d rounds each:" % ROUNDS)
    await timed("recall (mem0 hybrid)",
                lambda: memory.recall(QUERY, True, query_vec=qv, session_id=None))
    await timed("read_owner", lambda: vault.read_owner())
    await timed("read_queue", lambda: vault.read_queue())
    await timed("read_latest_letter", lambda: vault.read_latest_letter())
    await timed("read_relevant_topic", lambda: vault.read_relevant_topic(QUERY, query_vec=qv))
    await timed("predictions.pending", lambda: predictions.read_pending_block())
    await timed("assign_session", lambda: threads.assign_session(time.time(), "local"))
    await timed("intent.classify", lambda: intent.classify(QUERY))


asyncio.run(main())
