"""The Discord exchange of 5 October 2026, 00:13-00:17 (replayed live in exchange_live_probe.py).

1. The skills-update paragraph got a one-step plan with no tool, and so a "Working on ..." DM.
2. "Cancel and re-add each one against the skill" opened stock-research, so the scheduling reply
   counted as following it and its Checks had it rewritten with a bull and a bear case and
   closing prices nothing had looked up.
3. "Good night" got "the ledger is empty ... say the five lines again" while the ledger held his
   five positions: a stored note, not the tool. The tools are the source of truth for what they
   keep; the reply is checked against the ledger itself, and consolidation stops copying it.
Found by the replay: a read backed a write claim, a description of the ledger's close fetches
read as a night-task claim, and re-adding tasks dropped some (schedule_task update).
"""
import asyncio
from datetime import datetime

import pytest

from backend.brain import engine, consolidation, personality, skill_checks, skills
from backend.tools import policy, vault


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 1. no plan without a tool
class _Message:
    def __init__(self, content):
        self.content = content


class _Client:
    def __init__(self, content):
        resp = type("Resp", (), {"usage": None, "choices": [type("Choice", (), {"message": _Message(content)})()]})()

        async def create(**kwargs):
            return resp
        self.chat = type("Chat", (), {"completions": type("C", (), {"create": staticmethod(create)})()})()


def test_a_plan_with_no_tool_step_is_no_plan():
    raw = '[{"id": 1, "label": "Acknowledge the new skill system update", "tool": null, "parallel_group": null}]'
    assert run(engine._plan_task(_Client(raw), "here's what's new: ...", tools=[])) == []


def test_a_plan_with_a_tool_step_is_kept():
    raw = ('[{"id": 1, "label": "Check the schedule", "tool": "schedule_task", "parallel_group": null},'
           ' {"id": 2, "label": "Answer", "tool": null, "parallel_group": null}]')
    plan = run(engine._plan_task(_Client(raw), "re-add the checks", tools=[]))
    assert [s["id"] for s in plan] == [1, 2]


# ------------------------------------------------------------------ 2. checks only on the task
STOCK = dict(name="stock-research", description="Research a stock, ETF or other listed asset Sam asks about.",
             when_to_use="He asks about a stock.", steps=["Read his position", "Search the news", "Bull and bear case"],
             checks=["Every figure in the reply has its source and date.",
                     "The reply has both a bull case and a bear case."])


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    token = policy.current_caller.set(policy.LIVE)
    run(skills.skill("save", **STOCK))
    skills.start_turn()
    yield
    policy.current_caller.reset(token)


def test_a_reply_that_does_not_carry_out_the_task_is_left_alone(library, monkeypatch):
    prompts, reruns = [], []

    async def judge(prompt, max_tokens=0, **kw):
        prompts.append(prompt)
        return '{"applies": false, "checks": []}'

    async def rerun(msgs):
        reruns.append(msgs)
        return "rewritten"
    monkeypatch.setattr("backend.brain.research._llm", judge)
    run(skills.skill("view", name="stock-research"))
    reply = "Cancelled and rewritten against the skill: four routines, weekdays until 30 November."
    out = run(skill_checks.enforce(reply, skills.followed(), [], rerun,
                                   request="ok cancel and re add each one against the skill this time"))
    assert out == reply and not reruns
    assert skills.followed() == [] and "followed" not in skills.turn_footer()
    # the judge saw his message and what the skill is for
    assert "cancel and re add each one" in prompts[0] and "Research a stock, ETF" in prompts[0]


def test_a_reply_that_does_the_task_is_still_checked_and_fixed(library, monkeypatch):
    reruns = []

    async def judge(prompt, max_tokens=0, **kw):
        return ('{"applies": true, "checks": [{"check": "The reply has both a bull case and a bear case.", '
                '"pass": false}]}')

    async def rerun(msgs):
        reruns.append(msgs)
        return "TTWO with a bull and a bear case."
    monkeypatch.setattr("backend.brain.research._llm", judge)
    run(skills.skill("view", name="stock-research"))
    out = run(skill_checks.enforce("TTWO looks strong.", skills.followed(), [], rerun, request="research TTWO"))
    assert out == "TTWO with a bull and a bear case." and len(reruns) == 1
    fix = reruns[0][-1]["content"]
    assert "bull case and a bear case" in fix
    assert "Add no figure, date or source you did not get from a tool" in fix
    assert skills.followed() == ["stock-research"]


