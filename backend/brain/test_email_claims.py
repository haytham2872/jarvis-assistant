"""A reply may only say an email was sent when a send actually ran this turn (P1).

With approvals, "I emailed Sarah" is newly easy to get wrong: the model calls send_email, the
gateway turns it into a request, and the model -- which saw a tool call go by -- reports it as
done. The claim check is the backstop for that, and for the older failure of claiming a send
with no call at all.
"""
import asyncio

import pytest

from backend.brain import engine

SENT_CLAIMS = [
    "I've sent the email to Sarah.",
    "Done -- I emailed Marc about Thursday.",
    "The email was sent a moment ago.",
    "Email sent to sarah.klein@acme-corp.com.",
    "I just emailed her and told her you're running late.",
    "I sent Sarah the email you asked for.",
    "Your email has been sent.",
    "I already sent that email.",
    "I drafted the email to Sarah and the address checks out. Done, I've sent it.",
    "Mail to Marc: I just sent them your answer about Thursday.",
]

HONEST_REPLIES = [
    "I haven't sent it yet -- it's waiting for your approval (#12).",
    "Not sent: it needs your approval first, so I asked you on Discord.",
    "The email has not been sent. Tap Approve on Discord and it goes out.",
    "The email was not sent; the address wasn't one I could verify.",
    "I'll send the email as soon as you approve it.",
    "Shall I send the email to Sarah now?",
    "I can email her if you like.",
    "I sent you a message on Discord with the request.",
    "I've sent you the note you asked for.",              # vault_send's claim, not this one
    "Done, I sent it.",                                   # says nothing about an email: not this check's claim
    "I haven't sent it because the email needs your approval.",
    "I could send it, but the email is still a draft.",
    "You have three emails from Marc; the newest asks about Friday.",
    "Want me to email Sarah that you're running late?",
]


@pytest.mark.parametrize("reply", SENT_CLAIMS)
def test_a_sent_claim_with_no_send_is_caught(reply):
    problems = engine._unbacked_claims(reply, set(), 0)
    assert problems and "so nothing was sent" in problems[0], reply


@pytest.mark.parametrize("reply", SENT_CLAIMS)
def test_a_real_send_backs_the_claim(reply):
    assert engine._unbacked_claims(reply, {"gmail_action", "gmail_action:send_email"}, 2) == []


@pytest.mark.parametrize("reply", SENT_CLAIMS)
def test_reading_mail_does_not_back_a_sent_claim(reply):
    """gmail_action succeeded -- but as a read. Only the send action backs "sent"."""
    reads = {"gmail_action", "gmail_action:search_emails", "gmail_action:read_email"}
    assert engine._unbacked_claims(reply, reads, 2), reply


@pytest.mark.parametrize("reply", HONEST_REPLIES)
def test_honest_talk_about_an_unsent_email_is_not_a_claim(reply):
    """Not an EMAIL claim. (Other checks may have their say: "I've sent you the note" is
    vault_send's to judge.)"""
    problems = [p for p in engine._unbacked_claims(reply, set(), 1) if "gmail_action" in p]
    assert problems == [], reply


def test_a_request_is_not_a_send_for_the_claim_check():
    """The gateway turned the send into an approval request. `_exec_tool` neither counts it
    as a success nor records the action, so "I've sent it" stays unbacked."""
    from backend.storage import approvals
    from backend.tools import approval_flow, policy

    engine._reset_turn_stats()
    policy.start_scope(["email Sarah that I'm running late"], "JARVIS: Sarah is sarah@example.com")
    call = {"id": "1", "name": "gmail_action",
            "input": {"action": "send_email", "to": "sarah@example.com", "subject": "s", "body": "b"}}

    async def go():
        async def noop(_):
            pass
        return await engine._exec_tool(call, noop)
    _, result, ok = asyncio.run(go())
    assert ok and "Not done" in result and approvals.pending()
    assert engine._unbacked_claims("I've sent the email to Sarah.", engine._turn_tools_ok,
                                   engine._turn_tool_calls)
    approval_flow._undelivered.clear()


def test_the_correction_pass_runs_once_and_replaces_a_false_sent_claim():
    engine._reset_turn_stats()
    seen = []

    async def rerun(messages):
        seen.append(messages[-1]["content"])
        return "I haven't sent it: it's waiting for your approval (#4)."

    out = asyncio.run(engine._enforce_backed_claims(
        "Done, I've sent the email to Sarah.", [{"role": "user", "content": "email Sarah"}],
        rerun, "discord"))
    assert out == "I haven't sent it: it's waiting for your approval (#4)."
    assert len(seen) == 1 and "nothing was sent" in seen[0]


def test_the_fallback_note_names_sending():
    assert "scheduled or sent" in engine._UNBACKED_NOTE
