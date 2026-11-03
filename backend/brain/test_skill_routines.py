"""Skills, slice 2 (4 October 2026): routines, and skills in unattended work.

A routine is a scheduled task tied to a skill: each run follows the skill as it is then.
Background jobs, night tasks and routines can open skills, never change them.
"""
import asyncio
import datetime
import inspect

import pytest

from backend.brain import background, chronos, skills
from backend.brain.chronos import _TZ
from backend.brain.test_skills import CREATIVE_JUNE, STOCK_JUNE
from backend.storage import skills as store
from backend.tools import policy, vault

TASK = "Research TTWO and report what matters this week, with dates and sources."


def paris(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    for name, text in (("creative-write-on-demand", CREATIVE_JUNE),
                       ("structured-financial-research", STOCK_JUNE)):
        (tmp_path / "skills" / f"{name}.md").write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    now = {"t": paris(2026, 10, 4, 18, 0)}          # a Sunday evening
    monkeypatch.setattr(chronos, "now_local", lambda: now["t"])
    started = []

    async def start(label, brief, skill="", occurrence=""):
        started.append((label, brief))
        return ""
    monkeypatch.setattr(background, "start_scheduled", start)
    return {"dir": tmp_path, "now": now, "started": started}


def tasks():
    return [t for t in (chronos._parse_task_line(l) for l in chronos._read_schedule()) if t]


def add(**kw):
    args = dict(action="add", when="2026-10-05 09:00", task=TASK, repeat="weekly",
                skill="structured-financial-research")
    args.update(kw)
    return run(chronos.schedule_task(**args))


# ------------------------------------------------------------------ the line
def test_a_routine_line_round_trips_and_old_lines_still_read():
    due = paris(2026, 10, 5, 9, 0)
    line = chronos._format_task_line(due, "weekly", "", "Europe/Paris", "Check | it all", "stock-research")
    assert "| skill=stock-research | Check | it all" in line
    t = chronos._parse_task_line(line)
    assert (t["skill"], t["task"]) == ("stock-research", "Check | it all")
    old = chronos._parse_task_line("[task] 2026-10-05 09:00 | daily | until= | tz=Europe/Paris | Check it")
    assert (old["skill"], old["task"]) == ("", "Check it")


def test_a_skill_field_with_no_task_is_not_a_task():
    assert chronos._parse_task_line("[task] 2026-10-05 09:00 | daily | until= | tz=Europe/Paris | skill=x |") is None


# ------------------------------------------------------------------ scheduling
def test_scheduling_a_routine_names_the_skill_and_what_it_means(world):
    out = add()
    assert out.startswith("Confirmed") and "following skill structured-financial-research" in out
    assert "as it is at that moment" in out
    assert tasks()[0]["skill"] == "structured-financial-research"


def test_a_routine_for_a_skill_that_is_not_in_use_is_refused(world):
    out = add(skill="stock-researc")
    assert out.startswith("Error") and "nothing was scheduled" in out
    assert "structured-financial-research" in out or "creative" in out
    assert tasks() == []
    (world["dir"] / "skills" / "creative-write-on-demand.md").write_text(
        CREATIVE_JUNE.replace("status: active", "status: retired"), encoding="utf-8")
    assert add(skill="creative-write-on-demand").startswith("Error")


def test_cancelling_by_the_skill_name(world):
    add()
    out = run(chronos.schedule_task("cancel", task="structured-financial-research"))
    assert out.startswith("Cancelled 1") and tasks() == []


# ------------------------------------------------------------------ running
def test_each_run_follows_the_skill_as_it_is_then(world):
    """Improving the skill improves every later run: it is loaded at run time."""
    add()
    path = world["dir"] / "skills" / "structured-financial-research.md"
    path.write_text(STOCK_JUNE.replace("Identify 4 parallel research vectors.",
                                       "Read his thesis first, then search."), encoding="utf-8")
    world["now"]["t"] = paris(2026, 10, 5, 9, 1)
    run(chronos._fire_due_tasks(world["now"]["t"]))
    [(label, brief)] = world["started"]
    assert "Follow your skill 'structured-financial-research'" in brief
    assert "Read his thesis first" in brief and "Identify 4 parallel" not in brief
    assert store.use_stats()["structured-financial-research"]["uses"] == 1
    # the next weekly run keeps its skill
    [nxt] = tasks()
    assert nxt["skill"] == "structured-financial-research" and nxt["due"] == paris(2026, 10, 12, 9, 0)


def test_a_routine_whose_skill_was_retired_still_runs_and_says_so(world):
    add()
    (world["dir"] / "skills" / "structured-financial-research.md").write_text(
        STOCK_JUNE.replace("status: active", "status: retired"), encoding="utf-8")
    world["now"]["t"] = paris(2026, 10, 5, 9, 1)
    run(chronos._fire_due_tasks(world["now"]["t"]))
    [(_, brief)] = world["started"]
    assert "no longer in use" in brief and TASK in brief
    assert store.use_stats() == {}


def test_a_run_that_could_not_start_records_no_use(world, monkeypatch):
    add()

    async def busy(label, brief, skill="", occurrence=""):
        return "Error: 3 background tasks are already running"
    monkeypatch.setattr(background, "start_scheduled", busy)
    world["now"]["t"] = paris(2026, 10, 5, 9, 1)
    run(chronos._fire_due_tasks(world["now"]["t"]))
    assert store.use_stats() == {}


# ------------------------------------------------------------------ where he sees it
def test_skills_list_shows_when_a_routine_runs(world):
    add()
    text = skills.list_text()
    assert "• structured-financial-research — not opened yet. Runs weekly, next Mon 05 Oct 09:00." in text


def test_activity_names_the_skill_a_timed_check_follows(world):
    add()
    from backend.brain import activity
    assert "following skill structured-financial-research" in activity.activity_text()


# ------------------------------------------------------------------ unattended work
def test_a_job_is_offered_only_open_and_list():
    schema = next(t for t in background._bg_tools() if t["name"] == "skill")
    assert schema["input_schema"]["properties"]["action"]["enum"] == ["list", "view"]
    assert "skill" in background._ALLOWED_TOOLS


@pytest.mark.parametrize("caller", ["background", "scheduled", "night", "unattended"])
def test_only_the_live_conversation_changes_a_skill(world, caller):
    with policy.job_scope(caller):
        for action in ("save", "retire", "export"):
            out = run(skills.skill(action, name="creative-write-on-demand", description="x",
                                   steps=["y"]))
            assert out.startswith("Error: skills are changed or sent only in the conversation")
        assert run(skills.skill("view", name="creative-write-on-demand")).startswith("Skill '")
    assert skills.load("creative-write-on-demand").description.startswith("When the user invites")


def test_a_job_gets_the_list_in_its_instructions_and_the_names_beside_its_task(world):
    system, task = background._with_skills("SYSTEM", "Do the task.")
    assert system.startswith("SYSTEM\n\n" + skills.CATALOG_FRAME)
    assert task.startswith("Do the task.") and "Skills you keep:" in task


def test_without_skills_a_job_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    assert background._with_skills("SYSTEM", "Do it.") == ("SYSTEM", "Do it.")


def test_the_worker_loop_uses_it():
    for loop in (background._run_compat_loop,):
        assert "system, task_text = _with_skills(system, task_text)" in inspect.getsource(loop)


def test_the_compat_worker_really_sends_the_list(world, monkeypatch):
    """Driven through the real loop, provider faked: the request carries the list."""
    sent = {}

    class Msg:
        content, tool_calls = "done", None

    class Resp:
        choices = [type("C", (), {"message": Msg()})()]
        usage = None

    async def create(**kw):
        sent.update(kw)
        return Resp()
    fake = type("Client", (), {"chat": type("Chat", (), {"completions": type("Comp", (), {
        "create": staticmethod(create)})()})()})()
    from backend.brain import engine
    monkeypatch.setattr(engine, "_get_compat_client", lambda: fake)
    monkeypatch.setattr(background._llm_calls, "record_response", lambda *a, **k: None)
    assert run(background._run_compat_loop("Research TTWO.")).text == "done"
    system, user = sent["messages"][0]["content"], sent["messages"][1]["content"]
    assert "- structured-financial-research:" in system and "Skills you keep:" in user
    names = [t["function"]["name"] for t in sent["tools"]]
    skill_tool = next(t for t in sent["tools"] if t["function"]["name"] == "skill")
    assert "skill" in names and skill_tool["function"]["parameters"]["properties"]["action"]["enum"] == ["list", "view"]


# ------------------------------------------------------------------ he is told, and checked
def test_a_routine_claim_needs_a_schedule_that_worked():
    from backend.brain import engine
    reply = "Done: the routine is set for Monday mornings."
    assert engine._unbacked_claims(reply, set(), 0, asked_to_save=True)
    assert not engine._unbacked_claims(reply, {"schedule_task"}, 1, asked_to_save=True)


def test_he_is_told_about_routines():
    from backend.brain import personality
    from backend.tools.registry import TOOLS
    core = personality._INVARIANT_CORE
    assert "pass skill=<its name>: that makes it a routine" in core
    assert "can open your skills but not change them" in core
    schema = next(t for t in TOOLS if t["name"] == "schedule_task")["input_schema"]["properties"]
    assert "skill" in schema


# ------------------------------------------------------------------ found live: jobs ran out of steps
def test_every_jobs_last_step_answers_instead_of_hitting_the_cap(monkeypatch):
    """Live: a stock-research routine used all 8 steps and its whole message to Sam was
    "I'll start by reading his position..." plus the cap note. Now the last step can only answer."""
    from backend.brain import engine, router
    from backend.brain.router import OPENROUTER
    from backend.brain.test_handoff import _Completions
    monkeypatch.setattr(background, "_provider", lambda: OPENROUTER)
    client = _Completions([("I'll start by reading his position.", "web_browse")]
                          * (background._MAX_BG_ITERATIONS - 1) + [("TTWO closed at $238.40.", None)])
    monkeypatch.setattr(engine, "_get_compat_client", lambda: client)

    async def bg_exec(name, tool_input, invalid=None):
        return "page text"
    monkeypatch.setattr(background, "_bg_exec", bg_exec)
    out = run(background._run_compat_loop("routine brief"))
    assert (out.status, out.text) == (background.COMPLETE, "TTWO closed at $238.40.")
    assert background._BG_CAP_NOTE not in out.text
    assert "tools" not in client.calls[-1] and "tools" in client.calls[0]
    assert client.calls[-1]["messages"][-1]["content"] == background._DEEP_FINAL_NUDGE
    assert background._MAX_BG_ITERATIONS >= 12


def test_a_job_reads_the_portfolio_but_never_changes_it(monkeypatch):
    from backend.storage import db
    reached = []

    async def gateway_execute(name, tool_input, **kw):
        reached.append((name, tool_input))
        return type("R", (), {"result": "positions"})()
    monkeypatch.setattr(background._gateway, "execute", gateway_execute)
    out = run(background._bg_exec("portfolio", {"action": "record_trade", "trades": []}))
    assert out.startswith("Error: from a background job, portfolio only does show, history, record")
    assert reached == []
    receipt = db.connect().execute("SELECT tool, outcome FROM operations ORDER BY started_at DESC LIMIT 1").fetchone()
    assert tuple(receipt) == ("portfolio", "failed")
    assert run(background._bg_exec("portfolio", {"action": "show", "symbol": "TTWO"})) == "positions"
    assert run(background._bg_exec("skill", {"action": "save", "name": "x"})).startswith("Error")
    schema = next(t for t in background._bg_tools() if t["name"] == "portfolio")
    assert schema["input_schema"]["properties"]["action"]["enum"] == ["show", "history", "record"]


def test_a_routine_brief_names_the_tools_a_run_does_not_have(world, monkeypatch):
    path = world["dir"] / "skills" / "structured-financial-research.md"
    path.write_text(STOCK_JUNE.replace("requires_tools: []", "requires_tools: [web_browse, week_plan]"),
                    encoding="utf-8")
    t = chronos._parse_task_line(chronos._format_task_line(
        paris(2026, 10, 5, 9, 0), "weekly", "", "Europe/Paris", TASK, "structured-financial-research"))
    brief = chronos._task_brief(t, paris(2026, 10, 5, 9, 0))
    assert "does not have (week_plan)" in brief and "web_browse," not in brief.split("does not have")[1][:30]
