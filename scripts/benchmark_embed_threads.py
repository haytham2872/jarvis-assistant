"""Read-only CPU-thread sweep for the shared local embedder.

Run ``.venv\\Scripts\\python.exe -m scripts.benchmark_embed_threads`` from the repo root.
Each setting measures the same short query and a 16-text indexing batch. The
script changes PyTorch's thread count only in its own process.
"""
import json
import math
import time
import asyncio
import threading

import torch

from backend.brain import embeddings


def measure(texts, repetitions=20):
    values = []
    for _ in range(repetitions):
        start = time.perf_counter()
        embeddings._embed_many_sync(texts)
        values.append((time.perf_counter() - start) * 1000)
    ordered = sorted(values)
    return {
        "p50_ms": round(ordered[(len(ordered) - 1) // 2], 1),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 1),
    }


async def measure_under_derived_load(query, batch, repetitions=20):
    """One continuous derived worker, matching the existing A08 concurrency cap."""
    async def foreground():
        values = []
        for _ in range(repetitions):
            start = time.perf_counter()
            await embeddings.embed_many(query)
            values.append((time.perf_counter() - start) * 1000)
        ordered = sorted(values)
        return {
            "p50_ms": round(ordered[(len(ordered) - 1) // 2], 1),
            "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 1),
        }

    idle = await foreground()
    stop = threading.Event()
    started = threading.Event()

    def derived_worker():
        started.set()
        while not stop.is_set():
            embeddings._embed_many_sync(batch, background=True)

    job = asyncio.create_task(embeddings.run_derived(derived_worker))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    try:
        loaded = await foreground()
    finally:
        stop.set()
        await job
    return {"idle": idle, "with_one_derived_worker": loaded}


def main():
    embeddings._get_model()
    original = torch.get_num_threads()
    query = ["Summarize the project architecture and recent conversation."]
    batch = [f"Background index passage {i}: a short note about memory and tools."
             for i in range(16)]
    results = {}
    try:
        for count in dict.fromkeys([original, 1, 2, 4, 8]):
            torch.set_num_threads(count)
            embeddings._embed_many_sync(query)
            embeddings._embed_many_sync(batch)
            results[str(count)] = {"query": measure(query), "batch16": measure(batch)}
    finally:
        torch.set_num_threads(original)
    load = asyncio.run(measure_under_derived_load(query, batch))
    print(json.dumps({"original_threads": original, "results": results,
                      "derived_load": load}, indent=2))


if __name__ == "__main__":
    main()
