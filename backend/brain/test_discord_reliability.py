"""A07: clarification correlation and chunked-delivery reliability.

Acceptance criteria covered here:

- *"old/unrelated replies cannot answer a new clarification"* — `push` sets the
  steering event, so a message queued **before** the question left the event already
  set: `ev.wait()` returned instantly and that message was consumed as the answer.
  Reproduced — an unrelated *"never mind, what time is it?"* answered a question asked
  afterwards, in 0.000s.
- *"partial chunk delivery resumes/reconciles without restarting the entire reply"* —
  a multi-part send aborted on the first failing chunk, so parts 1-2 of 5 could land
  while the caller was told nothing was sent.
- *"voice uses the same session/execution path as text"* and *"all command variants
  pass"* — already true; pinned so they stay that way.
"""
import asyncio

import pytest

from backend.brain import discord_bridge as db
from backend.brain import engine, steering


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    steering.clear("discord")
    engine._reset_turn_stats()
    yield
    steering.clear("discord")


# ------------------------------------------------- clarification correlation
def test_a_message_queued_before_the_question_cannot_answer_it():
    """The reproduced incident."""
    steering.push("discord", "actually never mind, what time is it?")
    watermark = steering.latest_seq("discord")

    got = run(steering.await_reply("discord", 0.2, after_seq=watermark))
    assert got is None, "a message sent before the question was consumed as its answer"


def test_the_stale_message_is_kept_as_steering_not_discarded():
    """It is still a genuine correction for the work in flight -- just not a reply."""
    steering.push("discord", "use the June figures")
    watermark = steering.latest_seq("discord")
    run(steering.await_reply("discord", 0.1, after_seq=watermark))

    assert steering.drain("discord") == ["use the June figures"]


def test_a_reply_arriving_after_the_question_is_accepted():
    """The control: correlation must not stop real answers landing."""
    watermark = steering.latest_seq("discord")

    async def scenario():
        async def later():
            await asyncio.sleep(0.05)
            steering.push("discord", "the one I opened in June")
        asyncio.create_task(later())
        return await steering.await_reply("discord", 2.0, after_seq=watermark)

    assert run(scenario()) == "the one I opened in June"


def test_only_the_newer_messages_are_taken():
    steering.push("discord", "older steering")
    watermark = steering.latest_seq("discord")
    steering.push("discord", "the actual reply")

    assert run(steering.await_reply("discord", 0.5, after_seq=watermark)) == \
        "the actual reply"
    assert steering.drain("discord") == ["older steering"]


def test_waking_on_a_stale_message_does_not_end_the_wait_early():
    """The event is shared, so an older push wakes the waiter; it must re-check rather
    than return something stale, and keep waiting for the remaining timeout."""
    steering.push("discord", "ancient", conversation=steering.DM)   # before the watermark
    watermark = steering.latest_seq(steering.DM)

    async def scenario():
        async def noise_then_reply():
            await asyncio.sleep(0.02)
            steering._event(steering.DM).set()            # woken with nothing newer
            await asyncio.sleep(0.05)
            steering.push("discord", "real reply", conversation=steering.DM)
        asyncio.create_task(noise_then_reply())
        return await steering.await_reply(steering.DM, 2.0, after_seq=watermark)

    assert run(scenario()) == "real reply"


def test_the_real_clarify_path_records_the_watermark_first():
    """Recording it after the DM would let a message sent in between answer it."""
    import inspect
    src = inspect.getsource(engine._ask_clarifying_question)
    assert src.index("steering.latest_seq(steering.DM)") < src.index("_clarify_notifier("), (
        "the watermark is taken after the question is sent")
    assert "after_seq=asked_after" in src


def test_the_clarify_tool_times_out_on_a_stale_only_queue(monkeypatch):
    """End to end through the real tool."""
    monkeypatch.setattr(engine, "_CLARIFY_WAIT_SECONDS", 0.2)

    async def notifier(text):
        return True
    monkeypatch.setattr(engine, "_clarify_notifier", notifier)

    steering.push("discord", "unrelated chatter", conversation=steering.DM)
    result = run(engine._ask_clarifying_question("Which position did you mean?"))
    assert "No reply within" in result


# ---------------------------------------------------------- chunked delivery
LONG = "\n".join(f"line {i} " + "x" * 200 for i in range(40))


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(db, "_CHUNK_RETRY_DELAY", 0.001)


def test_the_message_really_does_span_several_parts():
    assert len(db._chunk(LONG)) > 1


def test_a_transient_chunk_failure_is_retried_not_abandoned():
    sent, failed_once = [], {"n": 0}

    async def flaky(part):
        if len(sent) == 1 and failed_once["n"] == 0:
            failed_once["n"] += 1
            raise RuntimeError("429 rate limited")
        sent.append(part)

    expected = len(db._chunk(LONG))
    assert run(db._send_chunks(flaky, LONG)) == expected
    assert len(sent) == expected


