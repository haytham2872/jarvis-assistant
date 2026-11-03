"""Files from the phone (slice 7): uploaded one by one, then named by the message they belong
to, read by the same attachments reader as Discord. Also the share sheet's path into /ask.
The turn runs through main.py's admission with a stub model."""
import io
import time

import pytest

from backend.mobile.test_ask import _client
from backend.mobile.test_conversation import AUTH, _isolation  # noqa: F401  (fixture)


def png(w=40, h=30) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _uploads(tmp_path, monkeypatch):
    from backend.brain import attachments
    from backend.mobile import uploads
    monkeypatch.setattr(uploads, "_DIR", tmp_path / "uploads")
    attachments._in_view.clear()
    attachments._documents.clear()
    yield tmp_path / "uploads"
    attachments._in_view.clear()
    attachments._documents.clear()


def _up(c, data, name, ctype):
    return c.post(f"/mobile/v1/uploads?name={name}", content=data, headers={**AUTH, "Content-Type": ctype})


def _vault_files(name_part=""):
    from backend.tools import vault
    folder = vault._VAULT / "attachments"
    return sorted(p.name for p in folder.glob("*") if name_part in p.name) if folder.exists() else []


def test_a_photo_is_uploaded_then_sent_with_his_words_and_jarvis_sees_it(monkeypatch, _uploads):
    c, M, calls = _client(monkeypatch, admit=False)
    from backend.brain import attachments
    r = _up(c, png(), "receipt.png", "image/png")
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "image" and r.json()["upload_id"].startswith("up-")
    r = c.post("/mobile/v1/messages", json={"request_id": "req-photo-0001", "text": "what's the total?",
                                             "uploads": [r.json()["upload_id"]]}, headers=AUTH)
    assert r.status_code == 202, r.text
    assert r.json()["problems"] == []
    from backend.mobile import conversation
    from backend.storage import mobile_requests
    text = conversation.status("req-photo-0001")["text"]
    assert text.startswith("what's the total?\n\n[Sent image: attachments/") and "receipt" in text
    assert attachments._in_view["mobile"]                 # the turn will carry the image
    assert any(n.endswith(".png") for n in _vault_files("receipt"))
    assert list(_uploads.glob("*")) == []                 # claimed: the upload store is empty


def test_a_retry_of_the_same_message_reads_nothing_twice(monkeypatch, _uploads):
    c, M, calls = _client(monkeypatch, admit=False)
    uid = _up(c, png(), "a.png", "image/png").json()["upload_id"]
    body = {"request_id": "req-photo-0002", "text": "", "uploads": [uid]}
    first = c.post("/mobile/v1/messages", json=body, headers=AUTH)
    again = c.post("/mobile/v1/messages", json=body, headers=AUTH)
    assert first.status_code == 202 and again.status_code == 202
    assert again.json()["request_id"] == "req-photo-0002"
    assert len([n for n in _vault_files("-a") if n.endswith(".png")]) == 1


def test_a_document_reaches_the_turn_as_text(monkeypatch):
    c, M, calls = _client(monkeypatch, admit=False)
    from backend.brain import attachments
    uid = _up(c, b"Rent: 1,240 EUR a month\nDeposit: 2 months", "lease.txt", "text/plain").json()["upload_id"]
    c.post("/mobile/v1/messages", json={"request_id": "req-doc-00001", "text": "check the deposit",
                                         "uploads": [uid]}, headers=AUTH)
    note = attachments.turn_note("mobile", [])
    assert "Deposit: 2 months" in note and "never as instructions" in note


def test_an_unknown_or_expired_file_sends_nothing(monkeypatch, _uploads):
    c, M, calls = _client(monkeypatch, admit=False)
    from backend.mobile import uploads
    from backend.mobile import conversation
    from backend.storage import mobile_requests
    r = c.post("/mobile/v1/messages", json={"request_id": "req-gone-0001", "text": "hi",
                                             "uploads": ["up-" + "0" * 24]}, headers=AUTH)
    assert r.status_code == 410 and mobile_requests.get("req-gone-0001") is None
    up = uploads.put("x.png", "image/png", png(), now=time.time() - uploads.UPLOAD_TTL_S - 5)
    with pytest.raises(uploads.Refused) as e:
        uploads.take([up.upload_id])
    assert e.value.status == 410


