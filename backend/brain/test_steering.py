"""Tests for backend/brain/steering.py -- Step 1's mid-turn steering queue.

Pure logic + one asyncio synchronization primitive (an Event per channel),
no I/O, no dependency on main.py or discord_bridge.py. Each test resets the
module's global dicts so tests can't leak state into each other.
"""

import asyncio

import pytest

from backend.brain import steering


@pytest.fixture(autouse=True)
def _reset_module_state():
    steering._reset_memory()
    yield
    steering._reset_memory()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# push / drain / pending
# ---------------------------------------------------------------------------

def test_push_then_drain_returns_in_order():
    steering.push("local", "first")
    steering.push("local", "second")
    assert steering.drain("local") == ["first", "second"]


def test_drain_empties_the_queue():
    steering.push("local", "x")
    steering.drain("local")
    assert steering.drain("local") == []


def test_drain_on_untouched_channel_returns_empty_list():
    assert steering.drain("never-pushed") == []


def test_push_blank_text_is_a_noop():
    steering.push("local", "   ")
    assert steering.drain("local") == []


def test_push_strips_whitespace():
    steering.push("local", "  hi there  ")
    assert steering.drain("local") == ["hi there"]


def test_pending_reflects_queue_state():
    assert steering.pending("local") is False
    steering.push("local", "x")
    assert steering.pending("local") is True
    steering.drain("local")
    assert steering.pending("local") is False


def test_channels_are_independent():
    steering.push("local", "desk message")
    steering.push("discord", "phone message")
    assert steering.drain("local") == ["desk message"]
    assert steering.drain("discord") == ["phone message"]


def test_a_burst_keeps_every_message():
    """G3 / D3: this test used to pin the defect -- maxlen=3 silently dropped the oldest.
    Every acknowledged message now has an outcome; none is evicted."""
    for i in range(5):
        steering.push("local", f"msg{i}")
    assert steering.drain("local") == [f"msg{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# format_interjection / join_as_followup
# ---------------------------------------------------------------------------

def test_format_interjection_single_message():
    text = steering.format_interjection(["stop, wrong company"])
    assert "stop, wrong company" in text
    assert "mid-task" in text


def test_format_interjection_multiple_messages_bulleted():
    text = steering.format_interjection(["first correction", "second correction"])
    assert "- first correction" in text
    assert "- second correction" in text


def test_join_as_followup_combines_with_newlines():
    assert steering.join_as_followup(["a", "b"]) == "a\nb"


def test_join_as_followup_single_message_unchanged():
    assert steering.join_as_followup(["just this"]) == "just this"


# ---------------------------------------------------------------------------
# await_reply -- timing-sensitive, exercised with a real (short) asyncio clock
# ---------------------------------------------------------------------------

def test_await_reply_times_out_when_nothing_arrives():
    result = run(steering.await_reply("local", timeout=0.05))
    assert result is None


def test_await_reply_returns_immediately_when_answer_already_queued():
    steering.push("local", "already here")
    result = run(steering.await_reply("local", timeout=5.0))
    assert result == "already here"


def test_await_reply_wakes_as_soon_as_push_happens():
    async def scenario():
        async def delayed_push():
            await asyncio.sleep(0.02)
            steering.push("local", "the answer")

        task = asyncio.create_task(delayed_push())
        start = asyncio.get_event_loop().time()
        result = await steering.await_reply("local", timeout=5.0)
        elapsed = asyncio.get_event_loop().time() - start
        await task
        return result, elapsed

    result, elapsed = run(scenario())
    assert result == "the answer"
    # Woken by the event, not polled to the full 5s timeout.
    assert elapsed < 1.0


def test_await_reply_drains_the_queue_it_reads_from():
    async def scenario():
        steering.push("local", "answer")
        await steering.await_reply("local", timeout=1.0)
        return steering.pending("local")

    assert run(scenario()) is False


def test_await_reply_joins_multiple_messages_that_arrived_together():
    steering.push("discord", "part one")
    steering.push("discord", "part two")
    result = run(steering.await_reply("discord", timeout=1.0))
    assert result == "part one\npart two"


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

def test_clear_drops_pending_messages_without_returning_them():
    steering.push("discord", "stale correction")
    steering.clear("discord")
    assert steering.drain("discord") == []


def test_clear_on_untouched_channel_does_not_raise():
    steering.clear("never-touched")  # must not raise
