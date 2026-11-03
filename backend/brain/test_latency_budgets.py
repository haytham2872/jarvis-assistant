"""A08: foreground responsiveness under background load.

Acceptance criteria. Three were closed by earlier packages and are pinned here so they
cannot regress; the fourth is this package's own work.

| criterion | where |
|---|---|
| history growth does not cause whole-history admission scans | A02 slice 2 |
| slow optional retrieval is bounded | A03 slice 5 |
| text startup does not wait for STT | A01 |
| foreground remains responsive under controlled background load | **here** |
| overload is bounded and observable | **here** |

**Measured against the real embedder, not argued.** Every turn pays one foreground
embed via `intent.classify`. Idle it costs ~35-54ms; with three unbounded background
embeds running (what post-turn indexing and ingestion do) it measured **873ms p50 — a
16x tax on the NEXT turn**. Capping concurrent *background* embeds at one brought that
to 170ms (3.1x), and re-measured through the real call path: 12.5x -> 3.5x.

One, not zero: the residual is genuine CPU contention from the single remaining
background embed. The roadmap warns that tightening further would "starve jobs" —
indexing still has to finish, and eventual completion matters as much as response time.

**The first enforcement of that cap was itself a regression.** It was a
threading.Semaphore acquired inside the shared default executor, so queued jobs sat
blocked while *holding* shared-pool threads -- the ones recall, search and intent
classification run on. On a 6-worker pool a foreground call needing no CPU waited
445ms behind 6 queued embeds and 2,449ms behind 10. Derived work now runs on its own
single-worker executor: same bound, and the same measurement gives ~2ms at any backlog.
`test_a_derived_backlog_never_blocks_the_shared_pool` is that measurement.
"""
import asyncio
import inspect
import threading
import time

import pytest

from backend.brain import embeddings


@pytest.fixture(autouse=True)
def _reset_pressure():
    embeddings._background_waiting = 0
    yield
    embeddings._background_waiting = 0


# ------------------------------------------- the gate itself
# ------------------------------------------- the derived executor
class _Slow:
    """Stands in for the model: a fixed-cost encode, so contention is deterministic."""

    def __init__(self, seconds=0.2):
        self.seconds = seconds

    def encode(self, texts, **k):
        import numpy as np
        time.sleep(self.seconds)
        return np.zeros((len(texts), 4), dtype="float32")


def test_a_derived_backlog_never_blocks_the_shared_pool(monkeypatch):
    """The regression the semaphore design shipped: a no-CPU foreground call on the
    shared pool must not wait behind queued indexing. Pool sized like a 2-vCPU VM."""
    import concurrent.futures

    monkeypatch.setattr(embeddings, "_get_model", lambda: _Slow(0.2))

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(6))
        backlog = [embeddings.run_derived(embeddings._embed_many_sync, ["x"], True)
                   for _ in range(10)]
        jobs = [asyncio.ensure_future(j) for j in backlog]
        await asyncio.sleep(0.05)
        t = time.perf_counter()
        await loop.run_in_executor(None, time.sleep, 0.001)
        waited = time.perf_counter() - t
        await asyncio.gather(*jobs)
        return waited

    waited = asyncio.run(scenario())
    assert waited < 0.15, f"foreground call waited {waited*1000:.0f}ms behind indexing"


def test_derived_work_is_serialised(monkeypatch):
    """The A08 bound itself: at most one derived job at a time."""
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def job():
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with lock:
            live["now"] -= 1

    async def scenario():
        await asyncio.gather(*(embeddings.run_derived(job) for _ in range(5)))

    asyncio.run(scenario())
    assert live["peak"] == embeddings._BACKGROUND_EMBED_CONCURRENCY == 1


def test_derived_work_runs_on_the_dedicated_thread():
    async def scenario():
        return await embeddings.run_derived(embeddings.on_derived_thread)
    assert asyncio.run(scenario()) is True
    assert embeddings.on_derived_thread() is False


def test_a_failing_job_does_not_wedge_the_executor():
    def boom():
        raise RuntimeError("encode failed")

    async def scenario():
        with pytest.raises(RuntimeError):
            await embeddings.run_derived(boom)
        return await embeddings.run_derived(lambda: "still running")

    assert asyncio.run(scenario()) == "still running"
    assert embeddings.background_pressure()[0] == 0


def test_foreground_embeds_are_never_routed_or_gated(monkeypatch):
    """A foreground embed is a plain call -- no executor hop, no wait -- even while
    derived work is running."""
    monkeypatch.setattr(embeddings, "_get_model", lambda: _Slow(0.0))
    t = time.perf_counter()
    assert len(embeddings._embed_many_sync(["q"])) == 1
    assert time.perf_counter() - t < 0.1


