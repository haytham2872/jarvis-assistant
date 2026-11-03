"""A reply may only claim a save that a successful tool call this turn backs.

Live incident (2026-09-23): asked to make a Spotify habit a standing rule, JARVIS
replied "Confirmed -- the rule is saved as a standing instruction" with no tool call;
/rules was empty; pressed, he invented a tool error ("No standing instructions database
found") that exists nowhere in the code. The store worked -- cold add on a fresh database
saves and /rules lists it. These tests pin the check that now runs before the reply
is delivered.
"""
import asyncio
import inspect

from backend.brain import engine, personality

LIVE_CLAIM = ("Confirmed — the rule is saved as a standing instruction.\n\nThe wording: "
              "before playing anything on Spotify, first verify the device is active.")
LIVE_FABRICATION = ("Attempted, and here's the verbatim return. The tool came back: "
                    "Error: No standing instructions database found. Nothing was written.")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ detection
def test_the_live_claim_without_a_tool_call_is_caught():
    assert engine._unbacked_claims(LIVE_CLAIM, set(), 0)


def test_the_same_claim_is_fine_when_the_save_succeeded():
    assert engine._unbacked_claims(LIVE_CLAIM, {"standing_instruction"}, 1) == []


def test_a_called_but_failed_save_does_not_back_the_claim():
    """Only a SUCCESSFUL call counts: a rejected call saved nothing either."""
    assert engine._unbacked_claims(LIVE_CLAIM, {"spotify_action"}, 3)


def test_an_invented_tool_result_is_caught_when_no_tool_ran():
    assert engine._unbacked_claims(LIVE_FABRICATION, set(), 0)
    assert not engine._unbacked_claims(LIVE_FABRICATION, {"standing_instruction"}, 1)


def test_other_saves_are_checked_too():
    assert engine._unbacked_claims("Done -- the watch is set for NVDA below $180.", set(), 0)
    assert engine._unbacked_claims("Your reminder is set for 10am tomorrow.", set(), 0)
    assert engine._unbacked_claims("The night task is queued for tonight.", set(), 0)
    assert not engine._unbacked_claims("The watch is set.", {"watch_action"}, 1)


def test_negations_and_ordinary_talk_are_not_claims():
    for reply in ("I haven't saved it as a rule yet -- want me to?",
                  "That rule was never saved, which is the bug.",
                  "You can list your rules with /rules.",
                  "Rule r2 says to check the device first, and I skipped it.",
                  "I'll look into why the play command lied."):
        assert engine._unbacked_claims(reply, set(), 0) == [], reply


# ------------------------------------------------------------------ enforcement
def _reset(ok=(), calls=0):
    engine._reset_turn_stats()
    engine._turn_tools_ok.update(ok)
    engine._turn_tool_calls = calls


def test_a_backed_reply_is_delivered_untouched():
    _reset(ok={"standing_instruction"}, calls=1)
    async def rerun(msgs):
        raise AssertionError("no correction pass for a backed claim")
    assert run(engine._enforce_backed_claims(LIVE_CLAIM, [], rerun, "discord")) == LIVE_CLAIM


def test_an_unbacked_claim_gets_one_correction_pass_whose_reply_replaces_it():
    _reset()
    seen = {}
    async def rerun(msgs):
        seen["msgs"] = msgs
        engine._turn_tools_ok.add("standing_instruction")   # the pass actually saved it
        return "Saved now -- rule r1. Earlier I said so without doing it."
    out = run(engine._enforce_backed_claims(LIVE_CLAIM, [{"role": "user", "content": "q"}],
                                            rerun, "discord"))
    assert out == "Saved now -- rule r1. Earlier I said so without doing it."
    assert seen["msgs"][-2] == {"role": "assistant", "content": LIVE_CLAIM}
    assert "no standing_instruction call succeeded" in seen["msgs"][-1]["content"]


def test_a_correction_that_still_claims_falsely_carries_a_plain_correction():
    _reset()
    async def rerun(msgs):
        return "Confirmed, the rule is saved."
    out = run(engine._enforce_backed_claims(LIVE_CLAIM, [], rerun, "discord"))
    assert out.endswith(engine._UNBACKED_NOTE)


def test_a_failed_correction_pass_never_delivers_the_bare_false_claim():
    _reset()
    async def rerun(msgs):
        raise RuntimeError("provider down")
    out = run(engine._enforce_backed_claims(LIVE_CLAIM, [], rerun, "discord"))
    assert out == LIVE_CLAIM + engine._UNBACKED_NOTE