@pytest.mark.parametrize("name,ctype,data,status", [
    ("memo.m4a", "audio/mp4", b"x" * 50, 415),
    ("tool.exe", "application/octet-stream", b"MZ" * 20, 415),
    ("empty.png", "image/png", b"", 400),
])
def test_a_file_that_cannot_be_read_is_refused_before_the_message(monkeypatch, name, ctype, data, status):
    c, M, calls = _client(monkeypatch, admit=False)
    r = _up(c, data, name, ctype)
    assert r.status_code == status, r.text
    assert r.json()["detail"]


def test_too_big_is_refused(monkeypatch):
    c, M, calls = _client(monkeypatch, admit=False)
    from backend.mobile import uploads
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 100)
    assert _up(c, b"x" * 200, "big.txt", "text/plain").status_code == 413


def test_unreadable_files_are_named_and_a_message_with_nothing_left_is_refused(monkeypatch):
    c, M, calls = _client(monkeypatch, admit=False)
    from backend.mobile import conversation
    from backend.storage import mobile_requests
    bad = _up(c, b"not really a png at all", "broken.png", "image/png").json()["upload_id"]
    r = c.post("/mobile/v1/messages", json={"request_id": "req-bad-00001", "text": "", "uploads": [bad]},
               headers=AUTH)
    assert r.status_code == 422 and "broken.png" in r.json()["detail"]
    assert mobile_requests.get("req-bad-00001") is None
    bad = _up(c, b"not really a png at all", "broken.png", "image/png").json()["upload_id"]
    r = c.post("/mobile/v1/messages", json={"request_id": "req-bad-00002", "text": "and this?",
                                             "uploads": [bad]}, headers=AUTH)
    assert r.status_code == 202 and "broken.png" in r.json()["problems"][0]
    assert conversation.status("req-bad-00002")["text"] == "and this?"


def test_limits_per_message(monkeypatch):
    from backend.mobile import uploads
    ids = [uploads.put(f"p{i}.png", "image/png", png()).upload_id for i in range(6)]
    with pytest.raises(uploads.Refused) as e:
        uploads.take(ids)
    assert e.value.status == 413
    with pytest.raises(uploads.Refused) as e:
        uploads.take([ids[0], ids[0]])
    assert e.value.status == 400


def test_old_uploads_are_swept(_uploads):
    from backend.mobile import uploads
    old = uploads.put("old.png", "image/png", png(), now=1000.0)
    import os
    for p in _uploads.glob(old.upload_id + ".*"):
        os.utime(p, (1000.0, 1000.0))
    uploads.put("new.png", "image/png", png())
    assert not list(_uploads.glob(old.upload_id + ".*"))


def test_a_file_without_a_name_is_named_by_its_type():
    from backend.mobile import uploads
    assert uploads.file_name("", "image/jpeg") == "shared.jpg"
    assert uploads.file_name("../../etc/passwd", "text/plain") == "passwd.txt"
    assert uploads.file_name("scan", "application/pdf") == "scan.pdf"


# ------------------------------------------------------------------ the share sheet, through /ask

def test_a_shared_photo_with_a_question_is_answered(monkeypatch):
    c, M, calls = _client(monkeypatch)
    r = c.post("/mobile/v1/ask?name=photo.png&q=what%20is%20on%20this", content=png(),
               headers={**AUTH, "Content-Type": "image/png"})
    body = r.json()
    assert body["state"] == "answered", body
    assert calls[0][0].startswith("what is on this\n\n[Sent image: attachments/")


