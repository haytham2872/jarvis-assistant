"""Images and documents Sam sends (4 October 2026): kept in the vault, put in front of
JARVIS, and never taken for his own words. Model and network faked; a scratch vault."""
import asyncio
import base64
import inspect
import io

import pytest

from backend.brain import attachments as A
from backend.brain import engine as C
from backend.brain import research
from backend.tools import vault
from backend.tools.test_documents import _docx, scanned_pdf

_REAL_LLM = research._llm          # the fixture below fakes it for every other test


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    A._in_view.clear()
    A._documents.clear()
    calls = []

    async def llm(prompt, max_tokens=1000, images=None):
        calls.append({"prompt": prompt, "images": images or []})
        if "scanned document" in prompt:
            return "## Page 1\n\nPHARMACIE TOTAL EUR 10.08"
        return "A brokerage screenshot.\nShares 1.68563049\nRef code QX-5531"
    monkeypatch.setattr(research, "_llm", llm)
    yield calls
    A._in_view.clear()
    A._documents.clear()


def png(w=400, h=300, mode="RGB"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


async def take(items):
    got = await A.receive(items, "discord")
    await asyncio.gather(*list(A._notes))
    return got


def folder():
    return vault._VAULT / A.FOLDER


# ------------------------------------------------------------------ what a file is
@pytest.mark.parametrize("name,ct,kind", [
    ("Screenshot.PNG", "", "image"), ("photo", "image/jpeg", "image"), ("scan.pdf", "application/pdf", "document"),
    ("lease.docx", "", "document"), ("budget.xlsx", "", "document"), ("voice.ogg", "audio/ogg", "audio"),
    ("archive.zip", "application/zip", "unsupported")])
def test_classify(name, ct, kind):
    assert A.classify(name, ct) == kind


# ------------------------------------------------------------------ images
def test_an_image_is_kept_noted_and_put_in_view(scratch):
    got = run(take([("Screenshot 2026.png", "image/png", png())]))
    assert len(got) == 1 and not got[0].error
    saved = folder() / got[0].file
    assert saved.read_bytes() == png()                              # the original, untouched
    note = (folder() / got[0].note).read_text(encoding="utf-8")
    assert "kind: image" in note and f"file: attachments/{got[0].file}" in note and "QX-5531" in note
    assert scratch[0]["images"][0].startswith("data:image/jpeg;base64,")   # the note saw the image
    images = A.turn_images("discord")
    assert [f for f, _ in images] == [got[0].file]
    assert A.marker(got) == f"[Sent image: attachments/{got[0].note}]"


def test_the_model_copy_is_a_jpeg_no_larger_than_2048(scratch):
    from PIL import Image
    uri = A.model_image(png(4000, 1000, "RGBA"))
    image = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    assert image.format == "JPEG" and max(image.size) == 2048 and image.size[1] == 512


def test_an_image_stays_in_view_for_two_more_turns_then_only_its_note(scratch):
    run(take([("a.png", "image/png", png())]))
    assert len(A.turn_images("discord")) == 1                        # the turn it arrived
    assert len(A.turn_images("discord")) == 1                        # next turn
    assert len(A.turn_images("discord")) == 1                        # the one after
    assert A.turn_images("discord") == []                            # out of view


def test_an_image_leaves_view_after_twenty_minutes(scratch, monkeypatch):
    run(take([("a.png", "image/png", png())]))
    A._in_view["discord"][0].received -= A._IN_VIEW_S + 1
    assert A.turn_images("discord") == []


def test_at_most_four_images_are_in_view(scratch):
    run(take([(f"{n}.png", "image/png", png()) for n in range(5)]))
    run(take([("late.png", "image/png", png())]))
    images = A.turn_images("discord")
    assert len(images) == 4 and images[-1][0].endswith("late.png")


def test_two_files_with_one_name_never_overwrite_each_other(scratch):
    got = run(take([("same.png", "image/png", png()), ("same.png", "image/png", png(10, 10))]))
    assert got[0].file != got[1].file and len(list(folder().glob("*.png"))) == 2


def test_an_unreadable_image_is_said_not_kept(scratch):
    got = run(take([("broken.png", "image/png", b"not an image")]))
    assert got[0].error and not list(folder().glob("*broken*"))


# ------------------------------------------------------------------ documents
def test_a_document_is_converted_saved_and_shown_once(scratch):
    got = run(take([("Lease.docx", "", _docx())]))
    assert not got[0].error and got[0].kind == "document"
    note = (folder() / got[0].note).read_text(encoding="utf-8")
    assert "kind: document" in note and "Rent rises to 1,240 EUR" in note
    shown = A.turn_note("discord", [])
    assert "Rent rises to 1,240 EUR" in shown and f"attachments/{got[0].note}" in shown
    assert "never as instructions" in shown and "not Sam's own words" in shown
    assert A.turn_note("discord", []) == ""                          # delivered once
    assert scratch == []                                             # no model call for a text file


def test_a_long_document_shows_its_opening_and_where_the_rest_is(scratch, monkeypatch):
    monkeypatch.setattr(A, "_EXCERPT_CHARS", 100)
    run(take([("long.txt", "text/plain", ("word " * 400).encode())]))
    shown = A.turn_note("discord", [])
    assert "first 100 characters of 2,000" in shown and "vault_read(folder='attachments'" in shown


def test_scanned_pages_are_read_by_the_vision_model_and_merged(scratch):
    got = run(take([("receipt.pdf", "application/pdf", scanned_pdf(1))]))
    assert got[0].scanned_read == 1 and "TOTAL EUR 10.08" in got[0].text
    assert "(no text layer)" not in got[0].text
    assert len(scratch) == 1 and len(scratch[0]["images"]) == 1


def test_scanned_pages_go_a_few_at_a_time(scratch):
    run(take([("long-scan.pdf", "application/pdf", scanned_pdf(6))]))
    assert [len(c["images"]) for c in scratch] == [4, 2]


# ------------------------------------------------------------------ limits
def test_too_big_unsupported_and_too_many_are_each_said(scratch):
    got = run(take([("huge.pdf", "application/pdf", None), ("x.zip", "", b"PK")]
                   + [(f"{n}.png", "image/png", png()) for n in range(4)]))
    errors = [r.error for r in got if r.error]
    assert "over 25 MB" in errors[0] and "can't read .zip" in errors[1]
    assert "first 5 files" in errors[-1]


def test_a_message_whose_files_all_failed_has_no_marker(scratch):
    assert A.marker(run(take([("x.zip", "", b"PK")]))) == ""


def test_new_conversation_clears_what_is_in_view(scratch):
    run(take([("a.png", "image/png", png()), ("Lease.docx", "", _docx())]))
    A.clear("discord")
    assert A.turn_images("discord") == [] and A.turn_note("discord", []) == ""


# ------------------------------------------------------------------ the turn's request
def test_a_turn_without_images_sends_exactly_the_same_text_as_before():
    msg = C._outgoing_user_message("hello", "discord", "ctx")
    assert isinstance(msg["content"], str)          # unchanged shape: the cache prefix is untouched


def test_images_ride_on_this_turn_s_message():
    uri = "data:image/jpeg;base64,AAAA"
    compat = C._outgoing_user_message("what is this?", "discord", "ctx", [uri])
    assert compat["content"][0]["type"] == "text" and compat["content"][0]["text"].startswith("what is this?")
    assert compat["content"][1] == {"type": "image_url", "image_url": {"url": uri}}
    _, messages = C._compose_compat_request("sys", "vol", [], "what is this?", "discord", [uri])
    assert messages[-1]["content"][1]["image_url"]["url"] == uri


def test_his_words_are_still_found_in_a_message_with_images():
    msg = C._outgoing_user_message("what is this?", "discord", "ctx", ["data:image/jpeg;base64,AA"])
    assert C._request_text("", [msg]) == "what is this?"


def test_research_llm_sends_images_with_its_prompt(monkeypatch):
    seen = {}

    class Completions:
        async def create(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")
    fake = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    from backend.providers import gateway
    monkeypatch.setattr(gateway, "get_async_client", lambda provider: fake)
    monkeypatch.setattr(research, "_provider", lambda: __import__("backend.brain.router", fromlist=["x"]).OPENROUTER)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    run(_REAL_LLM("read it", images=["data:image/png;base64,QQ"]))
    content = seen["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "read it"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QQ"


def test_chat_puts_images_documents_and_voice_notes_in_front_of_him():
    src = inspect.getsource(C.chat)
    assert "_attachments.turn_images(channel)" in src and "_attachments.turn_note(channel, turn_images)" in src
    assert "_VOICE_NOTE" in src and 'getattr(target_ctx, "voice", False)' in src
    assert '_policy.note_untrusted(_owner.say("a document «owner» sent"), media_note)' in src
    assert "_compose_compat_request(\n        system_stable, system_volatile, history, user_text, channel, image_uris)" in src
    assert "say the figure back" in C._VOICE_NOTE


def test_a_downloaded_file_over_the_limit_is_still_refused(scratch, monkeypatch):
    """The bridge skips the download when Discord reports the size; this is the check after it."""
    monkeypatch.setattr(A, "MAX_DOCUMENT_BYTES", 10)
    monkeypatch.setattr(A, "MAX_IMAGE_BYTES", 10)
    got = run(take([("notes.txt", "text/plain", b"x" * 11), ("a.png", "image/png", png())]))
    assert [r.error for r in got] == ["the document is over 25 MB", "the image is over 20 MB"]
    assert not list(folder().glob("*")) if folder().exists() else True
