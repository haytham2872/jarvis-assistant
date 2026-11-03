"""Tests for backend/brain/bootstrap.py — startup wiring registry.

Covers the collapsed main.py wiring: batched set_notifier/set_busy_check
calls across a list of modules, and the create-task/cancel-all loop
lifecycle that replaced five hand-written task variables.
"""

import asyncio

import pytest

from backend.brain import bootstrap


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# wire_notifier / wire_busy_check
# ---------------------------------------------------------------------------

class _FakeModule:
    def __init__(self):
        self.notifier = None
        self.busy_check = None

    def set_notifier(self, fn):
        self.notifier = fn

    def set_busy_check(self, fn):
        self.busy_check = fn


def test_wire_notifier_sets_every_module():
    mods = [_FakeModule(), _FakeModule(), _FakeModule()]
    fn = lambda text: None
    bootstrap.wire_notifier(mods, fn)
    assert all(m.notifier is fn for m in mods)


def test_wire_notifier_empty_list_is_a_noop():
    bootstrap.wire_notifier([], lambda: None)   # must not raise


def test_wire_busy_check_sets_every_module():
    mods = [_FakeModule(), _FakeModule()]
    fn = lambda: True
    bootstrap.wire_busy_check(mods, fn)
    assert all(m.busy_check is fn for m in mods)


def test_wire_notifier_only_touches_notifier_not_busy_check():
    mod = _FakeModule()
    bootstrap.wire_notifier([mod], lambda text: None)
    assert mod.notifier is not None
    assert mod.busy_check is None


def test_same_module_can_receive_both():
    """chronos/research get both notifier and busy_check wired -- must not conflict."""
    mod = _FakeModule()
    notifier = lambda text: None
    busy = lambda: False
    bootstrap.wire_notifier([mod], notifier)
    bootstrap.wire_busy_check([mod], busy)
    assert mod.notifier is notifier
    assert mod.busy_check is busy


# ---------------------------------------------------------------------------
# start_loops / cancel_all
# ---------------------------------------------------------------------------

def test_start_loops_creates_one_task_per_entry():
    calls = []

    async def fn_a():
        calls.append("a")
        await asyncio.sleep(10)   # never returns on its own -- cancelled below

    async def fn_b():
        calls.append("b")
        await asyncio.sleep(10)

    async def scenario():
        tasks = bootstrap.start_loops({"a": fn_a, "b": fn_b})
        await asyncio.sleep(0)   # let both tasks start running
        assert set(tasks.keys()) == {"a", "b"}
        assert all(isinstance(t, asyncio.Task) for t in tasks.values())
        bootstrap.cancel_all(tasks)

    run(scenario())
    assert set(calls) == {"a", "b"}


def test_start_loops_preserves_insertion_order():
    async def fn():
        await asyncio.sleep(10)

    async def scenario():
        specs = {"research": fn, "backup": fn, "canary": fn}
        tasks = bootstrap.start_loops(specs)
        order = list(tasks.keys())
        bootstrap.cancel_all(tasks)
        return order

    assert run(scenario()) == ["research", "backup", "canary"]


def test_cancel_all_cancels_every_task():
    async def fn():
        await asyncio.sleep(10)

    async def scenario():
        tasks = bootstrap.start_loops({"a": fn, "b": fn})
        await asyncio.sleep(0)
        bootstrap.cancel_all(tasks)
        await asyncio.sleep(0)
        return tasks

    tasks = run(scenario())
    assert all(t.cancelled() or t.done() for t in tasks.values())


def test_cancel_all_skips_already_done_tasks_without_error():
    async def fn():
        return None   # completes immediately

    async def scenario():
        tasks = bootstrap.start_loops({"a": fn})
        await asyncio.sleep(0)   # let it finish
        assert tasks["a"].done()
        bootstrap.cancel_all(tasks)   # must not raise on an already-done task

    run(scenario())


def test_cancel_all_empty_dict_is_a_noop():
    bootstrap.cancel_all({})   # must not raise


def test_cancel_all_tolerates_none_values():
    """Defensive: mirrors the old `if task is not None` guard every module
    used to write by hand before this existed."""
    bootstrap.cancel_all({"a": None})   # must not raise