def test_exec_tool_records_only_successes(monkeypatch):
    from backend.tools.gateway import ToolOutcome
    async def fake_execute(name, args, **kw):
        return ToolOutcome(name, "Error: nope", "failed", "x") if name == "bad" else \
               ToolOutcome(name, "fine", "ok")
    monkeypatch.setattr(engine._gateway, "execute", fake_execute)
    _reset()
    async def send(_): pass
    run(engine._exec_tool({"name": "bad", "input": {}}, send))
    run(engine._exec_tool({"name": "good", "input": {}}, send))
    assert engine._turn_tools_ok == {"good"}
    assert engine._turn_tools_called == {"bad", "good"}


# ------------------------------------------------------------------ wiring
def test_chat_enforces_claims_before_the_reply_is_stored():
    src = inspect.getsource(engine.chat)
    enforce = src.index("_enforce_backed_claims(full_response, messages, _rerun, channel,")
    # whether he asked to save something decides if a read of the schedule can back a description
    call = src[enforce:enforce + 260]
    assert "is_persistence_request(user_text)" in call and '_pause.is_paused("work")' in call
    assert enforce < src.index("await _ingest_mem(model, user_text, full_response)")
    assert enforce < src.index("_record_thread_task(user_text, full_response,")
    assert 'user_text="",' in src[src.index("def _rerun"):enforce]      # no second planner run


def test_rule_phrasings_trigger_the_correction_nudge():
    for text in ("Make this a standing rule: check the device first",
                 "Save it as a rule so you stop doing this", "make it a rule"):
        assert personality.is_correction_request(text), text


# ------------------------------------------------------------------ profile declines
LIVE_DECLINE = ("Noted. No profile. I've recorded the decline so I won't ask again.  That's "
                "the whole response, really.")


def test_an_unrecorded_profile_decline_is_caught():
    """Live (2026-09-26, keyword fallback): no person_profile call was made."""
    assert engine._unbacked_claims(LIVE_DECLINE, set(), 0)
    assert engine._unbacked_claims("Understood -- I won't offer a profile for her again.", set(), 0)
    assert not engine._unbacked_claims(LIVE_DECLINE, {"person_profile"}, 1)


def test_ordinary_wont_ask_again_and_negated_declines_are_not_claims():
    for reply in ("Fair enough, I won't ask again about the meeting.",     # no profile
                  "I haven't recorded the decline yet -- should I?",
                  "Noted — the decline on the lease was his call, not yours."):
        assert engine._unbacked_claims(reply, set(), 0) == [], reply


def test_a_profile_claim_phrased_as_is_in_his_profile_is_caught():
    """Live probe (2026-09-26): this draft was not flagged before."""
    live = "Done, Sofia is in his profile now under Relationships."
    assert engine._unbacked_claims(live, set(), 0)
    assert engine._unbacked_claims("It's already in Karim's profile.", set(), 0)
    assert not engine._unbacked_claims(live, {"person_profile"}, 1)
    assert not engine._unbacked_claims("That isn't in his profile yet -- want me to add it?", set(), 0)


def test_a_promise_or_offer_is_not_a_claim():
    for reply in ("I said I'd put it on his profile -- that was premature.",
                  "Tell me the name and I'll add it to his profile.",
                  "Want me to put that in Karim's profile?"):
        assert engine._unbacked_claims(reply, set(), 0) == [], reply
    assert engine._unbacked_claims("I put it in Karim's profile.", set(), 0)


# ------------------------------------------------------------------ describing what exists
# Live (2026-09-30, P2): /activity's text says "night task queued for the quiet hours"; the
# model repeated it in answer to "what are you working on?", the check called that a false
# claim, and the correction pass told Sam "I've now queued it properly" for a task that
# was already there. These are the replies the model actually wrote.
DESCRIBED = (
    "The night task is queued for the quiet hours -- research on how the ECB rate path feeds "
    "mortgage rates.",
    "The NVDA watch is live, and your reminder is set for 17:00.",
    "A timed check is scheduled for weekdays at 09:30, and one forecast is logged as a forecast "
    "due Saturday.",
)


def test_a_reply_describing_the_schedule_after_reading_it_is_not_a_claim():
    for reply in DESCRIBED:
        assert engine._unbacked_claims(reply, {"check_background_tasks"}, 1) == [], reply