def test_a_check_that_cannot_run_keeps_the_reply_and_the_line(library, monkeypatch):
    async def judge(prompt, max_tokens=0, **kw):
        raise RuntimeError("no model")
    monkeypatch.setattr("backend.brain.research._llm", judge)
    run(skills.skill("view", name="stock-research"))

    async def rerun(msgs):
        raise AssertionError("no fix pass")
    assert run(skill_checks.enforce("x", skills.followed(), [], rerun, request="research TTWO")) == "x"
    assert skills.followed() == ["stock-research"]


def test_chat_passes_his_words_to_the_check():
    import inspect
    src = inspect.getsource(engine.chat)
    assert "_skill_checks.enforce(" in src and "request=user_text" in src


# ------------------------------------------------------------------ 3. the ledger is the source of truth
FIVE = ["NVDA 1.14339682", "QNT 2.2683417", "SPCX 1.68563049", "TSLA 0.02822711", "TTWO 0.24335794"]
SAID = ("Good night, Sam. One thing, held for when you wake: the ledger is empty. Your message before "
        "last apparently never reached me in a form the tool would take, so no positions were recorded.")


def test_reproduced_live_an_empty_ledger_claim_against_a_full_ledger():
    problems = engine._unbacked_claims(SAID, set(), 0, ledger=FIVE)
    assert len(problems) == 1
    assert "holds 5 position(s) right now" in problems[0] and "SPCX 1.68563049" in problems[0]


def test_the_claim_is_checked_against_the_ledger_not_against_what_was_read():
    # a read this turn does not make a false claim true
    assert engine._unbacked_claims(SAID, {"portfolio:show"}, 1, ledger=FIVE)
    # an empty ledger makes it true, read or not
    assert engine._unbacked_claims(SAID, set(), 0, ledger=[]) == []
    # unreadable ledger: a read this turn decides
    assert engine._unbacked_claims(SAID, set(), 0, ledger=None)
    assert engine._unbacked_claims(SAID, {"portfolio:show"}, 1, ledger=None) == []


@pytest.mark.parametrize("text", [
    "the ledger is empty", "Your portfolio is still empty.", "nothing is in the ledger",
    "No positions were recorded.", "The five positions were never recorded.",
    "the holdings weren't saved", "no holdings have been recorded in the ledger"])
def test_denials_of_what_the_ledger_holds(text):
    assert engine._unbacked_claims(text, set(), 0, ledger=FIVE), text


@pytest.mark.parametrize("text", [
    "I told you the ledger was empty, and that was wrong.",
    "If the ledger is empty, send me the lines.",
    "Nothing is in the ledger for TTWO yet, so it's a candidate.",
    "The five positions are recorded.",
    "Good night. Interview at 10:30."])
def test_what_is_not_a_denial(text):
    # ("are recorded" is still a save claim for the save check; it is just not a denial)
    assert not [p for p in engine._unbacked_claims(text, set(), 0, ledger=FIVE) if "portfolio ledger" in p], text


def test_the_correction_pass_is_told_what_the_ledger_holds(monkeypatch):
    seen = []

    async def rerun(msgs):
        seen.append(msgs[-1]["content"])
        return "Good night. Your five positions are in the ledger."
    monkeypatch.setattr(engine, "_ledger_holdings", lambda: FIVE)
    monkeypatch.setattr(engine, "_turn_tools_ok", set())
    out = run(engine._enforce_backed_claims(SAID, [], rerun, "discord"))
    assert out == "Good night. Your five positions are in the ledger."
    assert "SPCX 1.68563049" in seen[0] and "out of date" in seen[0]


