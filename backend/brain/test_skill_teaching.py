"""Skills, slice 3 (4 October 2026): four ways to teach, undo, and the line that says so.

Learning reads the real records (runs, operation receipts, exchanges) from the per-test
SQLite database conftest provides; the one model call is faked and its prompt inspected.
"""
import asyncio
import datetime
import inspect
import json

import pytest

from backend.brain import research, skill_learning, skills
from backend.storage import db, exchanges, lifelog, operations
from backend.storage import runs as runs_store
from backend.storage import skills as store
from backend.tools import policy, vault

DRAFT = {
    "name": "meeting-prep",
    "description": "Prepare Sam for a meeting: the last emails with the person, their profile, three points.",
    "when_to_use": "He has a meeting coming up and asks to be prepared.",
    "steps": ["Find the event with calendar_action", "Search the mail with gmail_action for the person",
              "Write it to marc@acme.example then", "Give three points: who, what is open, one question"],
    "pitfalls": ["A profile search for a first name only finds nothing: use the full name"],
    "checks": ["Exactly three points"],
    "tools": ["calendar_action", "gmail_action", "teleport"],
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _live(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    token = policy.current_caller.set(policy.LIVE)
    run_token = operations.current_run_id.set(None)
    skills.start_turn()
    yield
    operations.current_run_id.reset(run_token)
    policy.current_caller.reset(token)


@pytest.fixture
def model(monkeypatch):
    seen = {}

    async def fake(prompt, max_tokens=0, images=None):
        seen["prompt"] = prompt
        return seen.get("reply", "Here it is:\n" + json.dumps(DRAFT))
    monkeypatch.setattr(research, "_llm", fake)
    return seen


def seed_run(run_id, user_text, ops, *, at, channel="discord", assistant="done"):
    conn = runs_store._ensure()
    with conn:
        conn.execute("INSERT INTO runs (run_id, session_id, channel, state, started_at, ended_at, "
                     "user_preview) VALUES (?, 's1', ?, 'completed', ?, ?, ?)",
                     (run_id, channel, at, at + 30, user_text[:200]))
    for i, (tool, outcome, args, result) in enumerate(ops):
        operations.record_operation(tool=tool, outcome=outcome, started_at=at + 1 + i,
                                    args=args, result=result, run_id=run_id)
    exchanges.record_exchange(exchange_id=f"x-{run_id}", session_id="s1", channel=channel,
                              user_text=user_text, assistant_text=assistant, significance=0.5,
                              high=False, timestamp=at + 20)


NOW = lifelog.stamp()[0]
PREP = [("calendar_action", "ok", {"action": "list_events", "date": "2026-10-06"}, "10:00 Meeting with Marc Dubois"),
        ("person_profile", "failed", {"action": "read", "name": "Marc"}, "No profile for Marc"),
        ("gmail_action", "ok", {"action": "search_emails", "query": "Marc Dubois"}, "Proposal v2")]


# ------------------------------------------------------------------ the line at the end
def test_a_save_puts_a_line_with_the_undo_at_the_end_of_the_turn():
    assert skills.turn_footer() == ""
    run(skills.skill("save", name="weekly-email", description="Write his weekly summary email.",
                     steps=["Read the week's calendar", "Draft the summary"]))
    assert skills.turn_footer() == "\n\n· Saved skill weekly-email (2 steps) · /skill undo takes it back"
    skills.start_turn()
    assert skills.turn_footer() == ""


def test_the_line_is_handed_over_per_channel_and_delivered_once():
    run(skills.skill("save", name="weekly-email", description="Write his weekly summary email.",
                     steps=["Read the week's calendar", "Draft the summary"]))
    skills.close_turn("discord")
    assert "Saved skill weekly-email" in skills.pop_footer("discord")
    assert skills.pop_footer("discord") == ""


def test_the_line_is_added_at_delivery_never_into_history_or_memory():
    """Live: with the line in history, JARVIS started writing it himself (it appeared twice)."""
    from backend import main
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert "_skills.start_turn()" in src and "_skills.close_turn(channel)" in src
    assert "turn_footer" not in src and "pop_footer" not in src
    # G3: the line is added where every Discord reply is shaped, after the turn ran: the
    # direct reply and an admitted message's own reply both go through _shape_discord.
    pipe = inspect.getsource(main.run_pipeline_from_discord)
    assert pipe.index("_execute_turn(") < pipe.index("_shape_discord(")
    assert 'pop_footer("discord")' in inspect.getsource(main._shape_discord)
    assert "_shape_discord(" in inspect.getsource(main._answer_admitted)


def test_follow_ups_come_from_the_same_conversation_within_two_hours(model):
    """Live: a task learned "from last Tuesday" took turns five days later as follow-ups."""
    seed_run("r1", "prep me for my meeting with Marc", PREP, at=NOW - 6 * 3600)
    seed_run("r-late", "find me an Italian place near Bastille", [], at=NOW - 3 * 3600)
    run(skills.skill("learn", learn_from="last_task"))
    assert "Italian place" not in model["prompt"]


# ------------------------------------------------------------------ undo
def save(name="weekly-email", steps=("Read the week's calendar", "Draft the summary")):
    return run(skills.skill("save", name=name, description="Write his weekly summary email.",
                            steps=list(steps)))


def test_undo_of_a_new_skill_removes_it_and_keeps_its_text():
    save()
    assert run(skills.skill("undo", name="weekly-email")).startswith("Undone: the skill 'weekly-email' was new")
    assert skills.load("weekly-email") is None
    assert "Draft the summary" in store.revisions("weekly-email")[0]["content"]


def test_undo_puts_back_the_exact_earlier_version_and_undo_again_goes_further():
    save()
    path = vault._VAULT / "skills" / "weekly-email.md"
    first = path.read_bytes()
    save(steps=["Read the calendar", "Read the sent mail", "Draft it"])
    assert run(skills.skill("undo", name="weekly-email")).startswith("Undone: 'weekly-email' is back")
    assert path.read_bytes() == first
    assert run(skills.skill("undo", name="weekly-email")).startswith("Undone: the skill 'weekly-email' was new")
    assert not path.exists()
    assert run(skills.skill("undo", name="weekly-email")) == "Nothing to undo for 'weekly-email'."


def test_undo_without_a_name_takes_back_the_last_change_but_never_a_migration():
    store.add_revision("creative-write-on-demand", "migrate", "old June text", actor="system")
    assert "nothing to undo" in skills.handle_undo_command("")
    save()
    out = skills.handle_undo_command("")
    assert "weekly-email" in out and skills.load("weekly-email") is None


def test_the_undo_command_is_wired_and_not_mistaken_for_running_a_skill():
    from backend import main
    from backend.brain import discord_bridge
    assert skills.forced("/skill undo weekly-email") == ("", "")
    from backend.brain import commands
    assert 'sub == "undo"' in inspect.getsource(commands)
    assert "skill_undo_handler  = skills.handle_undo_command" in inspect.getsource(main)


# ------------------------------------------------------------------ learning from the last task
def test_learning_from_the_last_task_reads_the_real_record(model):
    seed_run("r1", "prep me for my meeting with Marc on Tuesday", PREP, at=NOW - 600)
    seed_run("r2", "next time use his full name for the profile", [], at=NOW - 300)
    operations.current_run_id.set("r-now")
    out = run(skills.skill("learn", learn_from="last_task"))
    assert out.startswith("Confirmed: skill 'meeting-prep'") and "Learned from the task on" in out
    p = model["prompt"]
    assert "Sam asked: prep me for my meeting with Marc on Tuesday" in p
    assert p.index("1. calendar_action (ok)") < p.index("2. person_profile (failed)") < p.index("3. gmail_action (ok)")
    assert "next time use his full name for the profile" in p           # his correction
    s = skills.load("meeting-prep")
    assert s.source == "learned" and s.tools == ["calendar_action", "gmail_action"]   # teleport dropped
    assert "marc@acme.example" not in s.body                             # code strips addresses
    assert "Find the event with calendar_action" in out                 # steps returned to tell him


def test_the_task_being_learned_from_is_never_the_current_one(model):
    seed_run("r1", "prep me for my meeting with Marc", PREP, at=NOW - 600)
    operations.current_run_id.set("r1")
    assert run(skills.skill("learn", learn_from="last_task")).startswith("Error: I found no task")


def test_nothing_to_learn_from_says_so_and_saves_nothing(model):
    assert run(skills.skill("learn", learn_from="last_task")).startswith("Error: I found no task")
    assert skills.load_all() == [] and "prompt" not in model


def test_a_name_already_in_use_is_not_overwritten_unless_he_names_it(model):
    seed_run("r1", "prep me for my meeting with Marc", PREP, at=NOW - 600)
    save(name="meeting-prep", steps=["His own way"])
    out = run(skills.skill("learn", learn_from="last_task"))
    assert out.startswith("Error: you already have a skill called 'meeting-prep'")
    assert "His own way" in skills.load("meeting-prep").body
    out = run(skills.skill("learn", learn_from="last_task", name="meeting-prep"))
    assert out.startswith("Confirmed") and "replaced the earlier version, which is kept" in out


def test_a_draft_that_is_not_json_saves_nothing(model):
    seed_run("r1", "prep me for my meeting with Marc", PREP, at=NOW - 600)
    model["reply"] = "I think the procedure is: read the calendar."
    assert run(skills.skill("learn", learn_from="last_task")).startswith("Error: the skill could not be drafted")
    assert skills.load_all() == []


# ------------------------------------------------------------------ learning from a past day
def test_learning_from_a_past_day_finds_the_task_by_his_words(model):
    tuesday = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=lifelog.LOCAL_TZ).timestamp()
    seed_run("r-other", "what's NVDA trading at", [("web_browse", "ok", {"task": "NVDA"}, "$190")], at=tuesday)
    seed_run("r-prep", "prep me for my call with Marc Dubois", PREP, at=tuesday + 3600)
    out = run(skills.skill("learn", learn_from="past", day="2026-09-29", about="the Marc meeting prep"))
    assert out.startswith("Confirmed") and "Tue 29 Sep" in out
    assert "prep me for my call with Marc Dubois" in model["prompt"]
    assert "NVDA" not in model["prompt"]


def test_a_past_task_that_matches_nothing_is_not_guessed(model):
    tuesday = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=lifelog.LOCAL_TZ).timestamp()
    seed_run("r1", "prep me for my call with Marc", PREP, at=tuesday)
    out = run(skills.skill("learn", learn_from="past", day="2026-09-29", about="gym schedule"))
    assert out.startswith("Error: I found no task") and "prompt" not in model


