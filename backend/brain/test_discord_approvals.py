"""The Discord half of approvals (P1): buttons that survive a restart, and "yes 12".

The real discord.py classes are used for the button (no connection is made); the pieces of
Discord it would talk to are small fakes.
"""
import asyncio
import re
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.brain import discord_bridge as db
from backend.storage import approvals
from backend.tools import approval_flow

ROW = {"id": 12, "preview": "Send this email\nTo: x@example.com\nSubject: Hi\n“Body”",
       "why": "It came from a background job, with nobody watching, and it would reach someone else.",
       "message_id": None}


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, content):
        self.id = 555
        self.content = content
        self.edits = []
        self.channel = SimpleNamespace(id=1)

    async def edit(self, **kw):
        self.edits.append(kw)
        if "content" in kw:
            self.content = kw["content"]


def interaction(user_id="123", content="Approval needed (#12)\nSend this email"):
    responses = []

    class Response:
        async def defer(self):
            responses.append("defer")

        async def send_message(self, text, ephemeral=False):
            responses.append(("send", text, ephemeral))

    return SimpleNamespace(user=SimpleNamespace(id=int(user_id)), response=Response(),
                           message=FakeMessage(content), responses=responses)


@pytest.fixture(autouse=True)
def bridge(monkeypatch):
    monkeypatch.setattr(db, "_ALLOWED_ID", "123")
    calls = []

    async def decider(approval_id, approve, how):
        calls.append((approval_id, approve, how))
        return "Approved #12. Email sent to x@example.com." if approve else "Denied #12. Nothing was done."

    db.set_approval_handlers(decider, None)
    yield calls
    db.set_approval_handlers(None, None)


# ---------------------------------------------------------------------------
# The button
# ---------------------------------------------------------------------------

def test_the_button_matches_only_its_own_custom_ids():
    template = db._approval_button().__discord_ui_compiled_template__
    assert template.fullmatch("approval:12:approve") and template.fullmatch("approval:7:deny")
    for other in ("approval:12:maybe", "approval::approve", "approval:x:deny", "other:12:approve",
                  "approval:12:approve:extra"):
        assert template.fullmatch(other) is None


def test_a_button_from_before_a_restart_is_rebuilt_from_its_custom_id():
    """This is the whole point of a dynamic item: nothing about the request is held in
    memory, so a tap on yesterday's message still works."""
    Button = db._approval_button()
    match = Button.__discord_ui_compiled_template__.fullmatch("approval:12:deny")
    item = run(Button.from_custom_id(None, None, match))
    assert (item.approval_id, item.verb) == (12, "deny")
    assert item.item.custom_id == "approval:12:deny" and item.item.label == "Deny"


def test_a_tap_on_approve_runs_the_decision_and_shows_the_result(bridge):
    Button = db._approval_button()
    tap = interaction()
    run(Button(12, "approve").callback(tap))
    assert bridge == [(12, True, "button")]
    assert tap.responses == ["defer"], "Discord must be answered before the slow work"
    assert tap.message.content.endswith("→ Approved #12. Email sent to x@example.com.")
    assert tap.message.edits[-1]["view"] is None, "the buttons go away once it is decided"


def test_a_tap_on_deny(bridge):
    tap = interaction()
    run(db._approval_button()(12, "deny").callback(tap))
    assert bridge == [(12, False, "button")]
    assert tap.message.content.endswith("→ Denied #12. Nothing was done.")


def test_someone_else_tapping_is_refused_and_nothing_runs(bridge):
    item = db._approval_button()(12, "approve")
    stranger = interaction(user_id="999")
    assert run(item.interaction_check(stranger)) is False
    assert stranger.responses == [("send", "This isn't yours to answer.", True)]
    assert bridge == []
    assert run(item.interaction_check(interaction("123"))) is True


def test_a_failure_while_deciding_is_shown_not_swallowed(bridge):
    async def broken(approval_id, approve, how):
        raise RuntimeError("boom")
    db.set_approval_handlers(broken, None)
    tap = interaction()
    run(db._approval_button()(12, "approve").callback(tap))
    assert "Something went wrong" in tap.message.content