def test_the_same_words_without_a_read_are_still_claims():
    for reply in DESCRIBED[:2]:
        assert engine._unbacked_claims(reply, set(), 0), reply
    assert engine._unbacked_claims(DESCRIBED[0], {"web_browse"}, 1)


def test_when_he_asked_to_save_something_a_read_does_not_back_the_claim():
    """Reading the schedule proves what was there, not that the new task was queued."""
    for reply in DESCRIBED[:2]:
        assert engine._unbacked_claims(reply, {"check_background_tasks"}, 1, asked_to_save=True), reply
    assert engine._unbacked_claims(DESCRIBED[0], {"check_background_tasks", "schedule_night_task"},
                                   2, asked_to_save=True) == []


def test_a_read_backs_only_the_things_the_view_lists():
    """The activity view does not list standing rules, profiles or sent mail."""
    for reply in ("Confirmed -- the rule is saved as a standing instruction.",
                  "I put it in Karim's profile.", "I emailed the summary to Sarah."):
        assert engine._unbacked_claims(reply, {"check_background_tasks"}, 1), reply
    assert set(engine._STATE_READS["check_background_tasks"]) == {
        "schedule_night_task", "schedule_reminder", "schedule_task", "watch_action", "forecast"}


def test_the_enforcement_leaves_a_described_schedule_alone_and_asks_for_no_second_pass():
    _reset(ok={"check_background_tasks"}, calls=1)
    async def rerun(msgs):
        raise AssertionError("a description of the schedule needs no correction pass")
    assert run(engine._enforce_backed_claims(DESCRIBED[0], [], rerun, "discord")) == DESCRIBED[0]
    # but a request to save, answered only by a read, still gets the correction
    _reset(ok={"check_background_tasks"}, calls=1)
    called = []
    async def rerun2(msgs):
        called.append(msgs)
        return "I have not queued it yet."
    out = run(engine._enforce_backed_claims(DESCRIBED[0], [], rerun2, "discord", asked_to_save=True))
    assert called and out == "I have not queued it yet."


# ------------------------------------------------------------------ talking about the pause
# Live (2026-09-30, P2): asked "are you still doing your own research and checks?" while paused,
# 2 of 5 answers said "scheduled and night tasks are on hold" and were called claims that
# something had just been scheduled -- and one was rewritten into "Correction before anything
# else...". Only while a pause is actually in force, a sentence that says something is held is
# describing the pause.
PAUSE_TALK = (
    "You paused me at 10:30, so research, watches and scheduled and night tasks are all on hold.",
    "No. Nothing of my own is running: no research, no watches, no scheduled tasks, no night tasks.",
    "The reminder is set for 9:00, but you have me paused, so it waits until you /resume.",
)


def test_sentences_about_what_is_held_are_not_claims_while_paused():
    for reply in PAUSE_TALK:
        assert engine._unbacked_claims(reply, set(), 0, paused=True) == [], reply


def test_the_same_sentences_are_still_claims_when_no_pause_is_in_force():
    assert engine._unbacked_claims(PAUSE_TALK[0], set(), 0)
    assert engine._unbacked_claims(PAUSE_TALK[2], set(), 0)


def test_a_pause_does_not_excuse_a_claim_that_says_nothing_about_being_held():
    """Paused or not, 'all six checks are scheduled' with no tool call is still false."""
    for reply in ("Done. All six checks are scheduled.", "Your reminder is set for 9:00.",
                  "The night task is queued for tonight."):
        assert engine._unbacked_claims(reply, set(), 0, paused=True), reply


def test_only_the_sentence_that_mentions_the_hold_is_excused():
    reply = "Your reminder is set for 9:00. Separately, watches are on hold while you are paused."
    assert engine._unbacked_claims(reply, set(), 0, paused=True), "the first sentence is still a claim"


def test_a_thing_said_to_be_absent_is_not_a_claim_but_a_polite_no_is_not_an_absence():
    """'no scheduled tasks' (with or without a pause); 'No problem -- the reminder is set' is a claim."""
    for reply in ("Nothing is running: no research, no watches, no scheduled tasks, no night tasks.",
                  "I have zero scheduled checks right now.", "It runs without any scheduled tasks."):
        assert engine._unbacked_claims(reply, set(), 0) == [], reply
    assert engine._unbacked_claims("No problem -- the reminder is set for 9.", set(), 0)
    assert engine._unbacked_claims("No worries, the night task is queued for tonight.", set(), 0)