# ------------------------------------------------------------------ learning from a document
def test_a_document_becomes_a_skill_marked_as_someone_elses_text(model):
    folder = vault._VAULT / "attachments"
    folder.mkdir()
    (folder / "2026-10-04-onboarding-checklist.md").write_text(
        "Document: onboarding-checklist.docx\n\n## Steps\n1. Book the welcome lunch", encoding="utf-8")
    out = run(skills.skill("learn", learn_from="document", about="onboarding-checklist.docx"))
    assert out.startswith("Confirmed") and "glance over the steps" in out
    assert "written by someone else" in model["prompt"] and "Book the welcome lunch" in model["prompt"]
    assert skills.load("meeting-prep").source == "document"


def test_a_document_that_is_not_there(model):
    assert run(skills.skill("learn", learn_from="document", about="nope.pdf")).startswith("Error: no document")


# ------------------------------------------------------------------ who may teach, and the checks
@pytest.mark.parametrize("action", ["learn", "undo"])
def test_unattended_work_cannot_teach_or_undo(action):
    with policy.job_scope("background"):
        assert run(skills.skill(action, learn_from="last_task")).startswith("Error: skills are changed")


def test_learning_and_undo_back_their_claims():
    from backend.brain import engine
    saved = "Done, I saved that as a skill: meeting-prep."
    assert engine._unbacked_claims(saved, {"skill", "skill:view"}, 1, asked_to_save=True)
    assert not engine._unbacked_claims(saved, {"skill", "skill:learn"}, 1, asked_to_save=True)
    undone = "Undone: meeting-prep is back to its earlier version."
    assert engine._unbacked_claims(undone, set(), 0, asked_to_save=True)
    assert not engine._unbacked_claims(undone, {"skill:undo"}, 1, asked_to_save=True)