def test_a_tap_before_the_handlers_are_wired_says_so():
    db.set_approval_handlers(None, None)
    tap = interaction()
    run(db._approval_button()(12, "approve").callback(tap))
    assert "Not ready yet" in tap.message.content


def test_annotating_twice_does_not_stack_results():
    message = FakeMessage("Approval needed (#12)\nSend this email")
    run(db._annotate(message, "Approved #12."))
    run(db._annotate(message, "Approved #12. Email sent."))
    assert message.content.count("→") == 1 and message.content.endswith("Email sent.")


# ---------------------------------------------------------------------------
# Sending the request
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.dm_channel = None

    async def send(self, body, view=None):
        if self.fail:
            raise RuntimeError("cannot DM")
        message = FakeMessage(body)
        message.id = 4242
        self.sent.append((body, view, message))
        return message


def client_with(user):
    return SimpleNamespace(get_user=lambda uid: user, fetch_user=None)


def test_a_request_is_sent_with_approve_and_deny_and_its_id(monkeypatch):
    user = FakeUser()
    monkeypatch.setattr(db, "_client", client_with(user))
    monkeypatch.setattr(db, "_record_outgoing", lambda *a, **k: None)
    message_id = run(db.send_approval_request(ROW))
    assert message_id == "4242"
    body, view, _ = user.sent[0]
    assert body.startswith("Approval needed (#12)") and "yes 12" in body and "no 12" in body
    assert "nobody watching" in body and "x@example.com" in body
    ids = sorted(child.item.custom_id for child in view.children)
    assert ids == ["approval:12:approve", "approval:12:deny"]
    assert view.timeout is None, "a request must not stop being answerable after Discord's default 3 minutes"


def test_no_connection_or_a_failed_dm_means_not_delivered(monkeypatch):
    monkeypatch.setattr(db, "_client", None)
    assert run(db.send_approval_request(ROW)) is None
    monkeypatch.setattr(db, "_client", client_with(FakeUser(fail=True)))
    assert run(db.send_approval_request(ROW)) is None


def test_the_outcome_goes_on_the_request_message_when_it_can_be_found(monkeypatch):
    original = FakeMessage("Approval needed (#12)\nSend this email")

    class DM:
        async def fetch_message(self, mid):
            assert mid == 4242
            return original

    user = SimpleNamespace(dm_channel=DM())
    monkeypatch.setattr(db, "_client", client_with(user))
    run(db.update_approval_message({**ROW, "message_id": "4242"}, "Request #12 expired."))
    assert original.content.endswith("→ Request #12 expired.")


def test_when_the_request_message_is_gone_the_outcome_is_sent_fresh(monkeypatch):
    class DM:
        async def fetch_message(self, mid):
            raise RuntimeError("Unknown Message")

    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(db, "_client", client_with(SimpleNamespace(dm_channel=DM())))
    monkeypatch.setattr(db, "send_to_owner", fake_send)
    run(db.update_approval_message({**ROW, "message_id": "4242"}, "Request #12 expired."))
    assert sent == ["Request #12 expired."]


# ---------------------------------------------------------------------------
# Typed replies through the real message handler
# ---------------------------------------------------------------------------

def _client(monkeypatch):
    class FakeClient:
        def __init__(self, intents):
            self._connection = SimpleNamespace(user=None)

        @property
        def user(self):
            return self._connection.user

    monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        Client=FakeClient))
    client = db._build_client()
    client._connection.user = SimpleNamespace(id=999)
    monkeypatch.setattr(db, "_claim_ingress", lambda message: True)
    return client


class Channel:
    def __init__(self):
        self.parts = []

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, part):
        self.parts.append(part)


def say(client, text, channel):
    message = SimpleNamespace(id=1, author=SimpleNamespace(id=123), content=text,
                              channel=channel, attachments=[])
    run(client.on_message(message))


