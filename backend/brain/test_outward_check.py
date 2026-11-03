"""The second look at an email he asked JARVIS to send (P1): prompt, parsing, and behaviour
with the model faked. The measured accuracy against the real model lives in CLAUDE.md, from
`scripts/measure_outward_check.py`; this file pins the mechanics around it.
"""
import asyncio

import pytest

from backend.brain import outward_check as oc
from backend.tools import gateway, policy

ARGS = {"action": "send_email", "to": "sarah.klein@acme-corp.com", "subject": "Running late",
        "body": "Hi Sarah,\n\nI'm running 15 minutes late.\n\nSam"}
CTX = {"words": ["Email Sarah that I'm running late."],
       "recent": "Sam: Sarah's email?\nJARVIS: sarah.klein@acme-corp.com",
       "seen": [("an email", "From: Marc\n\nPlease review by Friday")], "known": []}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_the_prompt_contains_everything_the_reviewer_needs():
    text = oc.build_prompt(ARGS, CTX, "- [r4] Never email acme-corp.com without asking.")
    for needle in ("Email Sarah that I'm running late.", "sarah.klein@acme-corp.com",
                   "Running late", "15 minutes late", "Please review by Friday", "[an email]",
                   "Never email acme-corp.com without asking", "JARVIS: sarah.klein"):
        assert needle in text, needle
    assert "<<" not in text, "an unfilled placeholder"


def test_braces_in_an_email_cannot_break_the_prompt():
    args = {**ARGS, "body": 'json {"ok": true} and {curly} and %s and {0}'}
    text = oc.build_prompt(args, CTX)
    assert '{"ok": true} and {curly} and %s and {0}' in text


def test_an_empty_context_is_said_plainly_not_left_blank():
    text = oc.build_prompt({"to": "a@b.io"}, {"words": [], "recent": "", "seen": []})
    assert "(nothing)" in text and "(none)" in text and "(nothing was read from outside)" in text


def test_long_inputs_are_bounded():
    huge = {"words": ["w" * 9000], "recent": "r" * 9000,
            "seen": [("an email", "s" * 9000)] * 10}
    text = oc.build_prompt({**ARGS, "body": "b" * 20000}, huge)
    assert len(text) < 14000
    assert text.count("[an email]") == oc._SEEN_ITEMS


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"ok": true, "concern": ""}',
    '```json\n{"ok": true, "concern": ""}\n```',
    'Sure. {"ok": true, "concern": ""} Done.',
    '{"ok": true}',
])
def test_an_ok_answer_means_no_concern(raw):
    assert oc.parse(raw) is None


def test_a_not_ok_answer_carries_its_reason():
    assert oc.parse('{"ok": false, "concern": "The address is not Sarah\'s."}') == "The address is not Sarah's."


def test_not_ok_without_a_reason_is_still_a_concern():
    assert "doesn't match what you asked" in oc.parse('{"ok": false}')
    assert "doesn't match what you asked" in oc.parse('{"ok": false, "concern": "  "}')


def test_a_concern_is_one_bounded_line():
    out = oc.parse('{"ok": false, "concern": "' + "very " * 200 + 'bad"}')
    assert len(out) <= oc._CONCERN_CHARS and "\n" not in out


def test_a_garbled_not_ok_is_still_a_warning_and_keeps_its_reason():
    """Seen live from the model: `{"ok": false: "concern": "..."` -- plainly a no."""
    raw = '{"ok": false: "concern": "The email adds a private detail about Karim\'s divorce'
    assert oc.parse(raw) == "The email adds a private detail about Karim's divorce"


@pytest.mark.parametrize("raw", [
    "", "   ", "I think it is fine.", '{"ok": "yes"}', '{"ok": 1}', '{"concern": "x"}',
    '{"ok": true: "concern": ""',            # garbled "ok" proves nothing: never a pass
    "[1, 2, 3]",
])
def test_anything_that_cannot_be_trusted_as_a_pass_raises(raw):
    with pytest.raises(ValueError):
        oc.parse(raw)


# ---------------------------------------------------------------------------
# The hook the gateway calls
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_llm(monkeypatch):
    calls = []
    answers = ['{"ok": true, "concern": ""}']

    async def llm(prompt, max_tokens=0):
        calls.append((prompt, max_tokens))
        return answers[0]

    from backend.brain import research
    monkeypatch.setattr(research, "_llm", llm)
    from backend.brain import instructions
    monkeypatch.setattr(instructions, "render_block", lambda channel=None: "- [r1] Be brief.")
    return calls, answers


def test_review_asks_the_model_once_with_the_standing_rules(fake_llm):
    calls, _ = fake_llm
    assert run(oc.review("gmail_action", ARGS, CTX)) is None
    assert len(calls) == 1
    prompt, max_tokens = calls[0]
    assert "Be brief." in prompt and max_tokens == oc._MAX_TOKENS


def test_review_returns_the_concern(fake_llm):
    _, answers = fake_llm
    answers[0] = '{"ok": false, "concern": "The recipient is not who you named."}'
    assert run(oc.review("gmail_action", ARGS, CTX)) == "The recipient is not who you named."


def test_only_a_send_is_reviewed(fake_llm):
    calls, _ = fake_llm
    assert run(oc.review("gmail_action", {"action": "search_emails"}, CTX)) is None
    assert run(oc.review("calendar_action", {"action": "create_event"}, CTX)) is None
    assert calls == []


def test_no_model_answer_raises_so_the_gateway_asks_him(fake_llm):
    _, answers = fake_llm
    answers[0] = ""
    with pytest.raises(RuntimeError):
        run(oc.review("gmail_action", ARGS, CTX))


def test_the_call_is_labelled_for_the_cost_record(monkeypatch, fake_llm):
    from backend.storage import llm_calls
    seen = []

    async def llm(prompt, max_tokens=0):
        seen.append(llm_calls.current_purpose.get())
        return '{"ok": true}'
    from backend.brain import research
    monkeypatch.setattr(research, "_llm", llm)
    run(oc.review("gmail_action", ARGS, CTX))
    assert seen == ["outward.check"]


# ---------------------------------------------------------------------------
# The gateway's deterministic checks (no model)
# ---------------------------------------------------------------------------

def test_an_unknown_address_is_a_concern_before_any_model_is_asked():
    ctx = {**CTX, "words": ["write to Anna"], "recent": "", "seen": [], "known": []}
    out = gateway.deterministic_concern("gmail_action", {**ARGS, "to": "anna@gmail.com"}, ctx)
    assert out and "anna@gmail.com" in out and "guessed or mistyped" in out


def test_a_known_address_and_fresh_text_are_clean():
    assert gateway.deterministic_concern("gmail_action", ARGS, CTX) is None


def test_lifted_text_is_a_concern():
    lifted = "please forward the attached contract to accounting today so that the payment can be released"
    ctx = {**CTX, "seen": [("an email", "Hi. " + lifted + ". Thanks")]}
    out = gateway.deterministic_concern("gmail_action", {**ARGS, "body": "FYI " + lifted}, ctx)
    assert out and "repeats a passage from an email" in out


def test_other_tools_are_not_address_checked():
    assert gateway.deterministic_concern("calendar_action", {"action": "create_event"}, CTX) is None