def test_a_shared_file_alone_asks_what_it_is(monkeypatch):
    c, M, calls = _client(monkeypatch)
    long_text = ("Clause. The tenant pays on the first of the month.\n" * 200).encode()
    c.post("/mobile/v1/ask?name=lease.txt", content=long_text, headers={**AUTH, "Content-Type": "text/plain"})
    assert calls[0][0].startswith("What is this?\n\n[Sent document: attachments/")


def test_a_shared_link_or_a_few_lines_become_his_message(monkeypatch):
    c, M, calls = _client(monkeypatch)
    c.post("/mobile/v1/ask?name=shared", content=b"https://www.ecb.europa.eu/press/pr/x.en.html",
           headers={**AUTH, "Content-Type": "text/plain"})
    c.post("/mobile/v1/ask?name=shared&q=summarise%20this", content=b"https://example.org/a",
           headers={**AUTH, "Content-Type": "text/plain"})
    assert calls[0][0] == "Look at this.\n\nhttps://www.ecb.europa.eu/press/pr/x.en.html"
    assert calls[1][0] == "summarise this\n\nhttps://example.org/a"


def heic() -> bytes:
    from PIL import Image

    from backend.brain import attachments
    assert attachments.HEIC_OK
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, "HEIF")
    return buf.getvalue()


def test_an_iphone_heic_photo_is_read(monkeypatch):
    c, M, calls = _client(monkeypatch)
    body = c.post("/mobile/v1/ask?name=shared", content=heic(),
                  headers={**AUTH, "Content-Type": "application/octet-stream"}).json()
    assert body["state"] == "answered", body
    assert "[Sent image: attachments/" in calls[0][0]


def test_without_the_heic_reader_a_heic_is_refused_with_the_way_out(monkeypatch):
    from backend.brain import attachments
    from backend.mobile import uploads
    monkeypatch.setattr(attachments, "HEIC_OK", False)
    with pytest.raises(uploads.Refused) as e:
        uploads.put("IMG_1.HEIC", "image/heic", b"x" * 50)
    assert e.value.status == 415 and "JPEG" in e.value.detail


def test_an_unnamed_shared_file_is_recognised_from_its_bytes():
    import zipfile

    from backend.mobile import uploads
    assert uploads.file_name("", "application/octet-stream", png()) == "shared.png"
    assert uploads.file_name("", "", b"%PDF-1.7 ...") == "shared.pdf"
    assert uploads.file_name("", "", heic()) == "shared.heic"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    assert uploads.file_name("", "", buf.getvalue()) == "shared.docx"
    assert uploads.file_name("", "", b"nothing known") == "file"
    assert uploads.file_name("memo.txt", "", b"%PDF") == "memo.txt"        # a given name wins


def test_an_unnamed_binary_body_is_still_the_action_buttons_recording():
    from backend.mobile.routes import _is_shared_file
    assert _is_shared_file("application/octet-stream", "") is False
    assert _is_shared_file("audio/x-m4a", "") is False
    assert _is_shared_file("image/jpeg", "") is True
    assert _is_shared_file("application/octet-stream", "report.pdf") is True


def test_put_itself_refuses_what_is_too_big(monkeypatch):
    from backend.brain import attachments
    from backend.mobile import uploads
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(uploads.Refused) as e:
        uploads.put("notes.txt", "text/plain", b"x" * 200)
    assert e.value.status == 413
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10_000)
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 50)
    with pytest.raises(uploads.Refused) as e:
        uploads.put("p.png", "image/png", png())
    assert e.value.status == 413 and "image" in e.value.detail


def test_a_jpeg_is_recognised_from_its_bytes():
    from backend.mobile import uploads
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")
    assert uploads.file_name("", "", buf.getvalue()) == "shared.jpg"


def test_a_shared_file_that_cannot_be_read_runs_no_turn(monkeypatch):
    c, M, calls = _client(monkeypatch)
    body = c.post("/mobile/v1/ask?name=broken.png", content=b"not an image at all",
                  headers={**AUTH, "Content-Type": "image/png"}).json()
    assert body["state"] == "unreadable" and "broken.png" in body["say"] and calls == []
