"""Gmail headers (20 October 2026). The mail JARVIS sent went out with its subject, but he
could not see it: send_email wrote the headers as "to" and "subject", and the read paths
looked up "Subject" exactly, so his own mail showed "(no subject)" in a search and "?" when
read. He then told Sam his emails had gone out without a subject. Header names are
case-insensitive (RFC 5322); they are read that way now and written the usual way."""
import asyncio
import base64
import email

import pytest

from backend.tools import gmail
from backend.tools.test_gmail_untrusted import FakeGmail, _Req


class _Sent(FakeGmail):
    """send() stores the message the way Gmail does: with the header names it was given."""

    def send(self, userId, body):
        self.sent.append(body)
        msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        self.messages_by_id["sent1"] = {
            "headers": dict(msg.items()) | {"Date": "Tue, 20 Oct 2026 11:05:00 +0200"},
            "body": msg.get_payload()}
        return _Req({"id": "sent1"})


@pytest.fixture
def box(monkeypatch):
    fake = _Sent({"m1": {"headers": {"from": "Marc <marc@x.fr>", "subject": "Lease",
                                     "date": "Mon", "to": "sam@x.com"}, "body": "hi"}})
    monkeypatch.setattr(gmail, "_get_service", lambda: fake)
    return fake


def run(**kw):
    return asyncio.run(gmail.gmail_action(**kw))


def test_lowercase_headers_are_read(box):
    assert "Subject: Lease" in run(action="search_emails", query="x")
    assert "Subject: Lease" in run(action="list_inbox")
    out = run(action="read_email", email_id="m1")
    assert "Subject: Lease" in out and "From: Marc" in out and "To: sam@x.com" in out


def test_a_sent_email_is_written_with_the_usual_names_and_read_back(box):
    out = run(action="send_email", to="sarah@x.com", subject="Running late", body="Ten minutes.")
    raw = base64.urlsafe_b64decode(box.sent[0]["raw"]).decode()
    assert "\nTo: sarah@x.com" in "\n" + raw and "\nSubject: Running late" in "\n" + raw
    assert out.startswith("Email sent to sarah@x.com.")
    assert "Subject: Running late" in out and "sent1" in out
    found = run(action="search_emails", query="in:sent")
    assert "Subject: Running late" in found and "To: sarah@x.com" in found


def test_a_send_whose_read_back_fails_still_says_it_was_sent(box, monkeypatch):
    real_get = box.get

    def get(userId, id, format, metadataHeaders=None):
        if id == "sent1":
            raise RuntimeError("timeout")
        return real_get(userId, id, format, metadataHeaders)
    monkeypatch.setattr(box, "get", get)
    out = run(action="send_email", to="sarah@x.com", subject="Late", body="b")
    assert out.startswith("Email sent to sarah@x.com.") and "sent1" in out
