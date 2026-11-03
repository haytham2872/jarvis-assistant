"""Skills, slice 4 (4 October 2026): the "followed" line, one-sentence corrections as typed
patches, and a reply checked against the skill's Checks before it goes out."""
import asyncio
import inspect
import json

import pytest

from backend.brain import background, research, skill_checks, skills
from backend.storage import lifelog
from backend.storage import skills as store
from backend.tools import policy, vault

PREP = dict(name="meeting-prep", description="Prepare Sam for a meeting: emails, profile, three points.",
            when_to_use="He has a meeting coming up.",
            steps=["Find the event with calendar_action", "Read the person's profile with person_profile",
                   "Search the mail with gmail_action", "Give three points"],
            checks=["Exactly three points", "Names the meeting time"])


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _live(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(skills, "_last_followed", {})
    monkeypatch.setattr(skills, "_footers", {})
    token = policy.current_caller.set(policy.LIVE)
    run(skills.skill("save", **PREP))
    skills.start_turn()
    yield
    policy.current_caller.reset(token)


def patch(*edits, reason="skip the profile step next time"):
    return run(skills.skill("patch", name="meeting-prep", edits=list(edits), reason=reason))


def steps():
    return skills._section(skills.load("meeting-prep").body, ("procedure",)).splitlines()


# ------------------------------------------------------------------ the followed line
def test_a_reply_that_followed_a_skill_says_so():
    assert skills.turn_footer() == ""
    run(skills.skill("view", name="meeting-prep"))
    assert skills.turn_footer() == "\n\n· followed: meeting-prep"


def test_following_one_skill_and_changing_another_give_two_lines():
    run(skills.skill("save", name="stock-research", description="Research a stock.", steps=["Look it up"],
                     checks=["Bull and bear case"]))
    skills.start_turn()
    run(skills.skill("view", name="stock-research"))
    patch({"op": "remove", "section": "procedure", "index": 2})
    lines = skills.turn_footer().strip().splitlines()
    assert lines[0] == "· followed: stock-research"
    assert lines[1].startswith("· Updated skill meeting-prep: removed step 2") and lines[1].endswith(skills.UNDO_HINT)


def test_a_skill_opened_and_then_changed_was_worked_on_not_followed():
    """5 October: opening a skill before changing or scheduling it is not following it, so its
    reply gets no "followed" line and no Checks."""
    run(skills.skill("view", name="meeting-prep"))
    patch({"op": "remove", "section": "procedure", "index": 2})
    assert skills.followed() == []
    lines = skills.turn_footer().strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("· Updated skill meeting-prep")


def test_saving_over_a_skill_works_on_it():
    run(skills.skill("view", name="meeting-prep"))
    out = run(skills.skill("save", **{**PREP, "steps": PREP["steps"][:3]}))
    assert out.startswith("Confirmed"), out
    assert skills.followed() == []


@pytest.mark.parametrize("action", ["retire", "export", "undo"])
def test_retiring_exporting_or_undoing_works_on_the_skill(action, monkeypatch):
    async def sender(filename, text, caption):
        return True, "attachment"
    monkeypatch.setattr(skills, "_file_sender", sender)
    run(skills.skill("view", name="meeting-prep"))
    out = run(skills.skill(action, name="meeting-prep"))
    assert not out.startswith("Error"), out
    assert skills.followed() == []


def _next_week():
    from datetime import timedelta
    from backend.brain import chronos
    return (chronos.now_local() + timedelta(days=7)).strftime("%Y-%m-%d 09:00")


def test_scheduling_a_routine_works_on_its_skill(tmp_path, monkeypatch):
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    run(skills.skill("view", name="meeting-prep"))
    out = run(chronos.schedule_task("add", when=_next_week(), repeat="weekly",
                                    task="Prepare Sam for this week's meetings and send the three points.",
                                    skill="meeting-prep"))
    assert out.startswith("Confirmed"), out
    assert skills.followed() == []
    skills.start_turn()
    run(skills.skill("view", name="meeting-prep"))
    out = run(chronos.schedule_task("cancel", task="meeting-prep"))
    assert out.startswith("Cancelled 1"), out
    assert skills.followed() == []


def test_a_plain_scheduled_task_leaves_a_followed_skill_alone(tmp_path, monkeypatch):
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    run(skills.skill("view", name="meeting-prep"))
    run(chronos.schedule_task("add", when=_next_week(), task="Tell Sam the NVDA price at the open."))
    assert skills.followed() == ["meeting-prep"]


def test_slash_skill_counts_as_following():
    skills.forced("/skill meeting-prep Marc")
    assert skills.followed() == ["meeting-prep"]


# ------------------------------------------------------------------ the next message
def test_the_message_after_gets_the_skill_and_how_to_change_it():
    run(skills.skill("view", name="meeting-prep"))
    skills.close_turn("discord")
    ctx = skills.correction_context("discord", "next time skip the profile step")
    assert "Your last reply followed the skill below" in ctx and "2. Read the person's profile" in ctx
    assert 'skill(action="patch"' in ctx and "standing rule" in ctx
    assert skills.correction_context("local", "next time skip the profile step") == ""


def test_a_message_that_corrects_nothing_does_not_get_the_steps():
    """Live: with the steps given to every next message, back-to-back research requests
    followed them without opening the skill, so no Checks ran (8 of 10 opened)."""
    run(skills.skill("view", name="meeting-prep"))
    skills.close_turn("discord")
    assert skills.correction_context("discord", "what's going on with Nvidia, should I worry?") == ""
    assert skills.correction_context("discord", "the profile part was useless") != ""
    assert skills.correction_context("discord", "ok", correction=True) != ""      # Jev says so


def test_only_right_after_and_only_once_a_turn_without_skills_follows(monkeypatch):
    run(skills.skill("view", name="meeting-prep"))
    skills.close_turn("discord")
    names, at = skills._last_followed["discord"]
    skills._last_followed["discord"] = (names, at - 31 * 60)
    assert skills.correction_context("discord", "skip that next time") == ""
    skills.start_turn()
    run(skills.skill("view", name="meeting-prep"))
    skills.close_turn("discord")
    skills.start_turn()
    skills.close_turn("discord")                 # a turn that followed nothing
    assert skills.correction_context("discord", "skip that next time") == ""


def test_a_routine_message_names_its_skill_and_can_be_corrected(monkeypatch):
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(background, "_notifier", notifier)
    t = background.BgTask(id=1, description="routine", status="done", kind="scheduled",
                          notify=True, result="TTWO closed at $238.40.", skill="meeting-prep")
    run(background._notify_result(t))
    assert sent == ["TTWO closed at $238.40.\n\n· followed: meeting-prep"]
    assert "meeting-prep" in skills.correction_context("discord", "skip the analyst part next time")


# ------------------------------------------------------------------ patch
def test_removing_a_step_renumbers_dates_it_and_keeps_his_words():
    out = patch({"op": "remove", "section": "procedure", "index": 2})
    assert out.startswith("Confirmed: skill 'meeting-prep' changed: removed step 2 (Read the person's profile")
    assert steps() == ["1. Find the event with calendar_action", "2. Search the mail with gmail_action",
                       "3. Give three points"]
    body = skills.load("meeting-prep").body
    assert f'- {lifelog.local_date()}: removed step 2' in body and 'he said: "skip the profile step next time"' in body
    assert store.revisions("meeting-prep")[0]["actor"] == "owner"


def test_a_patch_can_be_undone_exactly():
    path = vault._VAULT / "skills" / "meeting-prep.md"
    before = path.read_bytes()
    patch({"op": "remove", "section": "procedure", "index": 2})
    run(skills.skill("undo", name="meeting-prep"))
    assert path.read_bytes() == before


def test_adding_a_pitfall_creates_the_section_in_its_place():
    patch({"op": "add", "section": "pitfalls", "text": "A first name alone finds no profile"},
          reason="use his full name")
    body = skills.load("meeting-prep").body
    assert body.index("## Procedure") < body.index("## Pitfalls") < body.index("## Checks")
    assert "- A first name alone finds no profile" in body


def test_add_at_a_position_and_revise():
    patch({"op": "add", "section": "procedure", "index": 1, "text": "Check he hasn't cancelled"},
          {"op": "revise", "section": "checks", "index": 1, "text": "Three points, no more"})
    assert steps()[0] == "1. Check he hasn't cancelled" and len(steps()) == 5
    assert "- Three points, no more" in skills.load("meeting-prep").body


@pytest.mark.parametrize("edit, needle", [
    ({"op": "remove", "section": "procedure", "index": 9}, "there is no step 9"),
    ({"op": "revise", "section": "procedure", "index": 1}, "needs the text"),
    ({"op": "add", "section": "procedure", "text": "Email marc@acme.example"}, "email address"),
    ({"op": "explode", "section": "procedure"}, "op must be"),
    ({"op": "add", "section": "tone", "text": "x"}, "section must be"),
])
def test_bad_edits_change_nothing(edit, needle):
    path = vault._VAULT / "skills" / "meeting-prep.md"
    before = path.read_bytes()
    out = patch(edit)
    assert out.startswith("Error: nothing was changed") and needle in out
    assert path.read_bytes() == before


def test_the_last_step_cannot_be_removed():
    for _ in range(3):
        patch({"op": "remove", "section": "procedure", "index": 1})
    assert "at least one step" in patch({"op": "remove", "section": "procedure", "index": 1})


def test_unattended_work_cannot_patch():
    with policy.job_scope("scheduled"):
        assert patch({"op": "remove", "section": "procedure", "index": 1}).startswith("Error: skills are changed")


def test_skills_list_counts_his_corrections():
    patch({"op": "remove", "section": "procedure", "index": 2})
    assert "meeting-prep — not opened yet. Corrected 1 time." in skills.list_text()


# ------------------------------------------------------------------ checks before sending
@pytest.fixture
def judge(monkeypatch):
    seen = {"calls": 0, "reply": json.dumps({"failed": []})}

    async def fake(prompt, max_tokens=0, images=None):
        seen["calls"] += 1
        seen["prompt"] = prompt
        return seen["reply"]
    monkeypatch.setattr(research, "_llm", fake)
    return seen


def test_a_reply_that_misses_a_check_gets_one_fix(judge):
    judge["reply"] = '{"failed": ["Exactly three points"]}'
    reruns = []

    async def rerun(msgs):
        reruns.append(msgs)
        return "Fixed reply with three points."
    out = run(skill_checks.enforce("Draft with five points.", ["meeting-prep"], [{"role": "user", "content": "prep"}], rerun))
    assert out == "Fixed reply with three points."
    assert "- Exactly three points" in judge["prompt"] and "Draft with five points." in judge["prompt"]
    [msgs] = reruns
    assert msgs[-2] == {"role": "assistant", "content": "Draft with five points."}
    assert "does not yet meet these checks" in msgs[-1]["content"] and "Exactly three points" in msgs[-1]["content"]


@pytest.mark.parametrize("reply", ['{"failed": []}', "I think it is fine", '{"failed": "x"}'])
def test_a_passing_or_unreadable_check_sends_the_draft(judge, reply):
    judge["reply"] = reply
    reran = []

    async def rerun(msgs):
        reran.append(msgs)
        return "rewritten"
    assert run(skill_checks.enforce("Draft.", ["meeting-prep"], [], rerun)) == "Draft." and reran == []


def test_a_check_that_cannot_run_never_costs_the_reply(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no model")
    monkeypatch.setattr(research, "_llm", boom)
    reran = []

    async def rerun(msgs):
        reran.append(msgs)
        return "rewritten"
    assert run(skill_checks.enforce("Draft.", ["meeting-prep"], [], rerun)) == "Draft." and reran == []


def test_a_skill_without_checks_costs_nothing(judge):
    run(skills.skill("save", name="plain", description="A plain one.", steps=["Do it"]))
    reran = []

    async def rerun(msgs):
        reran.append(msgs)
        return "rewritten"
    assert run(skill_checks.enforce("Draft.", ["plain"], [], rerun)) == "Draft." and judge["calls"] == 0
    assert reran == []


def test_a_checked_failure_not_in_the_skill_is_ignored(judge):
    judge["reply"] = '{"failed": ["Mentions the weather"]}'
    reran = []

    async def rerun(msgs):
        reran.append(msgs)
        return "rewritten"
    assert run(skill_checks.enforce("Draft.", ["meeting-prep"], [], rerun)) == "Draft." and reran == []


def test_an_empty_fix_keeps_the_draft(judge):
    judge["reply"] = '{"failed": ["Exactly three points"]}'

    async def rerun(msgs):
        return "   "
    assert run(skill_checks.enforce("Draft.", ["meeting-prep"], [], rerun)) == "Draft."


# ------------------------------------------------------------------ wiring
def test_chat_checks_discord_replies_that_followed_a_skill_and_offers_the_correction():
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert src.index("_skills.start_turn()") < src.index("skill_blocks = _skill_context(user_text, channel,")
    assert 'correction=signals.get("correction") if signals else None' in src
    assert 'system_volatile += ("\\n\\n" if system_volatile else "") + skill_blocks' in src
    # The phone speaks only the final answer, so its replies are checked too (mobile M1).
    assert ('if channel in ("discord", "mobile") and _skills.followed():\n'
            '        full_response = await _skill_checks.enforce(') in src
    assert src.index("_enforce_backed_claims(") < src.index("_skill_checks.enforce(")


def test_the_turn_gets_the_skill_he_ran_and_the_one_just_followed():
    from backend.brain import engine
    assert "Give three points" in engine._skill_context("/skill meeting-prep Marc", "discord")
    skills.start_turn()
    assert engine._skill_context("hello", "discord") == ""
    run(skills.skill("view", name="meeting-prep"))
    skills.close_turn("discord")
    skills.start_turn()
    assert "Your last reply followed the skill below" in engine._skill_context("skip the profile", "discord")
    assert engine._skill_context("skip the profile", "local") == ""


def test_a_changed_skill_claim_needs_the_patch():
    from backend.brain import engine
    reply = "Updated the skill: I'll skip the profile next time."
    assert engine._unbacked_claims(reply, set(), 0, asked_to_save=True)
    assert not engine._unbacked_claims(reply, {"skill:patch"}, 1, asked_to_save=True)
    assert not engine._unbacked_claims(reply, {"skill:save"}, 1, asked_to_save=True)   # saved over


def test_he_is_offered_patch():
    from backend.tools.registry import TOOLS
    schema = next(t for t in TOOLS if t["name"] == "skill")["input_schema"]["properties"]
    assert "patch" in schema["action"]["enum"]
    assert schema["edits"]["items"]["properties"]["op"]["enum"] == ["add", "revise", "remove"]
    assert policy.risk_of("skill", {"action": "patch"}) == policy.OWN


def test_the_per_check_answer_is_read():
    raw = ('{"checks": [{"check": "Exactly three points", "pass": true}, '
           '{"check": "Names the meeting\'s day and time", "pass": false}]}')
    assert skill_checks._parse(raw) == (True, ["Names the meeting's day and time"])
    assert skill_checks._parse('{"checks": []}') == (True, [])
    assert skill_checks._parse("no json") is None


def test_a_reply_that_does_not_do_the_task_is_not_checked():
    """5 October: a scheduling reply was judged against stock research's Checks."""
    assert skill_checks._parse('{"applies": false, "checks": []}') == (False, [])
    assert skill_checks._parse('{"applies": false, "checks": [{"check": "x", "pass": false}]}') == (False, [])
    # a missing or unreadable "applies" waves nothing through
    assert skill_checks._parse('{"checks": [{"check": "x", "pass": false}]}') == (True, ["x"])
    assert skill_checks._parse('{"applies": "no", "checks": [{"check": "x", "pass": false}]}') == (True, ["x"])


def test_edit_indexes_refer_to_the_skill_before_the_patch():
    """Live: "remove step 2" then "revise step 2" overwrote the old step 3 (the email search)."""
    patch({"op": "remove", "section": "procedure", "index": 2},
          {"op": "revise", "section": "procedure", "index": 3, "text": "Search the mail with gmail_action for the full name"})
    assert steps() == ["1. Find the event with calendar_action",
                       "2. Search the mail with gmail_action for the full name", "3. Give three points"]


def test_remove_then_add_at_the_same_place_is_a_replacement():
    patch({"op": "remove", "section": "procedure", "index": 2},
          {"op": "add", "section": "procedure", "index": 2, "text": "Check his notes on the person"})
    assert steps()[1] == "2. Check his notes on the person" and len(steps()) == 4


def test_the_correction_context_asks_for_a_pitfall_when_he_says_stop():
    run(skills.skill("view", name="meeting-prep"))
    skills.close_turn("discord")
    assert "remove that step AND add a pitfall" in skills.correction_context("discord", "stop reading the profile")
