"""A03 slice 5: one slow store cannot stall a turn.

Roadmap acceptance: "a slow optional source respects a deadline". Before this, `chat()`
gathered eight retrievals unbounded — a degraded Qdrant or a slow mem0 hybrid search held
the whole conversation for as long as it took (first measured at recall p50 6.4s / max 24.1s -- mostly a `localhost` connect stall; 0.255s / 0.400s once fixed).
"""
import asyncio

import pytest

from backend.context import deadlines


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    deadlines.reset()
    yield
    deadlines.reset()


async def _slow(seconds, value="late"):
    await asyncio.sleep(seconds)
    return value


async def _fast(value="quick"):
    return value


def test_fast_source_returns_its_value():
    assert run(deadlines.bounded(_fast(), source_id="x", timeout=1.0)) == "quick"
    assert deadlines.timed_out() == []


def test_slow_source_degrades_to_the_default():
    got = run(deadlines.bounded(_slow(5), source_id="vault:topic", timeout=0.05))
    assert got == ""
    assert [sid for sid, _ in deadlines.timed_out()] == ["vault:topic"]


def test_custom_default_is_used():
    """recall() returns a RecallResult, not a string -- the default must match the
    shape the caller expects or the turn breaks a line later."""
    sentinel = ("", "")
    got = run(deadlines.bounded(_slow(5), source_id="vault:skill",
                                timeout=0.05, default=sentinel))
    assert got is sentinel


def test_other_exceptions_also_degrade_but_are_not_recorded_as_timeouts():
    """The individual read functions already handle their own failures; a raised
    exception here is not evidence that a store is slow."""
    async def _boom():
        raise RuntimeError("qdrant down")

    assert run(deadlines.bounded(_boom(), source_id="x", timeout=1.0)) == ""
    assert deadlines.timed_out() == []


def test_cancellation_propagates():
    """An interrupted turn must actually stop, not be swallowed into a default."""
    async def _main():
        task = asyncio.create_task(
            deadlines.bounded(_slow(10), source_id="x", timeout=30))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(_main())


def test_timeouts_are_reset_per_turn():
    run(deadlines.bounded(_slow(5), source_id="a", timeout=0.01))
    assert len(deadlines.timed_out()) == 1
    deadlines.reset()
    assert deadlines.timed_out() == []


def test_one_slow_source_does_not_delay_the_others():
    """The actual property: sources run concurrently, so the turn waits for the longest
    DEADLINE, not the sum of them."""
    async def _main():
        started = asyncio.get_event_loop().time()
        results = await asyncio.gather(
            deadlines.bounded(_slow(10), source_id="slow", timeout=0.1),
            deadlines.bounded(_fast("a"), source_id="f1", timeout=5),
            deadlines.bounded(_fast("b"), source_id="f2", timeout=5),
        )
        return results, asyncio.get_event_loop().time() - started

    results, elapsed = run(_main())
    assert results == ["", "a", "b"]
    assert elapsed < 1.0, f"took {elapsed:.2f}s -- sources are not overlapping"


def test_deadline_values_are_ordered_by_measured_cost():
    """Guards against someone tightening a deadline below what the source really costs.
    Measured warm: file reads ~0.001s, semantic ~2.8s, recall p50 6.4s / max 24.1s."""
    assert deadlines.FAST < deadlines.SEMANTIC < deadlines.RECALL
    # Corrected measurements (see deadlines.py): semantic max 0.101s, recall max 0.400s.
    # At least 10x headroom over each, so load or a slower host never trips a healthy
    # store -- but no longer the 12s that let a sick one hold a turn.
    assert deadlines.SEMANTIC >= 10 * 0.101, "semantic bound too tight for its cost"
    assert deadlines.RECALL >= 10 * 0.400, "recall bound too tight for its cost"
    assert deadlines.RECALL <= 8.0, "recall bound back to masking a sick store"


def test_chat_bounds_optional_sources_but_not_session_assignment():
    """Static guard. Dropping session_task would not degrade the turn -- it would
    corrupt which session the turn belongs to."""
    import inspect

    from backend.brain import engine

    src = inspect.getsource(engine.chat)
    gather = src[src.index("await asyncio.gather("):src.index("if _deadlines.timed_out():")]
    assert "session_task," in gather
    # session_task must NOT be wrapped
    session_line = [l for l in gather.splitlines() if "session_task" in l][0]
    assert "_b(" not in session_line, "session assignment must stay unbounded"
    assert "vault:skill" not in gather     # skills are opened by the model (skills.py)
    for bounded_source in ("recall", "vault:owner", "vault:topic",
                           "predictions"):
        assert f'source_id="{bounded_source}"' in gather, f"{bounded_source} unbounded"