def test_the_sentence_around_a_match():
    import re
    text = "First one. The reminder is set for nine, but paused! Then more."
    m = re.search(r"reminder is set", text)
    assert engine._sentence_around(text, m) == " The reminder is set for nine, but paused"
    line = "a line\nreminder is set here\nnext"
    assert engine._sentence_around(line, re.search(r"reminder is set", line)) == "reminder is set here"


def test_where_a_note_came_from_is_not_a_claim_it_was_sent_to_him():
    """'the note to Sarah, sent from a background job' -- live, from the activity view's email."""
    assert engine._unbacked_claims(
        "Waiting on you: the note to Sarah Klein, sent from a background job, is held for approval.",
        set(), 0) == []
    assert engine._unbacked_claims("I sent the note to you as a file.", set(), 0)
    assert engine._unbacked_claims("The note is sent.", set(), 0)


def test_the_recheck_after_a_correction_also_knows_about_the_pause():
    _reset()
    said = "You paused me at 10:30, so scheduled and night tasks are on hold."
    async def rerun(msgs):
        return said
    out = run(engine._enforce_backed_claims("Done. All six checks are scheduled.", [], rerun,
                                            "discord", paused=True))
    assert out == said, "the corrected reply talks about the pause, and is not called a claim again"


def test_the_recheck_after_a_correction_still_knows_he_asked_to_save():
    _reset(ok={"check_background_tasks"}, calls=1)
    async def rerun(msgs):
        return DESCRIBED[0]                       # still describes the schedule instead of saving
    out = run(engine._enforce_backed_claims(DESCRIBED[1], [], rerun, "discord", asked_to_save=True))
    assert out.startswith(DESCRIBED[0]) and out.endswith(engine._UNBACKED_NOTE)


# ------------------------------------------------------------------ "it's in your calendar" (P3)
IN_CALENDAR = (
    "Done -- I've put the plan in your calendar.",
    "The plan is now in your calendar, four gym sessions and the work blocks.",
    "Added the gym sessions to your calendar.",
    "Moved Thursday's gym to the evening.",
    "Cancelled Friday's gym.",
)
WRITES = {"week_plan", "week_plan:apply"}


def test_a_claim_that_the_plan_is_in_his_calendar_needs_a_write_that_succeeded():
    for reply in IN_CALENDAR:
        assert engine._unbacked_claims(reply, set(), 0), reply
    for reply in IN_CALENDAR:
        assert engine._unbacked_claims(reply, WRITES, 1) == [], reply


def test_each_kind_of_write_backs_it_and_so_does_a_direct_calendar_event():
    for backer in ("week_plan:apply", "week_plan:change", "week_plan:remove", "week_plan:undo",
                   "calendar_action:create_event", "calendar_action:update_event",
                   "calendar_action:delete_event"):
        assert engine._unbacked_claims(IN_CALENDAR[0], {backer}, 1) == [], backer


def test_proposing_or_reading_the_plan_is_not_writing_it():
    for tools in ({"week_plan", "week_plan:propose"}, {"week_plan", "week_plan:add"},
                  {"calendar_action", "calendar_action:list_events"}):
        assert engine._unbacked_claims(IN_CALENDAR[0], tools, 1), tools


def test_a_request_waiting_for_his_approval_is_not_a_write():
    """A gateway-held apply is pending, not done: _exec_tool never records it as succeeded."""
    assert engine._unbacked_claims("Done, the plan is in your calendar.", {"week_plan:propose"}, 2)


def test_offers_and_negations_about_the_calendar_are_not_claims():
    for reply in ("Want me to put it in your calendar?", "I can put the plan in your calendar now.",
                  "I'll put it in your calendar once you say so.",
                  "The plan is not in your calendar yet.", "Nothing has been added to your calendar."):
        assert engine._unbacked_claims(reply, {"week_plan:propose"}, 1) == [], reply


def test_describing_the_plan_after_reading_it_is_not_a_claim_unless_he_asked_to_write_it():
    reply = "The gym blocks are in your calendar on Monday, Tuesday, Thursday and Friday."
    assert engine._unbacked_claims(reply, {"week_plan:show"}, 1) == []
    assert engine._unbacked_claims(reply, {"week_plan:sync"}, 1) == []
    assert engine._unbacked_claims(reply, {"week_plan:show"}, 1, asked_to_save=True)
    assert engine._unbacked_claims(reply, {"week_plan:propose"}, 1)


