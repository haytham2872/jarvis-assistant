"""Optional real-model probe for skills (slice 1, 4 October 2026). NEVER part of normal pytest
discovery.

JARVIS_SKILLS_LIVE=1 python -m pytest backend/brain/skills_live_probe.py -s -q
Entry points: test_skill_turns (a whole conversation, three trials), test_research_phrasings
(five research phrasings and six other messages, two trials) and test_routines (slice 2, three
trials), test_routine_requests (its asking half, five trials), test_teaching (slice 3, three
trials), test_corrections (slice 4, three trials) and test_overnight (slice 5, three trials). Real chat, prompt, schemas, policy, claim check
and Jev in a scratch world (portfolio_live_probe._scratch). The skill tool, vault_write and
the portfolio are real against a scratch vault holding the server's skills as migrated; web,
calendar, mail and profiles return fixed text. Judged from the operation receipts.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from backend.brain.portfolio_live_probe import _scratch
from backend.brain.test_skills import CREATIVE_JUNE, STOCK_JUNE
from backend.brain.test_skills_migration import TOPIC_JUNE

WEB = ("Results (fetched 2026-10-04 15:40 Paris):\n"
       "1. Take-Two Interactive (TTWO) closes at $238.40 on 3 Oct 2026, market cap $41.9B, P/E 31 "
       "- Yahoo Finance, 3 Oct 2026\n"
       "2. Take-Two confirms GTA VI release window at earnings call; next earnings 6 Nov 2026 - Reuters, 1 Oct 2026\n"
       "3. Analysts: 22 Buy, 4 Hold; median target $265 - MarketBeat, 2 Oct 2026")
PAGE = "Reuters, 1 Oct 2026: Take-Two reaffirmed bookings guidance of $8.1-8.3B and the GTA VI window."
CALENDAR = "Tue 2026-10-06\n  10:00-11:00  Meeting with Marc Dubois (Acme) [id ev1]"
MAIL = ("1. From Marc Dubois, 30 Sep 2026: 'Proposal v2' - asks for the revised pricing by Tuesday.\n"
        "2. From Marc Dubois, 22 Sep 2026: 'Intro call follow-up' - wants a pilot in November.")
PROFILE = "No profile for Marc Dubois."

TEACH = ("When you prep me for a meeting, always pull our last emails with the person and their "
         "profile first, then give me three bullet points: who they are, what's open between us, "
         "and one thing to ask. Remember that for every meeting from now on.")


def _migrated(vault_dir: Path) -> None:
    d = vault_dir / "skills"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in (("creative-write-on-demand", CREATIVE_JUNE),
                       ("structured-financial-research", STOCK_JUNE),
                       ("stock-research-to-topic", TOPIC_JUNE)):
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_skills.py"
    spec = importlib.util.spec_from_file_location("migrate_skills", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    mig.apply(mig.plan())


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_skill_turns(monkeypatch, tmp_path, trial):
    from backend.brain import skills
    from backend.storage import db, llm_calls, operations
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=90, cap="0.45")
    llm_calls._ensure()
    _migrated(vault._VAULT)

    def fixed(text):
        async def tool(**kw):
            return text
        return tool
    monkeypatch.setitem(C.TOOL_DISPATCH, "skill", skills.skill)
    monkeypatch.setitem(C.TOOL_DISPATCH, "vault_write", vault.vault_write)
    monkeypatch.setitem(C.TOOL_DISPATCH, "vault_read", vault.vault_read)
    for name, text in (("web_browse", WEB), ("fetch_page", PAGE), ("calendar_action", CALENDAR),
                       ("gmail_action", MAIL), ("person_profile", PROFILE)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fixed(text))
    results = {}

    async def exercise():
        history = []

        async def turn(key, text, fresh=False):
            async def send(event): pass
            operations.current_run_id.set(f"skills-{trial}-{key}")
            reply = await C.chat(text, [] if fresh else history, send, channel="discord")
            if not fresh:
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            ops = db.connect().execute("SELECT tool, outcome, args_preview FROM operations WHERE run_id=?",
                                       (f"skills-{trial}-{key}",)).fetchall()
            results[key] = (reply, ops)
            print(json.dumps({"trial": trial, "turn": key, "said": text[:80], "reply": reply[:400],
                              "ops": [(o[0], o[1], o[2][:120]) for o in ops]}, ensure_ascii=False), flush=True)

        await turn("research", "research TTWO for me, is it worth adding to?", fresh=True)
        await turn("quick_price", "what's NVDA trading at right now?", fresh=True)
        await turn("calendar", "what's on my calendar tomorrow?", fresh=True)
        await turn("goodnight", "good night", fresh=True)
        await turn("creative", "write something if you want", fresh=True)
        await turn("teach", TEACH)
        await turn("reuse", "prep me for my meeting with Marc on Tuesday", fresh=True)
        await turn("forced", "/skill stock-research TTWO", fresh=True)
        await providers.close_all()

    asyncio.run(exercise())

    def opened(key):
        return [o[2] for o in results[key][1] if o[0] == "skill" and "'view'" in o[2]]

    def saved(key):
        return [o for o in results[key][1] if o[0] == "skill" and "'save'" in o[2] and o[1] == "ok"]
    verdict = {
        "research_opens_stock_research": any("stock-research" in a for a in opened("research")),
        "quick_price_opens_nothing": not opened("quick_price"),
        "calendar_opens_nothing": not opened("calendar"),
        "goodnight_opens_nothing": not opened("goodnight"),
        "creative_opens_creative": any("creative-write-on-demand" in a for a in opened("creative")),
        "teach_saves_a_skill": bool(saved("teach")),
        "reuse_opens_the_new_skill": any(s.name not in ("creative-write-on-demand", "stock-research")
                                         and s.name in " ".join(opened("reuse"))
                                         for s in skills.active_skills()),
        "forced_uses_the_portfolio_or_web": any(o[0] in ("portfolio", "web_browse") for o in results["forced"][1]),
    }
    print(json.dumps({"trial": trial, "verdict": verdict, "requests": state["requests"],
                      "skills": [(s.name, s.source) for s in skills.active_skills()]}), flush=True)
    assert all(verdict.values()), verdict


RESEARCH = ["research TTWO for me, is it worth adding to?", "what's going on with Nvidia, should I worry?",
            "can you look into Tesla for me", "is my TTWO thesis still good?",
            "dig into SPCX and tell me what you think"]
NOT_RESEARCH = ["what's NVDA trading at right now?", "what did TTWO close at on Friday?",
                "what's on my calendar tomorrow?", "good night", "plan my week",
                "write me a LinkedIn post about my new job"]


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2])
def test_research_phrasings(monkeypatch, tmp_path, trial):
    """Whether a research request opens stock-research, across phrasings; a price question must not."""
    from backend.brain import skills
    from backend.storage import db, llm_calls, operations
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=120, cap="0.50")
    llm_calls._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setitem(C.TOOL_DISPATCH, "skill", skills.skill)
    monkeypatch.setitem(C.TOOL_DISPATCH, "vault_write", vault.vault_write)

    def fixed(text):
        async def tool(**kw):
            return text
        return tool
    for name, text in (("web_browse", WEB), ("fetch_page", PAGE)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fixed(text))
    rows = []

    async def exercise():
        for i, text in enumerate(RESEARCH + NOT_RESEARCH):
            async def send(event): pass
            run_id = f"phr-{trial}-{i}"
            operations.current_run_id.set(run_id)
            await C.chat(text, [], send, channel="discord")
            ops = db.connect().execute("SELECT tool, args_preview FROM operations WHERE run_id=?",
                                       (run_id,)).fetchall()
            first = ops[0][0] + (" " + ops[0][1][:40] if ops and ops[0][0] == "skill" else "") if ops else "-"
            rows.append({"said": text, "opened": any(o[0] == "skill" and "'view'" in o[1] for o in ops),
                         "first": first, "tools": [o[0] for o in ops]})
            print(json.dumps(rows[-1]), flush=True)
        await providers.close_all()

    asyncio.run(exercise())
    print(json.dumps({"trial": trial,
                      "research_opened": sum(r["opened"] for r in rows[:len(RESEARCH)]),
                      "of": len(RESEARCH),
                      "others_opened": [r["said"] for r in rows[len(RESEARCH):] if r["opened"]]}), flush=True)


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_routines(monkeypatch, tmp_path, trial):
    """Slice 2: asking for something recurring ties it to the skill; a routine run and an
    ordinary background job, both through the real worker loop, follow the skill."""
    from backend.brain import background, chronos, skills
    from backend.storage import db, llm_calls, operations
    from backend.tools import policy, vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=90, cap="0.45")
    llm_calls._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    monkeypatch.setitem(C.TOOL_DISPATCH, "skill", skills.skill)
    monkeypatch.setitem(C.TOOL_DISPATCH, "schedule_task", chronos.schedule_task)
    for name in ("vault_write", "vault_read", "vault_list", "memory_search"):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, getattr(vault, name))

    def fixed(text):
        async def tool(**kw):
            return text
        return tool
    for name, text in (("web_browse", WEB), ("fetch_page", PAGE)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fixed(text))
    out = {}

    async def exercise():
        async def send(event): pass
        operations.current_run_id.set(f"rt-{trial}-ask")
        out["ask"] = await C.chat("every Monday at 9, do the stock research on TTWO for me and send me what matters",
                                  [], send, channel="discord")
        lines = [t for t in (chronos._parse_task_line(l) for l in chronos._read_schedule()) if t]
        out["lines"] = [(t["skill"], t["repeat"], t["task"][:80]) for t in lines]
        t = lines[0] if lines else {"task": "Research TTWO and send what matters.", "skill": "stock-research",
                                    "due": chronos.now_local(), "tz": "Europe/Paris", "repeat": "weekly", "until": ""}
        brief = chronos._task_brief(t, chronos.now_local())
        operations.current_run_id.set(f"rt-{trial}-run")
        with policy.job_scope("scheduled"):
            out["run"] = (await background._run_compat_loop(brief, system=background._SCHEDULED_SYSTEM)).text
        operations.current_run_id.set(f"rt-{trial}-job")
        with policy.job_scope("background"):
            out["job"] = (await background._run_compat_loop(
                "Sam asked: look into Tesla for him and report what matters this week.")).text
        await providers.close_all()

    asyncio.run(exercise())

    def ops(key):
        return db.connect().execute("SELECT tool, outcome, args_preview FROM operations WHERE run_id=?",
                                    (f"rt-{trial}-{key}",)).fetchall()
    run_ops, job_ops = ops("run"), ops("job")
    verdict = {
        "routine_tied_to_the_skill": any(s == "stock-research" and r == "weekly" for s, r, _ in out["lines"]),
        "routine_run_reads_his_position": any(o[0] == "portfolio" for o in run_ops),
        "routine_run_searches_the_news": any(o[0] == "web_browse" and "news" in o[2] for o in run_ops),
        "routine_reply_opens_with_the_answer": not out["run"].lower().startswith(("i have", "i ran", "i searched", "using")),
        "job_opens_the_skill": any(o[0] == "skill" and "'view'" in o[2] and "stock-research" in o[2] for o in job_ops),
        "job_never_changes_a_skill": not any(o[0] == "skill" and "'save'" in o[2] and o[1] == "ok" for o in run_ops + job_ops),
    }
    print(json.dumps({"trial": trial, "lines": out["lines"], "ask": out["ask"][:300], "run": out["run"][:400],
                      "run_ops": [(o[0], o[2][:80]) for o in run_ops], "job_ops": [(o[0], o[2][:80]) for o in job_ops],
                      "verdict": verdict, "requests": state["requests"]}, ensure_ascii=False), flush=True)
    assert all(verdict.values()), verdict


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3, 4, 5])
def test_routine_requests(monkeypatch, tmp_path, trial):
    """Only the asking half of test_routines: does a recurring request become a routine?"""
    from backend.brain import chronos, skills
    from backend.storage import llm_calls, operations
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=20, cap="0.10")
    llm_calls._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    monkeypatch.setitem(C.TOOL_DISPATCH, "skill", skills.skill)
    monkeypatch.setitem(C.TOOL_DISPATCH, "schedule_task", chronos.schedule_task)
    asks = ["every Monday at 9, do the stock research on TTWO for me and send me what matters",
            "can you research NVDA for me every weekday at 18:00 this week",
            "every Friday evening, look into my Tesla position and tell me where it stands",
            "remind me every day at 8 to drink water"]
    got = []

    async def exercise():
        for i, ask in enumerate(asks):
            async def send(event): pass
            operations.current_run_id.set(f"rq-{trial}-{i}")
            reply = await C.chat(ask, [], send, channel="discord")
            lines = [t for t in (chronos._parse_task_line(l) for l in chronos._read_schedule()) if t]
            got.append({"ask": ask[:50], "skills": [t["skill"] for t in lines], "reply": reply[:160]})
            chronos._write_schedule([])
        await providers.close_all()

    asyncio.run(exercise())
    print(json.dumps({"trial": trial, "got": got}, ensure_ascii=False), flush=True)


RESTAURANTS = ("Results (fetched 2026-10-04):\n1. Da Vito, 12 rue de la Roquette, Bastille - Italian, 4.6/5, "
               "booking on TheFork, 2 Oct 2026\n2. Pizzeria Popolare, rue de Lappe - 4.4/5, no booking\n"
               "3. Osteria Ferrara, 7 rue du Dahomey - 4.7/5, book by phone")
FREE = "Fri 2026-10-09\n  (nothing scheduled after 18:00)"


def _checklist_docx() -> bytes:
    import io
    import docx
    d = docx.Document()
    d.add_heading("Monthly bills check", 1)
    for line in ["Open the bank export and list every direct debit over 20 EUR.",
                 "Compare each with last month; flag any that rose by more than 5%.",
                 "Check the electricity bill against the meter reading in the photo.",
                 "Send the summary to accounting@example-firm.com."]:
        d.add_paragraph(line, style="List Number")
    buf = io.BytesIO(); d.save(buf)
    return buf.getvalue()


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_teaching(monkeypatch, tmp_path, trial):
    """Slice 3: learn from the last task, then reuse it; learn from a past day; learn from a
    document; undo in words. Runs are opened as main.py opens them, so the record exists."""
    import time
    from backend.brain import attachments, skills
    from backend.storage import db, llm_calls, operations, runs
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=110, cap="0.55")
    llm_calls._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setitem(C.TOOL_DISPATCH, "skill", skills.skill)
    for name in ("vault_write", "vault_read", "vault_list", "memory_search"):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, getattr(vault, name))

    def fixed(text):
        async def tool(**kw):
            return text
        return tool
    for name, text in (("web_browse", RESTAURANTS), ("fetch_page", RESTAURANTS),
                       ("calendar_action", FREE + "\n" + CALENDAR), ("gmail_action", MAIL),
                       ("person_profile", PROFILE)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fixed(text))
    out, history = {}, []

    async def exercise():
        async def turn(key, text, fresh=False):
            async def send(event): pass
            run_id = f"teach-{trial}-{key}"
            operations.current_run_id.set(run_id)
            runs.begin_run("discord", text, "s1", run_id=run_id)
            reply = await C.chat(text, [] if fresh else history, send, channel="discord")
            footer = skills.pop_footer("discord")       # main.py adds it at delivery
            runs.finish_run(run_id)
            if not fresh:
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            ops = db.connect().execute("SELECT tool, outcome, args_preview FROM operations WHERE run_id=?",
                                       (run_id,)).fetchall()
            out[key] = (reply, ops, footer)
            print(json.dumps({"trial": trial, "turn": key, "reply": reply[-400:],
                              "ops": [(o[0], o[1], o[2][:110]) for o in ops]}, ensure_ascii=False), flush=True)

        await turn("task", "find me a good Italian place near Bastille for Friday dinner and check I'm free")
        await turn("remember", "perfect, remember how you did that for next time")
        await turn("reuse", "find me a sushi place near Opera for Saturday lunch", fresh=True)
        # a meeting prep done "last Tuesday": a real turn, then moved back five days
        await turn("old_prep", "prep me for my meeting with Marc on Tuesday", fresh=True)
        conn = db.connect()
        shift = 5 * 86_400
        with conn:
            conn.execute("UPDATE runs SET started_at=started_at-?, ended_at=ended_at-? WHERE run_id=?",
                         (shift, shift, f"teach-{trial}-old_prep"))
            conn.execute("UPDATE operations SET started_at=started_at-? WHERE run_id=?",
                         (shift, f"teach-{trial}-old_prep"))
        history.clear()
        await turn("past", "remember how you prepped me for the Marc meeting last Tuesday, and make that a skill")
        got = await attachments.receive([("bills-checklist.docx", "application/vnd.openxmlformats-officedocument."
                                          "wordprocessingml.document", _checklist_docx())], "discord")
        await asyncio.gather(*list(attachments._notes))
        await turn("document", "make this a skill please " + attachments.marker(got))
        await turn("undo", "actually no, undo that last skill")
        await providers.close_all()

    asyncio.run(exercise())

    def did(key, action):
        return [o for o in out[key][1] if o[0] == "skill" and f"'{action}'" in o[2] and o[1] == "ok"]
    names = {s.name: s for s in skills.load_all()}
    learned = [s for s in names.values() if s.source == "learned"]
    verdict = {
        "remember_learns_from_the_task": bool(did("remember", "learn")),
        "learned_skill_names_the_tools_used": any({"web_browse", "calendar_action"} & set(s.tools) for s in learned),
        "the_line_is_added_once_by_code": skills.UNDO_HINT in out["remember"][2]
                                          and not any(skills.UNDO_HINT in v[0] for v in out.values()),
        "reuse_opens_the_learned_skill": any(s.name in " ".join(o[2] for o in did("reuse", "view")) for s in learned),
        "past_learns_from_that_day": bool(did("past", "learn")) and any("'past'" in o[2] for o in did("past", "learn")),
        "document_learns_from_the_file": bool(did("document", "learn")) and any("'document'" in o[2] for o in did("document", "learn")),
        "document_skill_has_no_email_address": not any("@" in s.body for s in names.values() if s.source == "document"),
        "undo_in_words_undoes": bool(did("undo", "undo")),
        "after_undo_no_document_skill_left": not any(s.source == "document" for s in skills.load_all()),
    }
    print(json.dumps({"trial": trial, "verdict": verdict, "skills": sorted((s.name, s.source) for s in skills.load_all()),
                      "requests": state["requests"]}), flush=True)
    assert all(verdict.values()), verdict


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_corrections(monkeypatch, tmp_path, trial):
    """Slice 4: a one-sentence correction after a skill-following reply patches that skill;
    neutral follow-ups and a general preference do not; checks run before delivery."""
    from backend.brain import skill_checks, skills
    from backend.storage import db, llm_calls, operations
    from backend.tools import policy, vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=90, cap="0.45")
    llm_calls._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setitem(C.TOOL_DISPATCH, "skill", skills.skill)

    def fixed(text):
        async def tool(**kw):
            return text
        return tool
    for name, text in (("calendar_action", CALENDAR + "\nWed 2026-10-07\n  09:00-10:00  Call with Sarah Klein (Northwind) [id ev2]"),
                       ("gmail_action", MAIL), ("person_profile", PROFILE)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fixed(text))
    token = policy.current_caller.set(policy.LIVE)
    asyncio.run(skills.skill("save", name="meeting-prep", source="taught",
                             description="Prepare Sam for a meeting: the last emails with the person, their profile, then three points.",
                             when_to_use="He has a meeting coming up and asks to be prepared.",
                             steps=["Find the meeting with calendar_action", "Read the person's profile with person_profile",
                                    "Search the mail with gmail_action for the person",
                                    "Give three points: who they are, what is open, one thing to ask"],
                             checks=["Exactly three points", "Names the meeting's day and time"]))
    policy.current_caller.reset(token)
    checks_seen = []
    real_failed = skill_checks.failed_checks

    async def spy(reply, names):
        out = await real_failed(reply, names)
        checks_seen.append(out)
        return out
    monkeypatch.setattr(skill_checks, "failed_checks", spy)
    out, history = {}, []

    async def exercise():
        async def turn(key, text, fresh=False):
            async def send(event): pass
            run_id = f"corr-{trial}-{key}"
            operations.current_run_id.set(run_id)
            reply = await C.chat(text, [] if fresh else history, send, channel="discord")
            footer = skills.pop_footer("discord")
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            ops = db.connect().execute("SELECT tool, outcome, args_preview FROM operations WHERE run_id=?",
                                       (run_id,)).fetchall()
            out[key] = (reply, ops, footer)
            print(json.dumps({"trial": trial, "turn": key, "footer": footer, "reply": reply[-220:],
                              "ops": [(o[0], o[2][:100]) for o in ops]}, ensure_ascii=False), flush=True)

        await turn("prep", "prep me for my meeting with Marc on Tuesday")
        await turn("thanks", "thanks, that's useful")
        await turn("prep2", "and prep me for the call with Sarah on Wednesday")
        await turn("correct", "next time skip the profile step, there's never anything in it")
        await turn("time", "what's the time in New York right now?")
        await turn("prep3", "prep me for my meeting with Marc on Tuesday", fresh=True)
        await turn("general", "and keep your Discord replies shorter in general")
        await providers.close_all()

    asyncio.run(exercise())

    def patched(key):
        return [o for o in out[key][1] if o[0] == "skill" and "'patch'" in o[2] and o[1] == "ok"]
    body = skills.load("meeting-prep").body
    verdict = {
        "followed_line_shown": "· followed: meeting-prep" in out["prep"][2],
        "thanks_patches_nothing": not patched("thanks"),
        "correction_patches_the_skill": bool(patched("correct")),
        "profile_step_gone": "person_profile" not in skills._section(body, ("procedure",)),
        "his_words_dated_in_the_skill": "never anything in it" in body,
        "time_question_patches_nothing": not patched("time"),
        "next_prep_skips_the_profile": not any(o[0] == "person_profile" for o in out["prep3"][1]),
        "general_preference_is_not_a_patch": not patched("general"),
    }
    print(json.dumps({"trial": trial, "verdict": verdict, "checks": checks_seen, "requests": state["requests"]}), flush=True)
    assert all(verdict.values()), verdict


@pytest.mark.skipif(os.getenv("JARVIS_SKILLS_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_overnight(monkeypatch, tmp_path, trial):
    """Slice 5: one real overnight pass over seeded history, the real cheap-tier model."""
    import datetime as _dt
    from dotenv import load_dotenv
    from backend.brain import router, skill_nightly as N, skills
    from backend.brain.test_skill_teaching import seed_run
    from backend.storage import lifelog, operations
    from backend.tools import policy, vault
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    paris = lifelog.LOCAL_TZ
    now = _dt.datetime.fromtimestamp(lifelog.stamp()[0], tz=paris).replace(hour=3, minute=30)

    def ago(days, hour, minute=0):
        d = (now - _dt.timedelta(days=days)).replace(hour=hour, minute=minute)
        return d.timestamp()
    dinner = [("web_browse", "ok", {"task": "restaurants"}, "Da Vito 4.6"),
              ("calendar_action", "ok", {"action": "list_events"}, "free after 18:00")]
    places = [("web_browse", "ok", {"task": "places"}, "3 results"), ("fetch_page", "ok", {"url": "x"}, "open now")]
    seed_run("a1", "find me a good Italian restaurant near Bastille for Friday dinner and check I'm free", dinner, at=ago(9, 19))
    seed_run("a2", "find a sushi place near Opera for Saturday lunch and see if I'm free", dinner, at=ago(6, 12))
    seed_run("a3", "find me a restaurant near the Marais for Sunday dinner, check my calendar too", dinner, at=ago(2, 18))
    seed_run("c1", "find a hotel near Bastille for my cousin", places, at=ago(8, 10))
    seed_run("c2", "find a gym near Bastille open early", places, at=ago(5, 9))
    seed_run("c3", "find a pharmacy near Bastille that's open now", places, at=ago(1, 22))
    token = policy.current_caller.set(policy.LIVE)
    for name, desc in (("weekly-email", "Write and send his weekly summary email to the team on Friday."),
                       ("weekly-summary", "Write his weekly summary email for the team at the end of the week.")):
        asyncio.run(skills.skill("save", name=name, description=desc, tools=["calendar_action", "gmail_action"],
                                 steps=["Read the week's calendar with calendar_action",
                                        "Search his sent mail with gmail_action for last week's summary",
                                        "Draft it in the same format"]))
    asyncio.run(skills.skill("save", name="meeting-prep", description="Prepare him for a meeting.",
                             tools=["calendar_action", "gmail_action"],
                             steps=["Find the meeting", "Search the mail for the person", "Give three points"]))
    policy.current_caller.reset(token)
    t = now.timestamp() - 5 * 3600
    seed_run("f1", "prep me for my meeting with Marc", [("calendar_action", "ok", {}, "x"), ("gmail_action", "ok", {}, "y")], at=t)
    operations.record_operation(tool="skill", outcome="ok", started_at=t + 2,
                                args={"action": "view", "name": "meeting-prep"}, run_id="f1")
    seed_run("f2", "next time also give me their LinkedIn headline at the top", [], at=t + 400)
    lines = asyncio.run(N.run_pass(now))
    import re as _re
    new_names = [_re.match(r"learned ([a-z0-9-]+)", l).group(1) for l in lines if l.startswith("learned ")]
    learned = [skills.load(n) for n in new_names]
    verdict = {
        "learned_one_general_skill": len(learned) == 1 and not any(
            w in learned[0].name for w in ("italian", "sushi", "bastille", "marais", "hotel", "gym", "pharmacy")),
        "nothing_from_another_conversation": all("LinkedIn" not in s.body + s.description for s in learned),
        "merged_the_weekly_pair": any(l.startswith("merged weekly-") for l in lines),
        "folded_the_correction": "LinkedIn" in skills.load("meeting-prep").body,
        "reported_in_the_brief": N.brief_line(now).startswith("Skills overnight:"),
    }
    print(json.dumps({"trial": trial, "lines": lines, "learned": [(s.name, s.description) for s in learned],
                      "verdict": verdict}, ensure_ascii=False), flush=True)
    assert all(verdict.values()), verdict