def test_a_correction_that_still_says_empty_gets_a_ledger_note(monkeypatch):
    async def rerun(msgs):
        return "Still: the ledger is empty."
    monkeypatch.setattr(engine, "_ledger_holdings", lambda: FIVE)
    monkeypatch.setattr(engine, "_turn_tools_ok", set())
    out = run(engine._enforce_backed_claims(SAID, [], rerun, "discord"))
    assert out.endswith(engine._LEDGER_NOTE) and engine._UNBACKED_NOTE not in out


def test_the_ledger_is_read_from_the_store():
    from backend.brain import portfolio as P
    from backend.storage import lifelog
    from backend.storage import portfolio as S
    assert engine._ledger_holdings() == []
    i = P.resolve_instrument("NVDA")
    S.record_trades([{"instrument": i, "side": "opening", "shares": "1.14339682", "price": "228.68",
                      "currency": "USD", "fees": "0", "native_price": "228.68", "native_fees": "0",
                      "traded_on": "2026-10-03", "quote": "fixture", "dedup_key": "NVDA:opening"}],
                    run_id="fixture", now=datetime(2026, 10, 3, 23, 0, tzinfo=lifelog.LOCAL_TZ).timestamp())
    assert engine._ledger_holdings() == ["NVDA 1.14339682"]


def test_he_is_told_the_tools_are_the_source_of_truth():
    core = personality._INVARIANT_CORE
    start = core.index("Some things are kept by a tool")
    rule = " ".join(core[start:start + 900].split())
    for name in ("portfolio show", "check_background_tasks", "standing_instruction list"):
        assert name in rule
    assert "Never ask him to resend something without checking first" in rule


def test_consolidation_does_not_copy_what_a_tool_keeps():
    prompt = " ".join(consolidation._PROMPT.split())
    assert "What a tool keeps is not yours to record" in prompt
    assert "never record that one is empty, that a save failed or that something is missing" in prompt
    assert "don't ask him to resend anything" in prompt
    consolidation._PROMPT.format(**{k: "" for k in ("prior_letter_block", "owner_block", "queue_block",
                                                    "threads_block", "facts_block", "listening_block",
                                                    "sleep_block", "positions_block", "predictions_block",
                                                    "people_block", "mood_register", "date_today",
                                                    "tool_record_block")})


def test_last_sleep_says_whose_sleep_it_was(monkeypatch):
    from backend.brain import memory

    async def marker():
        return {"timestamp": 0, "mood_tone": "calm", "mood_intensity": 0.1, "turn_count": 3}
    monkeypatch.setattr(memory, "latest_sleep", marker)
    text = run(memory._format_sleep_info())
    assert "You (JARVIS, not Sam) last went offline" in text


# ------------------------------------------------------------------ the claim check's own holes
def _exec(monkeypatch, name, args, result="ok"):
    async def tool(**kw):
        return result
    monkeypatch.setitem(engine.TOOL_DISPATCH, name, tool)
    monkeypatch.setattr(engine, "_turn_tools_ok", set())

    async def send(event):
        pass
    run(engine._exec_tool({"name": name, "input": args, "id": "t1"}, send))
    return set(engine._turn_tools_ok)


def test_a_read_does_not_back_a_write(monkeypatch):
    ok = _exec(monkeypatch, "schedule_task", {"action": "list"}, "Scheduled tasks: ...")
    assert ok == {"schedule_task:list"}
    assert engine._unbacked_claims("Done: four routines are set up.", ok, 1, asked_to_save=True)
    ok = _exec(monkeypatch, "standing_instruction", {"action": "list"}, "Standing rules: none.")
    assert engine._unbacked_claims("The rule is saved as a standing instruction.", ok, 1, asked_to_save=True)


def test_a_write_still_backs_its_claim(monkeypatch):
    ok = _exec(monkeypatch, "schedule_task", {"action": "add", "when": "2026-10-06 15:30",
                                              "task": "Report NVDA's price at the open."}, "Confirmed written ...")
    assert {"schedule_task", "schedule_task:add"} <= ok
    assert engine._unbacked_claims("Done: four routines are set up.", ok, 1, asked_to_save=True) == []


