"""Read-only local latency probe for the A08 context path.

Run from the repository root with ``.venv\\Scripts\\python.exe -m scripts.benchmark_a08``.
It prints timings only; it never sends a model request or prints memory contents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

from backend.brain import intent, memory, sleep, threads


def _summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "p50_ms": round(ordered[(len(ordered) - 1) // 2], 1),
        "p95_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 1),
        "max_ms": round(ordered[-1], 1),
    }


async def _measure(label, count, call, output):
    values = []
    for _ in range(count):
        start = time.perf_counter()
        await call()
        values.append((time.perf_counter() - start) * 1000)
    output[label] = _summary(values)


async def main(samples: int, deep_samples: int, compare_sleep: bool) -> None:
    query = "Summarize the project architecture and the most recent conversation."
    output = {}
    # Warm the local model and route matrix outside the measured samples.
    await asyncio.get_running_loop().run_in_executor(None, intent._build_matrix_sync)
    if intent._matrix is None:
        raise RuntimeError("Intent matrix did not build; benchmark would measure a fallback")
    await intent.classify(query)
    await sleep.latest_sleep()
    decision = await threads.assign_session(time.time(), channel="discord")
    await memory.recall(query, deep=False, session_id=decision.session_id)

    if compare_sleep:
        original = sleep._scroll_latest_sync

        def uncached_sleep():
            return sleep._scan_latest_sync()[1]

        sleep._scroll_latest_sync = uncached_sleep
        try:
            await _measure("latest_sleep_uncached", samples, sleep.latest_sleep, output)
            await _measure("assign_session_uncached", samples,
                           lambda: threads.assign_session(time.time(), channel="discord"), output)
            await _measure("recall_simple_uncached", samples,
                           lambda: memory.recall(query, deep=False,
                                                 session_id=decision.session_id), output)
        finally:
            sleep._scroll_latest_sync = original

    await _measure("intent_classify", samples, lambda: intent.classify(query), output)
    await _measure("latest_sleep", samples, sleep.latest_sleep, output)
    await _measure("assign_session", samples,
                   lambda: threads.assign_session(time.time(), channel="discord"), output)
    await _measure("recall_simple", samples,
                   lambda: memory.recall(query, deep=False,
                                         session_id=decision.session_id), output)
    if deep_samples:
        await memory.recall(query, deep=True, session_id=decision.session_id)
        await _measure("recall_deep", deep_samples,
                       lambda: memory.recall(query, deep=True,
                                             session_id=decision.session_id), output)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--deep-samples", type=int, default=6)
    parser.add_argument("--compare-sleep", action="store_true")
    args = parser.parse_args()
    if args.samples < 1 or args.deep_samples < 0:
        parser.error("samples must be positive and deep-samples cannot be negative")
    asyncio.run(main(args.samples, args.deep_samples, args.compare_sleep))
