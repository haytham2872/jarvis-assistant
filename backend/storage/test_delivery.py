"""Persisted Discord chunk receipts and restart recovery."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.brain import discord_bridge as bridge
from backend.storage import db, delivery


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "delivery.db"))
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def test_confirmed_chunks_resume_without_resending():
    body = "a" * 3900
    chunks = bridge._chunk(body)
    assert len(chunks) == 3
    record = delivery.prepare("reply:42", "channel:7", body, chunks)
    assert record["state"] == "pending"
    assert delivery.claim_part("reply:42", 0)
    assert delivery.mark_sent("reply:42", 0, "remote-1")
    assert delivery.get("reply:42")["state"] == "partial"

    sent = []

    async def sender(part):
        sent.append(part)
        return SimpleNamespace(id=len(sent) + 1)

    assert asyncio.run(bridge._send_durable(sender, body, intent_id="reply:42",
                                            route="channel:7"))
    assert sent == chunks[1:]
    assert delivery.get("reply:42")["state"] == "sent"
    assert asyncio.run(bridge._send_durable(sender, body, intent_id="reply:42",
                                            route="channel:7"))
    assert sent == chunks[1:]


def test_ambiguous_send_is_not_retried():
    body = "b" * 3900
    sent = []

    async def sender(part):
        sent.append(part)
        if len(sent) == 2:
            raise ConnectionError("reply might have landed")
        return SimpleNamespace(id=len(sent))

    assert not asyncio.run(bridge._send_durable(sender, body, intent_id="reply:43",
                                                route="channel:7"))
    state = delivery.get("reply:43")
    assert [p["state"] for p in state["parts"]] == ["sent", "uncertain", "pending"]
    assert state["state"] == "uncertain"
    assert not asyncio.run(bridge._send_durable(sender, body, intent_id="reply:43",
                                                route="channel:7"))
    assert len(sent) == 2


def test_restart_marks_an_inflight_part_uncertain():
    body = "hello"
    delivery.prepare("reply:44", "channel:7", body, [body])
    assert delivery.claim_part("reply:44", 0)
    assert delivery.reconcile_inflight() == 1
    assert delivery.get("reply:44")["state"] == "uncertain"
    assert delivery.resumable() == []


def test_key_collision_rejected_without_sending():
    assert delivery.prepare("reply:45", "channel:7", "original", ["original"])
    assert delivery.prepare("reply:45", "channel:7", "changed", ["changed"]) is None
    assert delivery.get("reply:45")["body"] == "original"


def test_resume_keeps_original_chunk_boundaries_after_code_change():
    body = "saved reply"
    delivery.prepare("reply:47", "channel:7", body, ["saved ", "reply"])
    # A newer chunking rule could generate a single part from the same body.
    record = delivery.prepare("reply:47", "channel:7", body, [body])
    assert [part["body"] for part in record["parts"]] == ["saved ", "reply"]


def test_startup_resumes_pending_reply(monkeypatch):
    body = "c" * 3900
    chunks = bridge._chunk(body)
    delivery.prepare("reply:46", "channel:7", body, chunks)
    assert delivery.claim_part("reply:46", 0)
    assert delivery.mark_sent("reply:46", 0, "remote-1")

    class Channel:
        async def send(self, part):
            sent.append(part)
            return SimpleNamespace(id=len(sent) + 1)

    class Client:
        def get_channel(self, channel_id):
            assert channel_id == 7
            return Channel()

    sent = []
    monkeypatch.setattr(bridge, "_client", Client())
    asyncio.run(bridge._resume_pending())
    assert sent == chunks[1:]
    assert delivery.get("reply:46")["state"] == "sent"