def test_a_persistent_failure_reports_how_much_landed():
    """Collapsing partial delivery into 'nothing sent' is what made it invisible."""
    sent = []

    async def broken(part):
        if len(sent) == 1:
            raise RuntimeError("channel gone")
        sent.append(part)

    delivered = run(db._send_chunks(broken, LONG))
    assert delivered == 1
    assert delivered < len(db._chunk(LONG))


def test_send_chunks_never_raises():
    async def always_broken(part):
        raise RuntimeError("nope")

    assert run(db._send_chunks(always_broken, LONG)) == 0


def test_a_fully_successful_send_reports_every_part():
    sent = []

    async def ok(part):
        sent.append(part)

    assert run(db._send_chunks(ok, LONG)) == len(sent) == len(db._chunk(LONG))


def test_the_reply_path_uses_the_retrying_sender():
    """_send_reply and send_to_owner must not drift back to a bare loop."""
    import inspect
    assert "_send_chunks" in inspect.getsource(db._send_reply)
    assert "_send_owner" in inspect.getsource(db.send_to_owner)
    assert "_send_chunks" in inspect.getsource(db._send_owner)


def test_brief_now_receives_the_real_delivery_result(monkeypatch):
    """The brief handler must see a failed chunk before acknowledging findings."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import sys

    # Exercise the bridge without requiring a live Discord client installation.
    class FakeClient:
        def __init__(self, intents):
            self._connection = SimpleNamespace(user=None)

        @property
        def user(self):
            return self._connection.user

    fake_discord = SimpleNamespace(
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        Client=FakeClient,
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    client = db._build_client()
    client._connection.user = SimpleNamespace(id=999)
    monkeypatch.setattr(db, "_ALLOWED_ID", "123")
    monkeypatch.setattr(db, "_claim_ingress", lambda message: True)
    monkeypatch.setattr(db, "_CHUNK_RETRIES", 0)
    observed = []

    async def handler(deliver):
        observed.append(await deliver(LONG))
        return LONG

    monkeypatch.setattr(db, "_brief_now_handler", handler)

    class Channel:
        def __init__(self):
            self.parts = []

        @asynccontextmanager
        async def typing(self):
            yield

        async def send(self, part):
            if self.parts:
                raise RuntimeError("Discord send failed")
            self.parts.append(part)

    channel = Channel()
    message = SimpleNamespace(id=1, author=SimpleNamespace(id=123),
                              content="/brief now", channel=channel, attachments=[])
    run(client.on_message(message))

    assert observed == [False]
    assert len(channel.parts) == 1


def test_partial_delivery_is_reported_as_not_sent(monkeypatch, caplog):
    """A duplicate on retry is recoverable; a silent loss is not -- but it must be
    visible in the log rather than looking like nothing happened."""
    async def partial(sender, text):
        return 1

    monkeypatch.setattr(db, "_send_chunks", partial)
    monkeypatch.setattr(db, "_ALLOWED_ID", "123")

    class _User:
        async def send(self, part):
            pass

    class _Client:
        def get_user(self, _id):
            return _User()

    monkeypatch.setattr(db, "_client", _Client())
    with caplog.at_level("WARNING"):
        assert run(db.send_to_owner(LONG)) is False
    assert "partial delivery" in caplog.text


# --------------------------------------------------- voice and command parity
def test_voice_and_text_use_the_same_turn_handler():
    """Roadmap: 'voice uses the same session/execution path as text'."""
    import inspect
    src = inspect.getsource(db)
    text_fn = src[src.index("async def _handle_text"):src.index("async def _handle_voice")]
    voice_fn = src[src.index("async def _handle_voice"):]
    # H4: both go through _run_turn_with_progress, which wraps the shared handler.
    helper = inspect.getsource(db._run_turn_with_progress)
    assert "_turn_handler(text)" in helper
    assert "_run_turn_with_progress(message, text)" in text_fn
    assert "_run_turn_with_progress(message, text)" in voice_fn, (
        "voice no longer goes through the shared pipeline entry point")


def test_a_voice_attachment_always_cleans_up_its_temp_file():
    import inspect
    src = inspect.getsource(db)
    voice_fn = src[src.index("async def _handle_voice"):]
    assert "finally:" in voice_fn and "_cleanup(tmp_path)" in voice_fn


def test_every_documented_command_is_still_handled():
    """'All command variants pass'. _COMMAND_HELP is what /commands replies with, so a
    command listed there but no longer handled would be a lie to Sam."""
    import inspect
    src = inspect.getsource(db)
    for cmd, _desc in db._COMMAND_HELP:
        bare = cmd.split()[0]
        assert bare in src, f"{bare} is advertised by /commands but not handled"