def test_the_correction_says_nothing_was_written_to_the_calendar():
    (problem,) = engine._unbacked_claims(IN_CALENDAR[0], set(), 0)
    assert "no week_plan:write call succeeded" in problem and "nothing was written to the calendar" in problem


def test_asking_to_put_it_in_the_calendar_counts_as_a_request_to_save():
    for text in ("put it in my calendar", "Add it to my calendar please", "plan my week",
                 "organise my working hours next week", "organize my working hours for next week"):
        assert personality.is_persistence_request(text), text
    assert not personality.is_persistence_request("what is in my calendar today?")


def test_week_plan_is_one_of_the_tools_whose_absence_the_confab_guard_checks():
    assert "week_plan" in engine._PERSISTENCE_TOOLS


UNDO_CLAIMS = (
    "Undone. Thursday's gym is back to 17:30 to 18:30.",
    "I undid the move; the gym block is back where it was.",
    "Reverted the gym block to its earlier slot.",
    "Thursday's gym is back at 17:30.",
    "Moved. Thursday's gym now runs 18:30 to 19:30.",
    "Rolled back the last change to the plan.",
)


def test_saying_a_plan_change_is_undone_or_moved_needs_a_call_that_wrote():
    for reply in UNDO_CLAIMS:
        assert engine._unbacked_claims(reply, set(), 0), reply
        assert engine._unbacked_claims(reply, {"week_plan", "week_plan:propose"}, 1), reply
        assert engine._unbacked_claims(reply, {"week_plan", "week_plan:undo"}, 1) == [], reply
        assert engine._unbacked_claims(reply, {"week_plan", "week_plan:change"}, 1) == [], reply


def test_talking_about_undo_without_claiming_it_is_not_flagged():
    for reply in ("Say the word and I'll undo it.", "There is nothing to undo yet.",
                  "I can put it back if you want.", "The gym is not moved, it stays at 17:30."):
        assert engine._unbacked_claims(reply, {"week_plan:propose"}, 1) == [], reply


REPORT_CLAIMS = (
    "I marked the gym as done and the reading as skipped.",
    "Logged it as skipped, with your reason.",
    "Recorded the gym done and work partly.",
    "Noted: gym done.",
)


def test_saying_an_outcome_was_recorded_needs_a_report_that_succeeded():
    for reply in REPORT_CLAIMS:
        assert engine._unbacked_claims(reply, set(), 0), reply
        assert engine._unbacked_claims(reply, {"week_plan", "week_plan:propose"}, 1), reply
        assert engine._unbacked_claims(reply, {"week_plan", "week_plan:report"}, 1) == [], reply


def test_the_correction_for_an_unrecorded_outcome_says_nothing_was_recorded():
    (problem,) = engine._unbacked_claims(REPORT_CLAIMS[0], set(), 0)
    assert "no week_plan:report call succeeded" in problem and "so nothing was recorded" in problem


def test_describing_outcomes_after_reading_the_plan_is_not_a_claim():
    reply = "The gym is marked done for Monday."
    assert engine._unbacked_claims(reply, {"week_plan:show"}, 1) == []
    assert engine._unbacked_claims(reply, {"week_plan:show"}, 1, asked_to_save=True)


def test_talking_about_outcomes_without_claiming_a_record_is_fine():
    for reply in ("Tell me how it went and I will mark the gym as done or skipped.",
                  "I can log it as skipped if you like.",
                  "I recorded that in the vault.",
                  "Nothing has been marked as done yet."):
        assert engine._unbacked_claims(reply, set(), 0) == [], reply


def test_a_verb_ending_its_sentence_is_a_claim_only_when_the_next_sentence_names_the_block():
    """Live (2026-09-30, P4): 'moved. Same for the gym' -- an outcome remark about a block that was
    NOT moved -- drew a correction; 'Moved. Thursday's gym now runs 18:30' is still a claim."""
    for reply in ("Moved. Thursday's gym now runs 18:30 to 19:30.", "Removed. The gym is gone from Friday.",
                  "Cancelled. Your gym block is out of the calendar."):
        assert engine._unbacked_claims(reply, set(), 0), reply
    for reply in ("Work was not moved. Same for the gym.", "Nothing moved. Same for the gym."):
        assert engine._unbacked_claims(reply, set(), 0) == [], reply