def test_he_is_told_and_the_tool_offers_it():
    from backend.brain import personality
    from backend.tools.registry import TOOLS
    core = personality._INVARIANT_CORE
    assert 'skill(action="learn")' in core and "/skill undo" in core
    schema = next(t for t in TOOLS if t["name"] == "skill")["input_schema"]["properties"]
    assert {"learn", "undo"} <= set(schema["action"]["enum"])
    assert schema["learn_from"]["enum"] == ["last_task", "past", "document"]
    assert policy.risk_of("skill", {"action": "learn"}) == policy.OWN


# ------------------------------------------------------------------ the nudge
@pytest.mark.parametrize("text", [
    "perfect, remember how you did that for next time",
    "remember how you prepped me for the Marc meeting last Tuesday",
    "do it like we did last week for Sarah",
    "make this a skill please",
    "turn this checklist into a skill",
    "save that as a skill",
])
def test_asking_to_keep_a_procedure_brings_the_learn_nudge(text):
    from backend.brain import personality
    assert personality.is_skill_request(text)
    _, volatile = personality.build_system_prompt(text)
    assert personality._SKILL_LEARN_FRAME in volatile


@pytest.mark.parametrize("text", [
    "do you remember what I said about Marc?", "what skills do you have?",
    "I did it like you said", "remember to call mum", "research TTWO for me",
])
def test_ordinary_messages_do_not(text):
    from backend.brain import personality
    assert not personality.is_skill_request(text)