def test_describing_portfolio_closes_queued_for_tonight_is_not_a_night_task_claim():
    said = "And the five portfolio closes are queued for tonight at 22:20, the first marks this book has had."
    assert engine._unbacked_claims(said, set(), 0, asked_to_save=True) == []
    assert engine._unbacked_claims("I've queued the Fed research for tonight.", set(), 0, asked_to_save=True)
    assert engine._unbacked_claims("Queued it for tonight.", set(), 0, asked_to_save=True)


# ------------------------------------------------------------------ tying existing tasks to a skill
@pytest.fixture
def schedule(tmp_path, monkeypatch, library):
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    from datetime import timedelta
    day = (chronos.now_local() + timedelta(days=7)).strftime("%Y-%m-%d")
    for when, task in ((f"{day} 15:30", "Report NVDA's price at the market open."),
                       (f"{day} 23:30", "Report NVDA's official closing price."),
                       (f"{day} 15:30", "Report SPCX's price at the market open.")):
        out = run(chronos.schedule_task("add", when=when, task=task, repeat="weekdays", timezone="America/New_York"))
        assert out.startswith("Confirmed"), out
    skills.start_turn()
    return chronos


def _lines(chronos):
    return [t for t in (chronos._parse_task_line(l) for l in chronos._read_schedule()) if t]


def test_update_ties_existing_tasks_to_a_skill_and_keeps_their_times(schedule):
    before = [(t["due"], t["repeat"], t["tz"], t["task"]) for t in _lines(schedule)]
    run(skills.skill("view", name="stock-research"))       # opened first, as live
    out = run(schedule.schedule_task("update", task="NVDA", skill="stock-research"))
    assert out.startswith("Confirmed: 2 scheduled task(s) changed"), out
    after = _lines(schedule)
    assert [(t["due"], t["repeat"], t["tz"], t["task"]) for t in after] == before
    assert [t["skill"] for t in after] == ["stock-research", "stock-research", ""]
    assert skills.followed() == []          # working on the skill, not following it


def test_update_can_take_the_skill_off(schedule):
    run(schedule.schedule_task("update", task="NVDA", skill="stock-research"))
    out = run(schedule.schedule_task("update", task="stock-research", skill=""))
    assert out.startswith("Confirmed: 2"), out
    assert [t["skill"] for t in _lines(schedule)] == ["", "", ""]


def test_update_refuses_what_it_cannot_do(schedule):
    assert run(schedule.schedule_task("update", task="TSLA", skill="stock-research")).startswith("No scheduled task")
    assert run(schedule.schedule_task("update", task="NVDA", skill="no-such-skill")).startswith("Error")
    assert run(schedule.schedule_task("update", task="NV", skill="stock-research")).startswith("Error")
    assert [t["skill"] for t in _lines(schedule)] == ["", "", ""]


def test_he_is_told_to_update_rather_than_cancel_and_re_add():
    from backend.tools.registry import TOOLS
    tool = next(t for t in TOOLS if t["name"] == "schedule_task")
    assert "update" in tool["input_schema"]["properties"]["action"]["enum"]
    assert "nothing is cancelled and re-added" in " ".join(tool["description"].split())
    assert "update them rather than cancelling and re-adding" in " ".join(personality._INVARIANT_CORE.split())


def test_an_update_that_does_not_land_says_so(schedule, monkeypatch):
    monkeypatch.setattr(schedule, "_write_schedule", lambda lines: None)      # the write is lost
    out = run(schedule.schedule_task("update", task="NVDA", skill="stock-research"))
    assert out.startswith("Error: the change did not persist"), out


def test_what_he_was_about_to_say_is_not_a_claim():
    said = "Checking before you sleep: I was about to tell you the ledger was empty. It isn't."
    assert not [p for p in engine._unbacked_claims(said, set(), 0, ledger=FIVE) if "portfolio ledger" in p]