# ------------------------------------------------------------------ 3 October: too strict
# Live: the holdings were recorded; the reply to "good night" said so; the check called that
# unbacked because nothing was recorded in THAT turn; the correction pass told JARVIS to call the
# tool; the redo was refused (the figures were in the previous message); he told Sam "nothing
# is in the ledger", and the note said everything above was NOT saved. The next turn's "a message
# about scheduled runs" drew the note again. Replayed with the real model before the fix: the
# correction pass retracted the true holdings in 2 of 3 trials.
GOODNIGHT = "Good night. The five positions are recorded and the ledger is the reference from here."
RETRACTION = ("I told you the five positions were recorded. They were not. Neither call succeeded, "
              "and nothing is in the ledger.")
EARLIER = frozenset({"portfolio", "portfolio:record_trade"})


def test_reproduced_live_describing_what_this_conversation_saved_is_not_a_claim():
    assert engine._unbacked_claims(GOODNIGHT, set(), 0)                         # the false alarm, before
    assert engine._unbacked_claims(GOODNIGHT, set(), 0, earlier=EARLIER) == []


def test_a_save_he_asks_for_now_still_needs_this_turn():
    assert engine._unbacked_claims(GOODNIGHT, set(), 0, asked_to_save=True, earlier=EARLIER)


def test_an_earlier_save_of_another_kind_backs_nothing():
    assert engine._unbacked_claims("Your reminder is set for 9.", set(), 0, earlier=EARLIER)


def test_a_correction_pass_is_not_run_for_a_true_description():
    _reset()
    async def rerun(msgs):
        raise AssertionError("no correction pass for what this conversation already saved")
    assert run(engine._enforce_backed_claims(GOODNIGHT, [], rerun, "discord", earlier=EARLIER)) == GOODNIGHT


def test_earlier_successes_are_kept_per_channel_and_session_only():
    engine._session_ok.clear()
    _reset(ok={"portfolio:record_trade"})
    engine._remember_ok("discord", "s1")
    _reset(ok={"standing_instruction"})
    engine._remember_ok("discord", "s1")
    assert engine._earlier_ok("discord", "s1") >= {"portfolio:record_trade", "standing_instruction"}
    assert engine._earlier_ok("discord", "s2") == frozenset()                 # a new session starts empty
    assert engine._earlier_ok("local", "s1") == frozenset()
    engine._remember_ok("discord", "s2")
    assert "portfolio:record_trade" not in engine._earlier_ok("discord", "s2")
    engine._session_ok.clear()


def test_chat_passes_the_session_successes_and_records_this_turn():
    src = inspect.getsource(engine.chat)
    check = src.index("_enforce_backed_claims(")
    assert "_earlier_ok(channel, decision.session_id)" in src[check:check + 400]
    assert src.index("_remember_ok(channel, decision.session_id)") > check    # after the correction pass too


def test_reporting_an_earlier_statement_is_not_making_it():
    # "I told you the five positions were recorded" is reported speech, not a save claim; its
    # "nothing is in the ledger" is a claim about the ledger, checked against the ledger itself
    # (test_ledger_truth.py), so it is the only problem left.
    problems = engine._unbacked_claims(RETRACTION, set(), 0)
    assert len(problems) == 1 and "about the portfolio ledger" in problems[0]
    assert engine._unbacked_claims("The five positions were recorded.", set(), 0)     # control


def test_a_participle_naming_a_thing_is_not_a_claim():
    third = "Last night there was a draft of a message about scheduled runs, the NVDA open and close prints."
    assert engine._unbacked_claims(third, set(), 0) == []
    for claim in ("Your reminder is set for 9.", "The scheduled checks are set for every weekday.",
                  "I've scheduled six runs for the week.", "Scheduled runs for every weekday at 15:30.",
                  "The email went out to Sarah.", "I recorded your NVDA position.",
                  # starts with the noun, not a participle: still a claim that the watch is active
                  "With the watch active, you will hear the moment NVDA drops."):
        assert engine._unbacked_claims(claim, set(), 0), claim


def test_the_correction_pass_checks_before_redoing():
    frame = engine._CLAIM_CHECK_FRAME
    assert "do not redo it" in frame and "portfolio show" in frame
    assert "a refused redo does not mean it is missing" in frame


def test_the_note_speaks_for_this_turn_only():
    note = engine._UNBACKED_NOTE
    assert "in this turn" in note and "/portfolio" in note and "was NOT" not in note
    (problem,) = engine._unbacked_claims(GOODNIGHT, set(), 0)
    assert problem.endswith("so nothing was saved this turn")