def test_a_turn_that_only_opened_a_skill_is_not_a_task_to_learn_from(model):
    """"What skills do you have?" leaves a skill receipt; learning from it would copy a list."""
    seed_run("r1", "prep me for my meeting with Marc", PREP, at=NOW - 900)
    seed_run("r2", "what skills do you have?", [("skill", "ok", {"action": "list"}, "Skills (1)")], at=NOW - 600)
    run(skills.skill("learn", learn_from="last_task"))
    assert "prep me for my meeting with Marc" in model["prompt"]
    assert "skill (ok)" not in model["prompt"]


def test_follow_ups_are_his_next_messages_in_that_session_never_this_one(model):
    seed_run("r1", "prep me for my meeting with Marc", PREP, at=NOW - 900)
    conn = runs_store._ensure()
    with conn:
        conn.execute("INSERT INTO runs (run_id, session_id, channel, state, started_at, ended_at, user_preview) "
                     "VALUES ('r-other', 's2', 'discord', 'completed', ?, ?, 'a message from another session')",
                     (NOW - 800, NOW - 790))
        conn.execute("INSERT INTO runs (run_id, session_id, channel, state, started_at, ended_at, user_preview) "
                     "VALUES ('r-now', 's1', 'discord', 'running', ?, ?, 'remember how you did that')",
                     (NOW - 10, NOW - 10))
    operations.current_run_id.set("r-now")
    run(skills.skill("learn", learn_from="last_task"))
    assert "another session" not in model["prompt"] and "remember how you did that" not in model["prompt"]


def test_follow_ups_stop_at_the_next_task(model):
    """Live (slice 5): "next time also give me their LinkedIn headline", said after a later
    meeting prep, became part of a learned "find a place" skill."""
    seed_run("r1", "find a pharmacy near Bastille that's open now", PREP, at=NOW - 3000)
    seed_run("r2", "the one on rue de Lyon is better, prefer those", [], at=NOW - 2800)
    seed_run("r3", "prep me for my meeting with Marc", PREP, at=NOW - 2600)
    seed_run("r4", "next time also give me their LinkedIn headline", [], at=NOW - 2400)
    run_dict = skill_learning._runs("discord", NOW - 3001, NOW - 2999)[0]
    after = [r["run_id"] for r in skill_learning._follow_ups("discord", run_dict)]
    assert after == ["r2"]