# ------------------------------------------------------------------ local times across clock changes
def test_the_paris_times_of_a_new_york_task_across_both_clock_changes():
    """Live (5 October): "shift with it on 1 November, landing an hour earlier". Paris changes on
    25 October, New York on 1 November."""
    from zoneinfo import ZoneInfo
    from backend.brain import chronos
    due = datetime(2026, 10, 6, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert chronos.local_shifts(due, "weekdays", "2026-11-30") == (
        "15:30 until Fri 23 Oct, 14:30 from Mon 26 Oct to Fri 30 Oct, 15:30 from Mon 02 Nov")


def test_no_shift_note_when_the_paris_time_never_changes():
    from zoneinfo import ZoneInfo
    from backend.brain import chronos
    due = datetime(2026, 10, 6, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert chronos.local_shifts(due, "weekdays", "2026-10-20") == ""
    assert chronos.local_shifts(due, "once", "") == ""
    paris = datetime(2026, 10, 6, 15, 30, tzinfo=ZoneInfo("Europe/Paris"))
    assert chronos._shift_note({"due": paris, "repeat": "weekdays", "until": "2026-11-30",
                                "tz": "Europe/Paris"}) == ""


def test_add_list_and_update_say_the_paris_times(schedule):
    out = run(schedule.schedule_task("list"))
    # the fixture's tasks start next week; whether they cross a change depends on today's date,
    # so check the wiring with a task that certainly does
    from zoneinfo import ZoneInfo
    t = {"due": datetime(2026, 10, 6, 9, 30, tzinfo=ZoneInfo("America/New_York")), "repeat": "weekdays",
         "until": "2026-11-30", "tz": "America/New_York"}
    assert "In local time it runs at 15:30 until Fri 23 Oct" in schedule._shift_note(t)
    import inspect
    src = inspect.getsource(schedule.schedule_task)
    assert src.count("_shift_note(") >= 3, "add, list and update"
    assert out.startswith("Scheduled tasks")


def test_a_sold_out_position_is_not_held():
    """The positions view keeps a stock he sold out of, with 0 shares: that is not a holding."""
    from backend.brain import portfolio as P
    from backend.storage import lifelog
    from backend.storage import portfolio as S
    i = P.resolve_instrument("TSLA")
    when = datetime(2026, 10, 3, 23, 0, tzinfo=lifelog.LOCAL_TZ).timestamp()
    for side, day in (("buy", "2026-09-01"), ("sell", "2026-09-02")):
        S.record_trades([{"instrument": i, "side": side, "shares": "2", "price": "400", "currency": "USD",
                          "fees": "0", "native_price": "400", "native_fees": "0", "traded_on": day,
                          "quote": "fixture", "dedup_key": f"TSLA:{side}"}], run_id="fixture", now=when)
    assert [p["symbol"] for p in S.positions()] == ["TSLA"]       # still listed, at 0 shares
    assert engine._ledger_holdings() == []


# ------------------------------------------------------------------ memory the ledger contradicts
OCT3 = ("i'm still really tired so i'm going to sleep now, good night\nGood night. I told you the five "
        "positions were recorded. They were not. Neither call succeeded, and nothing is in the ledger. Send the "
        "five lines again in one sentence when you're up.")
CLEAN = "what's on tomorrow?\nThe Diametral interview at 10:30, on Teams."


def _rec(**kw):
    return engine._RecallResult(**kw)


def test_reproduced_live_a_recalled_exchange_the_ledger_contradicts_never_reaches_him(monkeypatch):
    from backend.brain.memory import USER_ID
    from backend.storage import fact_corrections
    monkeypatch.setattr(engine, "_ledger_holdings", lambda: FIVE)
    rec = _rec(exchange=OCT3 + engine._MEMORY_SEPARATOR + CLEAN, facts="- Likes short answers")
    out, letter = engine._drop_contradicted_memory(rec, "")
    assert out.exchange == CLEAN and out.facts == "- Likes short answers"
    stored = fact_corrections.list_for_user(USER_ID)
    assert stored and "held 5 positions" in stored[0]["correction"]
    # suppressed from later recall: the same exchange no longer passes the corrections filter
    assert fact_corrections.suppresses(OCT3, stored) and not fact_corrections.suppresses(CLEAN, stored)
    # a second recall adds no second correction
    engine._drop_contradicted_memory(rec, "")
    assert len(fact_corrections.list_for_user(USER_ID)) == len(stored)


def test_memory_saying_the_ledger_is_empty_is_kept_when_it_is_empty_or_unreadable(monkeypatch):
    rec = _rec(exchange=OCT3)
    for holdings in ([], None):
        monkeypatch.setattr(engine, "_ledger_holdings", lambda h=holdings: h)
        assert engine._drop_contradicted_memory(rec, "")[0].exchange == OCT3


def test_facts_passages_and_letters_the_ledger_contradicts_are_left_out(monkeypatch):
    monkeypatch.setattr(engine, "_ledger_holdings", lambda: FIVE)
    rec = _rec(facts="- Likes short answers\n- The portfolio ledger is empty", vault_passage="Nothing is in the ledger.")
    out, letter = engine._drop_contradicted_memory(rec, "A quiet night. The ledger is still empty; ask him to resend.")
    assert out.facts == "- Likes short answers" and out.vault_passage == "" and letter == ""


def test_memory_without_a_denial_is_untouched_and_the_ledger_not_read(monkeypatch):
    def boom():
        raise AssertionError("the ledger is read only when memory says it is empty")
    monkeypatch.setattr(engine, "_ledger_holdings", boom)
    rec = _rec(exchange=CLEAN, facts="- I told you the ledger was empty, which was wrong")
    assert engine._drop_contradicted_memory(rec, "letter") == (rec, "letter")


def test_chat_drops_contradicted_memory_before_building_the_prompt():
    import inspect
    src = inspect.getsource(engine.chat)
    drop = src.index("_drop_contradicted_memory(rec, vault_letter)")
    assert drop < src.index("_build_prompt_via_context(")


@pytest.mark.parametrize("text", [
    "Two lines for the morning: the five positions are still unrecorded.",
    "The five lines still aren't in the ledger.",
    "Send the five lines again in one sentence.",
    "The five lines when you're up -- one sentence, and they're in."])
def test_the_phrasings_that_slipped_through_are_denials(text):
    assert [p for p in engine._unbacked_claims(text, set(), 0, ledger=FIVE) if "portfolio ledger" in p], text


def test_a_recalled_exchange_is_framed_as_words_not_state():
    assert personality.MEMORY_NOT_STATE in personality._EXCHANGE_FRAME
    assert "true only in their tools" in personality.MEMORY_NOT_STATE


def test_queue_lines_and_portrait_sentences_the_ledger_contradicts_are_left_out(monkeypatch):
    monkeypatch.setattr(engine, "_ledger_holdings", lambda: FIVE)
    queue = ("[open] 2026-10-01 -- Diametral interview prep\n"
             "[open] 2026-10-05 -- The ledger is empty: ask him to send the five lines again")
    assert engine._strip_contradicted(queue, by_line=True) == "[open] 2026-10-01 -- Diametral interview prep"
    portrait = "## Situation\nPreparing for Diametral. Nothing is in the ledger yet. Prefers short answers."
    out = engine._strip_contradicted(portrait)
    assert "ledger" not in out and "Preparing for Diametral." in out and "Prefers short answers." in out
    monkeypatch.setattr(engine, "_ledger_holdings", lambda: [])
    assert engine._strip_contradicted(queue, by_line=True) == queue


def test_chat_strips_the_queue_and_portrait_before_using_them():
    import inspect
    src = inspect.getsource(engine.chat)
    strip = src.index("_strip_contradicted(queue_raw, by_line=True)")
    assert strip < src.index("intent_context = _format_intent_context(queue_raw)")
    assert strip < src.index("_build_prompt_via_context(")


def test_what_the_ledger_was_before_this_turn_recorded_it_is_not_a_denial():
    """Replayed (5 October): the turn recording his five positions said the ledger "was empty"
    before them, and the check called it a false claim."""
    said = "The ledger was empty, so these went in as opening positions. No positions were recorded before today."
    assert not [p for p in engine._unbacked_claims(said, {"portfolio", "portfolio:record_trade"}, 1, ledger=FIVE)
                if "portfolio ledger" in p]
    assert not [p for p in engine._unbacked_claims("The ledger was empty this morning.", set(), 0, ledger=FIVE)
                if "portfolio ledger" in p]
    # the present tense still counts on a turn that recorded nothing
    assert engine._unbacked_claims("The ledger is empty.", set(), 0, ledger=FIVE)
