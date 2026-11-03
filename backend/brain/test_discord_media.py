"""Discord: images and documents reach a turn, voice notes show what was heard (4 October 2026)."""
import asyncio
import io
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.brain import attachments, discord_bridge as bridge, discord_context
from backend.storage import db
from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "runtime.db"))
    db._reset_for_tests()
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    monkeypatch.setattr(bridge, "_ALLOWED_ID", "123")
    fake_discord = SimpleNamespace(
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        Client=type("Client", (), {"__init__": lambda self, intents: None}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    client = bridge._build_client()
    client.user = SimpleNamespace(id=77)
    monkeypatch.setattr(bridge, "_claim_ingress", lambda _: True)
    monkeypatch.setattr(bridge, "_client", SimpleNamespace(user=SimpleNamespace(id=77)))
    state = {"turns": [], "sent": [], "replies": []}

    async def handler(text):
        ctx = discord_context.current.get()
        state["turns"].append((text, getattr(ctx, "voice", None)))
        return "answered"

    async def send_reply(channel, text, **_kw):
        state["replies"].append(text)
        return True

    async def notes_off(*a, **k):
        return None
    monkeypatch.setattr(bridge, "_turn_handler", handler)
    monkeypatch.setattr(bridge, "_send_reply", send_reply)
    monkeypatch.setattr(attachments, "_image_note", notes_off)
    attachments._in_view.clear()
    attachments._documents.clear()
    yield client, state
    attachments._in_view.clear()
    attachments._documents.clear()
    db._reset_for_tests()


def message(state, content="", files=()):
    @asynccontextmanager
    async def typing():
        yield

    async def send(text):
        state["sent"].append(text)
    channel = SimpleNamespace(typing=typing, send=send, id="dm")
    return SimpleNamespace(id=50, author=SimpleNamespace(id=123), content=content, reference=None,
                           attachments=list(files), channel=channel)


def attachment(name, ct, data, size=None):
    reads = []

    async def read():
        reads.append(name)
        return data

    async def save(path):
        open(path, "wb").write(data)
    att = SimpleNamespace(filename=name, content_type=ct, size=len(data) if size is None else size,
                          read=read, save=save)
    return att, reads


def png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(buf, "PNG")
    return buf.getvalue()


def test_an_image_with_words_runs_one_turn_with_a_marker(world):
    client, state = world
    att, _ = attachment("shot.png", "image/png", png())
    run(client.on_message(message(state, "what is this?", [att])))
    (text, voice), = state["turns"]
    assert text.startswith("what is this?\n\n[Sent image: attachments/") and text.endswith(".md]")
    assert voice is False and state["replies"] == ["answered"]
    assert len(attachments.turn_images("discord")) == 1


def test_an_image_alone_still_gets_an_answer(world):
    """Before this an image with no text was silently dropped."""
    client, state = world
    att, _ = attachment("photo.jpg", "image/jpeg", png())
    run(client.on_message(message(state, "", [att])))
    assert state["turns"][0][0].startswith("[Sent image: attachments/")


def test_a_file_over_the_limit_is_not_downloaded_and_is_named_back(world):
    client, state = world
    att, reads = attachment("huge.pdf", "application/pdf", b"%PDF", size=attachments.MAX_DOCUMENT_BYTES + 1)
    run(client.on_message(message(state, "", [att])))
    assert reads == [] and state["turns"] == []
    assert state["sent"] == ["Couldn't read huge.pdf (it is over 25 MB)."]


def test_an_unreadable_file_is_named_and_the_rest_still_goes_through(world):
    client, state = world
    bad, _ = attachment("x.zip", "application/zip", b"PK")
    good, _ = attachment("notes.txt", "text/plain", b"call the notary before Friday")
    run(client.on_message(message(state, "see these", [bad, good])))
    assert state["sent"] == ["Couldn't read x.zip (I can't read .zip files)."]
    assert "[Sent document: attachments/" in state["turns"][0][0]
    assert "call the notary" in attachments.turn_note("discord", [])


def test_a_voice_note_is_not_taken_for_a_file(world, monkeypatch):
    client, state = world
    monkeypatch.setattr(bridge, "_transcribe_handler", lambda path: "remind me to call Sarah at ten")
    att, _ = attachment("voice-message.ogg", "audio/ogg", b"OggS")
    run(client.on_message(message(state, "", [att])))
    assert state["sent"][0] == 'Heard: "remind me to call Sarah at ten"'
    assert state["turns"] == [("remind me to call Sarah at ten", True)]   # marked as a transcript
    assert discord_context.current.get() is None


def test_what_was_heard_is_shown_whole_up_to_three_hundred_characters():
    assert bridge.heard_line("  bonjour   Jarvis ") == 'Heard: "bonjour Jarvis"'
    long = bridge.heard_line("word " * 200)
    assert long.endswith(' …"') and len(long) < 320