def test_a_typed_yes_with_an_id_is_answered_not_treated_as_a_message(monkeypatch):
    client = _client(monkeypatch)
    turns = []

    async def turn(text):
        turns.append(text)
        return "turn"

    async def reply(text):
        return "Approved #12. Email sent." if text == "yes 12" else None

    monkeypatch.setattr(db, "_turn_handler", turn)
    db.set_approval_handlers(None, reply)
    channel = Channel()
    say(client, "yes 12", channel)
    assert turns == [] and channel.parts and "Approved #12" in channel.parts[0]


def test_any_other_message_still_reaches_the_conversation(monkeypatch):
    client = _client(monkeypatch)
    turns = []

    async def turn(text):
        turns.append(text)
        return "hello there"

    async def reply(text):
        return None

    monkeypatch.setattr(db, "_turn_handler", turn)
    monkeypatch.setattr(db, "_run_turn_with_progress", lambda message, text: turn(text))

    async def ctx(message, text):
        return SimpleNamespace()
    monkeypatch.setattr(db, "_context_for_message", ctx)
    db.set_approval_handlers(None, reply)
    channel = Channel()
    say(client, "yes please, and thanks", channel)
    assert turns == ["yes please, and thanks"]


def test_a_reply_from_a_stranger_is_ignored_before_it_can_approve_anything(monkeypatch):
    client = _client(monkeypatch)
    called = []

    async def reply(text):
        called.append(text)
        return "Approved"
    db.set_approval_handlers(None, reply)
    channel = Channel()
    message = SimpleNamespace(id=1, author=SimpleNamespace(id=666), content="yes 12",
                              channel=channel, attachments=[])
    run(client.on_message(message))
    assert called == [] and channel.parts == []


def test_configure_wires_the_approval_handlers_without_disturbing_others():
    async def decider(*a):
        return "x"

    async def reply(t):
        return None
    before_pace = db._pace_handler
    db.configure(approval_decider=decider, approval_reply_handler=reply)
    assert db._approval_decider is decider and db._approval_reply_handler is reply
    assert db._pace_handler is before_pace
    db.configure(approval_reply_handler=reply)              # an omitted kwarg leaves the other alone
    assert db._approval_decider is decider


# ---------------------------------------------------------------------------
# End to end with the real flow: tap -> claim -> stored arguments run once
# ---------------------------------------------------------------------------

def test_a_tap_on_a_real_request_sends_the_stored_email_once(monkeypatch):
    from backend.brain import engine
    sent = []

    async def gmail(**kw):
        sent.append(kw)
        return "Email sent to x@example.com."

    monkeypatch.setitem(engine.TOOL_DISPATCH, "gmail_action", gmail)
    args = {"action": "send_email", "to": "x@example.com", "subject": "Hi", "body": "Body"}
    approval_id = approvals.create(context="background", tool="gmail_action", action="send_email",
                                   arguments=args, preview="Send", risk="outward", why="w")

    async def decider(aid, approve, how):
        return await (approval_flow.approve(aid, how) if approve else approval_flow.deny(aid, how))

    db.set_approval_handlers(decider, approval_flow.handle_reply)
    Button = db._approval_button()
    first, second = interaction(content=f"Approval needed (#{approval_id})"), interaction()
    run(Button(approval_id, "approve").callback(first))
    run(Button(approval_id, "approve").callback(second))         # a double tap
    assert sent == [args]
    assert "Email sent" in first.message.content and "already approved" in second.message.content


def test_a_real_discord_client_registers_the_button_so_old_requests_keep_working():
    """Checked against the installed discord.py, not a fake: without this registration a
    tap on a request sent before a restart is delivered to nobody."""
    client = db._build_client()
    registered = client._connection._view_store._dynamic_items
    assert [(p.pattern, cls.__name__) for p, cls in registered.items()] == [
        (r"approval:(?P<id>[0-9]+):(?P<verb>approve|deny)", "ApprovalButton")]