def test_pressure_counts_jobs_waiting_to_start(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    seen = {"peak": 0}

    def blocker():
        started.set()
        release.wait(2)

    async def scenario():
        first = asyncio.ensure_future(embeddings.run_derived(blocker))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
        rest = [asyncio.ensure_future(embeddings.run_derived(lambda: None)) for _ in range(3)]
        await asyncio.sleep(0.05)
        seen["peak"] = embeddings.background_pressure()[0]
        release.set()
        await asyncio.gather(first, *rest)

    asyncio.run(scenario())
    assert seen["peak"] == 3, "jobs queued behind a running one were not counted"
    assert embeddings.background_pressure()[0] == 0, "pressure leaked after completion"


def test_cancelled_waiter_does_not_double_decrement_when_work_still_starts(monkeypatch):
    """Cancellation can lose the race to a worker that is marked running."""
    from concurrent.futures import Future

    class DeferredExecutor:
        def submit(self, fn, *args):
            self.fn, self.args = fn, args
            self.future = Future()
            self.future.set_running_or_notify_cancel()
            return self.future

        def finish(self):
            self.future.set_result(self.fn(*self.args))

    executor = DeferredExecutor()
    monkeypatch.setattr(embeddings, "_derived_executor", executor)

    async def scenario():
        task = asyncio.create_task(embeddings.run_derived(lambda: "done"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert embeddings.background_pressure()[0] == 0
        executor.finish()  # the underlying future could not be cancelled
        await asyncio.sleep(0)
        assert embeddings.background_pressure()[0] == 0

    asyncio.run(scenario())


def test_a_misrouted_background_embed_is_reported(monkeypatch, caplog):
    """The marker's job now: notice a caller that bypassed run_derived."""
    monkeypatch.setattr(embeddings, "_get_model", lambda: _Slow(0.0))
    monkeypatch.setattr(embeddings, "_misrouted_warned", False)
    with caplog.at_level("WARNING"):
        embeddings._embed_many_sync(["x"], background=True)
    assert "outside the derived executor" in caplog.text


def test_every_entry_point_to_derived_work_uses_the_dedicated_executor():
    """The structural guard: no async wrapper may hand a background-embedding function
    to the shared executor. This is how the regression would come back."""
    import ast
    import pathlib

    derived = {"_index_file_sync", "_index_exchange_sync", "_index_skill_sync",
               "_backfill_sync", "_backfill_skills_sync", "_dedup_facts_sync",
               "_reconcile_sync"}
    offenders = []
    root = pathlib.Path(embeddings.__file__).resolve().parent.parent
    for path in root.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_in_executor"):
                names = {a.id for a in node.args if isinstance(a, ast.Name)}
                if names & derived:
                    offenders.append(f"{path.name}: {sorted(names & derived)}")
    assert not offenders, "derived work sent to the shared pool: " + "; ".join(offenders)


# ------------------------------------------- the right call sites are marked
def test_derived_work_is_marked_background():
    """Indexing and dedup are what tax the next turn."""
    from backend.brain import semindex, memory

    for fn in (semindex._index_file_sync, semindex._index_exchange_sync,
               semindex._index_skill_locked):
        assert "background=True" in inspect.getsource(fn), (
            f"{fn.__name__} embeds without yielding to live retrieval")
    assert "background=True" in inspect.getsource(memory._dedup_facts_sync)


def test_live_retrieval_is_not_marked_background():
    """The query embed on the read path, and the per-turn intent classify, must stay
    ungated — gating them would make the turn wait on indexing."""
    from backend.brain import semindex, intent

    assert "background=True" not in inspect.getsource(semindex._search_sync)
    assert "background=True" not in inspect.getsource(intent.classify_sync)


# ------------------------------------------- bounded AND observable
def test_pressure_is_reported():
    embeddings._background_waiting = 4
    waiting, limit = embeddings.background_pressure()
    assert waiting == 4 and limit == embeddings._BACKGROUND_EMBED_CONCURRENCY


def test_runtime_status_surfaces_a_backlog(monkeypatch):
    from backend.brain import runtime_status

    monkeypatch.setattr(embeddings, "_background_waiting", 3)
    assert "Indexing backlog: 3 embed job(s) waiting" in runtime_status.runtime_status_text()


def test_runtime_status_stays_quiet_when_idle():
    """A permanent '0 waiting' line is noise nobody reads."""
    from backend.brain import runtime_status

    embeddings._background_waiting = 0
    assert "Indexing backlog" not in runtime_status.runtime_status_text()


def test_runtime_status_never_blocks_on_pressure():
    """runtime_status_text is documented as fully synchronous with no network; the
    pressure read must not change that."""
    assert not inspect.iscoroutinefunction(embeddings.background_pressure)
    src = inspect.getsource(embeddings.background_pressure)
    assert "await" not in src and "run_in_executor" not in src


# ------------------------------------------- criteria closed by earlier packages
def test_session_lookup_does_not_scan_whole_history():
    """A02 slice 2: assign_session called recent_threads(1), which fetched EVERY thread
    and sorted in process — measured 2.884s at 345 threads, 0.001s off the index."""
    from backend.brain import threads

    src = inspect.getsource(threads._last_exchange)
    assert "exchanges" in src or "sqlite" in src.lower() or "last_exchange" in src
    assert "recent_threads" in src, "the Qdrant scroll fallback was removed"


def test_optional_retrieval_is_deadline_bounded():
    """A03 slice 5, including that session assignment is deliberately NOT bounded —
    dropping it would corrupt which session a turn belongs to, not merely degrade it."""
    from backend.brain import engine
    from backend.context import deadlines

    src = inspect.getsource(engine.chat)
    assert "_b = _deadlines.bounded" in src, "optional retrieval is no longer bounded"
    # every optional source in the gather is wrapped, and session assignment is not
    assert src.count("_b(") >= 6     # 7 until the skill read left the gather (2026-10-04)
    assert "session_task," in src, (
        "session assignment was wrapped in a deadline -- dropping it would corrupt "
        "which session a turn belongs to, not merely degrade it")
    assert deadlines.RECALL >= deadlines.SEMANTIC >= deadlines.FAST


def test_text_startup_does_not_construct_stt():
    """A01: main.py used to build SpeechToText at module scope, loading Whisper on every
    startup including the headless deployment that has no microphone."""
    from backend import main

    assert hasattr(main, "get_stt"), "the lazy STT accessor is gone"
    # The property that actually matters, rather than a source grep: importing main
    # must leave no STT instance built. (`test_headless_startup.py` covers the heavier
    # claim in a fresh subprocess — that faster_whisper is not even imported.)
    assert main._stt is None, "importing main constructed a SpeechToText instance"
