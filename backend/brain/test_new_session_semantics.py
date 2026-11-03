"""A02's last deferral: `/new` during a slow tool call cannot cross-contaminate sessions.

The criterion has two halves, and only one was already satisfied.

**Safe already:** a turn records under `decision.session_id` — the id captured at *its*
start — so an exchange from the pre-`/new` conversation can never land in the fresh
session, however long its tool call runs.

**Not safe:** `force_new_session` fired `consolidate_session(old_id)` *immediately*,
while the turn was still mid-tool-call. The exchange was then written into a session
whose letter had already been produced, and `consolidate_session` is idempotent on that
letter — so it never ran again and the exchange **never reached the vault at all**.
Reproduced: consolidation ran with 0 threads, then the exchange landed.

`flush_session` already had the right ordering for shutdown. `/new` is the same session
boundary reached a different way, so it now gets the same treatment and the same 5s
bound.
"""
import asyncio

import pytest

from backend.brain import engine


@pytest.fixture(autouse=True)
def _clean():
    engine._active_session_ids.clear()
    engine._pending_thread_tasks.clear()
    engine._forced_session.clear()
    yield
    engine._active_session_ids.clear()
    engine._pending_thread_tasks.clear()
    engine._forced_session.clear()


class _Harness:
    """Records what each session contained at the moment it was consolidated."""

    def __init__(self, monkeypatch, record_delay=0.05):
        self.recorded: list[tuple[str, str]] = []
        self.consolidated: list[tuple[str, int]] = []
        self.delay = record_delay

        async def fake_consolidate(session_id):
            self.consolidated.append(
                (session_id, sum(1 for s, _ in self.recorded if s == session_id)))

        async def fake_record(user_text, assistant_text, session_id=None, channel=None):
            await asyncio.sleep(self.delay)
            self.recorded.append((session_id, user_text))

        monkeypatch.setattr(engine, "consolidate_session", fake_consolidate)
        monkeypatch.setattr(engine, "record_thread", fake_record)

    def start_turn(self, session_id, channel="discord", text="what about NVDA?"):
        task = asyncio.create_task(
            engine._record_thread_task(text, "let me check", session_id, channel))
        engine._pending_thread_tasks[channel] = task
        return task


def test_the_reproduced_incident(monkeypatch):
    """A slow turn's exchange must be in the session when it is consolidated."""
    h = _Harness(monkeypatch)

    async def scenario():
        engine._active_session_ids["discord"] = "session-A"
        task = h.start_turn("session-A")
        await asyncio.sleep(0.01)          # still mid tool call
        engine.force_new_session("discord")
        await task
        await asyncio.sleep(0.1)           # let the close task finish

    asyncio.run(scenario())

    assert h.consolidated, "the old session was never consolidated"
    session, count = h.consolidated[0]
    assert session == "session-A"
    assert count == 1, "consolidation ran before the in-flight exchange was written"


def test_the_in_flight_exchange_never_lands_in_the_new_session(monkeypatch):
    """The half that was already safe — pinned so it stays that way."""
    h = _Harness(monkeypatch)

    async def scenario():
        engine._active_session_ids["discord"] = "session-A"
        task = h.start_turn("session-A")
        await asyncio.sleep(0.01)
        engine.force_new_session("discord")
        new_id = engine._forced_session["discord"]
        await task
        await asyncio.sleep(0.1)
        return new_id

    new_id = asyncio.run(scenario())
    assert h.recorded == [("session-A", "what about NVDA?")]
    assert all(s != new_id for s, _ in h.recorded), (
        "a pre-/new exchange contaminated the fresh session")


def test_new_pins_a_distinct_session_for_the_next_turn(monkeypatch):
    _Harness(monkeypatch)

    async def scenario():
        # inside a loop: force_new_session schedules the close via create_task
        engine._active_session_ids["discord"] = "session-A"
        engine.force_new_session("discord")
        await asyncio.sleep(0.05)
        return engine._forced_session["discord"]

    pinned = asyncio.run(scenario())
    assert pinned and pinned != "session-A"


def test_new_on_an_idle_channel_consolidates_nothing(monkeypatch):
    """No active session means there is nothing to close — it must not invent one."""
    h = _Harness(monkeypatch)

    async def scenario():
        engine.force_new_session("discord")
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert h.consolidated == []
    assert engine._forced_session["discord"]


def test_a_write_that_never_finishes_does_not_block_the_close(monkeypatch):
    """Degradation: a late exchange is better than a session that is never closed."""
    h = _Harness(monkeypatch)
    monkeypatch.setattr(engine, "_CLOSE_PENDING_TIMEOUT", 0.05, raising=False)

    async def scenario():
        engine._active_session_ids["discord"] = "session-A"

        async def never():
            await asyncio.sleep(30)

        task = asyncio.create_task(never())
        engine._pending_thread_tasks["discord"] = task
        await engine._close_session_after_pending_write("discord", "session-A")
        task.cancel()

    # the real bound is 5s; assert it completes and consolidates regardless
    asyncio.run(asyncio.wait_for(scenario(), timeout=8))
    assert [s for s, _ in h.consolidated] == ["session-A"]


def test_a_failing_write_still_lets_the_session_close(monkeypatch):
    h = _Harness(monkeypatch)

    async def scenario():
        engine._active_session_ids["discord"] = "session-A"

        async def boom():
            raise RuntimeError("write failed")

        task = asyncio.create_task(boom())
        engine._pending_thread_tasks["discord"] = task
        await asyncio.sleep(0.01)
        await engine._close_session_after_pending_write("discord", "session-A")

    asyncio.run(scenario())
    assert [s for s, _ in h.consolidated] == ["session-A"]


def test_the_close_mirrors_flush_sessions_ordering():
    """Both are the same session boundary reached differently; if one grows a new
    guarantee the other should too."""
    import inspect

    import ast

    def _code_only(fn):
        """Source with docstrings stripped.

        Both functions *describe* `consolidate_session` in their docstring before the
        code reaches it, so a naive text search compares prose, not ordering — the same
        trap an A10 invariant hit against a docstring quoting `AsyncOpenAI(...)`.
        """
        tree = ast.parse(inspect.getsource(fn).lstrip())
        body = tree.body[0].body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]
        return "\n".join(ast.unparse(stmt) for stmt in body)

    for fn in (engine._close_session_after_pending_write, engine.flush_session):
        src = _code_only(fn)
        assert "_pending_thread_tasks" in src
        assert "wait_for" in src
        assert src.index("_pending_thread_tasks") < src.index("consolidate_session"), (
            f"{fn.__name__}: consolidation runs before the pending write is awaited")


def test_steering_is_cleared_so_a_stale_correction_cannot_steer_the_new_session():
    """A correction queued for the abandoned conversation must not silently steer the
    fresh one — checked at the Discord handler, which owns /new."""
    import inspect
    from backend import main

    src = inspect.getsource(main.discord_new_session)
    assert 'steering.clear("discord"' in src
