import asyncio
import gc

from backend import tasks


def test_spawn_holds_the_task_until_it_finishes():
    async def main():
        gate = asyncio.Event()
        done = []

        async def work():
            await gate.wait()          # awaiting something only this task refers to
            done.append(True)

        tasks.spawn(work())
        gc.collect()
        assert len(tasks._running) == 1
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert done == [True] and not tasks._running
    asyncio.run(main())


def test_spawn_without_a_loop_closes_the_coroutine():
    async def work():
        pass
    coro = work()
    try:
        tasks.spawn(coro)
    except RuntimeError:
        pass
    assert coro.cr_frame is None          # closed, so no "never awaited" warning
