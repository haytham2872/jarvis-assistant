"""Tests for the research loop's scoring, gating, and pace persistence.

Pure-function tests only — no Qdrant, no network, no LLM. Run with:
    .venv\\Scripts\\pytest backend/brain/test_research.py -v
"""

import math
import time
import asyncio

import pytest

from backend.brain import research
from backend.brain.research import (
    freshness, staleness, score_topic, should_tick, _topic_title,
    _FRESHNESS_WEIGHT, _STALENESS_WEIGHT, _PACE_INTERVALS_S,
    _EXISTING_NOTE_CAP, _SYNTH_MAX_TOKENS, _PICK_MAX_TOKENS,
)


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------

def test_staleness_never_researched_is_maximum():
    assert staleness(None) == 1.0


def test_staleness_just_researched_is_zero():
    assert staleness(0.0) == 0.0


def test_staleness_monotonically_increases():
    values = [staleness(d) for d in (0, 1, 3, 7, 14, 30, 90)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)  # strictly increasing


def test_staleness_bounded_below_one():
    assert staleness(365.0) < 1.0            # asymptotic, never quite reaches
    assert staleness(365.0) > 0.99           # ...but effectively saturated


def test_staleness_negative_days_clamped():
    assert staleness(-5.0) == 0.0


def test_never_researched_beats_any_researched_topic():
    # The whole point of the None case: an untouched note outranks even a
    # note last researched three months ago.
    assert staleness(None) > staleness(90.0)


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------

def test_freshness_decays_with_age():
    values = [freshness(0.8, 0.8, d) for d in (0, 1, 3, 7, 14)]
    assert values == sorted(values, reverse=True)


def test_freshness_increases_with_similarity_and_significance():
    base = freshness(0.5, 0.5, 1.0)
    assert freshness(0.9, 0.5, 1.0) > base
    assert freshness(0.5, 0.9, 1.0) > base


def test_freshness_bounded_zero_one():
    assert 0.0 <= freshness(0.0, 0.0, 0.0) <= 1.0
    assert 0.0 <= freshness(1.0, 1.0, 0.0) <= 1.0
    # out-of-range inputs are clamped, not propagated
    assert 0.0 <= freshness(1.5, 2.0, 0.0) <= 1.0
    assert freshness(-0.5, 0.5, 1.0) == 0.0


def test_freshness_zero_similarity_is_zero():
    assert freshness(0.0, 1.0, 0.0) == 0.0


def test_freshness_half_life_is_days_not_weeks():
    # The fast decay is the design point: a topic from ~a week ago should have
    # lost most of its heat (e^-7/3 ≈ 0.10 of its day-zero value).
    assert freshness(0.8, 0.8, 7.0) < 0.15 * freshness(0.8, 0.8, 0.0)


# ---------------------------------------------------------------------------
# fused score — the behaviors the design promises
# ---------------------------------------------------------------------------

def test_weights_sum_to_one():
    assert math.isclose(_FRESHNESS_WEIGHT + _STALENESS_WEIGHT, 1.0)


def test_hot_recent_topic_beats_cold_stale_topic():
    """E-behavior dominates: something discussed substantively an hour ago
    outranks something never researched but never discussed either."""
    hot  = score_topic(freshness(0.8, 0.8, 0.05), staleness(1.0))   # researched yesterday
    cold = score_topic(freshness(0.0, 0.0, 999),  staleness(None))  # untouched, undiscussed
    assert hot > cold


def test_stale_topic_eventually_wins_over_lukewarm_one():
    """D-behavior backstop: when nothing is hot, the most research-starved
    topic wins even if another was discussed slightly more recently."""
    barely_warm_recently_researched = score_topic(freshness(0.3, 0.2, 2.5), staleness(0.5))
    cold_but_never_researched       = score_topic(freshness(0.0, 0.0, 999), staleness(None))
    assert cold_but_never_researched > barely_warm_recently_researched


def test_equal_freshness_tie_broken_by_staleness():
    fresh = freshness(0.6, 0.6, 1.0)
    assert score_topic(fresh, staleness(None)) > score_topic(fresh, staleness(0.5))


def test_equal_staleness_tie_broken_by_freshness():
    stale = staleness(7.0)
    assert score_topic(freshness(0.8, 0.8, 0.5), stale) > score_topic(freshness(0.3, 0.3, 2.0), stale)


def test_score_bounded_zero_one():
    assert 0.0 <= score_topic(0.0, 0.0) <= 1.0
    assert 0.0 <= score_topic(1.0, 1.0) <= 1.0


# ---------------------------------------------------------------------------
# should_tick — pace gating
# ---------------------------------------------------------------------------

def test_off_never_ticks():
    now = time.time()
    assert not should_tick("off", 0.0, now)                 # even after forever
    assert not should_tick("off", now - 10**9, now)


def test_tick_not_due_immediately():
    now = time.time()
    for pace in ("low", "normal", "high"):
        assert not should_tick(pace, now, now)


def test_tick_due_after_interval():
    now = time.time()
    for pace in ("low", "normal", "high"):
        interval = _PACE_INTERVALS_S[pace]
        assert not should_tick(pace, now - interval + 1, now)
        assert should_tick(pace, now - interval, now)
        assert should_tick(pace, now - interval - 1, now)


def test_pace_ordering_high_faster_than_normal_faster_than_low():
    assert _PACE_INTERVALS_S["high"] < _PACE_INTERVALS_S["normal"] < _PACE_INTERVALS_S["low"]


def test_unknown_pace_never_ticks():
    assert not should_tick("bogus", 0.0, time.time())


# ---------------------------------------------------------------------------
# pace persistence + /pace command (uses a temp state file)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_pace_file(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    return tmp_path / "pace.json"


def test_pace_defaults_to_normal_when_no_file(tmp_pace_file):
    assert research.get_pace() == "normal"


def test_set_pace_round_trips(tmp_pace_file):
    assert research.set_pace("high")
    assert research.get_pace() == "high"
    assert research.set_pace("off")
    assert research.get_pace() == "off"


def test_set_pace_rejects_invalid(tmp_pace_file):
    assert not research.set_pace("turbo")
    assert research.get_pace() == "normal"   # unchanged


def test_corrupt_state_file_degrades_to_default(tmp_pace_file):
    tmp_pace_file.write_text("{not json", encoding="utf-8")
    assert research.get_pace() == "normal"


def test_pace_command_bare_reports_current(tmp_pace_file):
    reply = research.handle_pace_command("")
    assert "normal" in reply.lower()


def test_pace_command_sets_level(tmp_pace_file):
    reply = research.handle_pace_command("high")
    assert "high" in reply.lower()
    assert research.get_pace() == "high"


def test_pace_command_off(tmp_pace_file):
    reply = research.handle_pace_command("off")
    assert "off" in reply.lower()
    assert research.get_pace() == "off"


def test_pace_command_rejects_garbage(tmp_pace_file):
    reply = research.handle_pace_command("warp9")
    assert research.get_pace() == "normal"
    assert "off" in reply and "high" in reply   # lists the valid options


def test_set_pace_preserves_other_state_keys(tmp_pace_file):
    research._save_state({"pace": "normal", "last_tick": 12345.0, "last_notify": 99.0})
    research.set_pace("low")
    state = research._load_state()
    assert state["pace"] == "low"
    assert state["last_tick"] == 12345.0
    assert state["last_notify"] == 99.0


# ---------------------------------------------------------------------------
# note_update_allowed — the anti-truncation guard
# ---------------------------------------------------------------------------

def test_new_note_allowed_when_nothing_exists():
    assert research.note_update_allowed("", "x" * 200)


def test_tiny_output_rejected_even_for_new_note():
    assert not research.note_update_allowed("", "too short")


def test_growth_always_allowed():
    assert research.note_update_allowed("x" * 1000, "y" * 1500)


def test_modest_trim_allowed():
    # 70% of the original passes the 60% floor — deliberate tightening is OK
    assert research.note_update_allowed("x" * 1000, "y" * 700)


def test_truncated_rewrite_rejected():
    # The exact live failure: 5747-char note replaced by a 1124-char stub
    assert not research.note_update_allowed("x" * 5747, "y" * 1124)


def test_floor_boundary():
    existing = "x" * 1000
    assert research.note_update_allowed(existing, "y" * 600)       # exactly at floor
    assert not research.note_update_allowed(existing, "y" * 599)   # just below


def test_whitespace_not_counted():
    assert not research.note_update_allowed("x" * 1000, ("y" * 200) + " " * 5000)


# ---------------------------------------------------------------------------
# _salvage_message_text + the no-thinking wiring (2026-09-01: KIMI_STANDARD
# moved to kimi-k2.6, which thinks by default -- reasoning shares max_tokens
# with the answer and starved the picker/synthesis budgets dry, live-verified:
# same prompt with thinking disabled = finish_reason "stop" + clean JSON at
# 55 tokens; with the default = finish_reason "length", empty content, the
# whole budget consumed by reasoning_content)
# ---------------------------------------------------------------------------

class _FakeMsg:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


def test_salvage_prefers_content_when_present():
    m = _FakeMsg(content='{"subject": "x"}', reasoning_content="ignored narration")
    assert research._salvage_message_text(m) == '{"subject": "x"}'


def test_salvage_slices_reasoning_from_first_brace():
    m = _FakeMsg(content="", reasoning_content='The user wants me to pick... {"subject": "x"} done')
    assert research._salvage_message_text(m) == '{"subject": "x"} done'


def test_salvage_pure_narration_returns_empty():
    # The exact live failure shape (2026-09-01 20:34): all reasoning, no JSON.
    # Returning the narration only poisons _parse_json and triggers a doomed,
    # expensive continuation call downstream -- '' is the honest answer.
    m = _FakeMsg(content="", reasoning_content="The user wants me to act as JARVIS and choose ONE subject")
    assert research._salvage_message_text(m) == ""


def test_salvage_no_reasoning_attr_returns_empty():
    class Bare:
        content = None
    assert research._salvage_message_text(Bare()) == ""


def test_llm_disables_thinking(monkeypatch):
    """_llm must pass its active cheap provider's no_thinking_body as extra_body --
    both providers' standard tier reasons by default, sharing max_tokens with the
    answer (the live 2026-09-01 picker/synthesis starvation failure). A behavioral
    check against a fake client rather than source-inspection, since the call site
    now reads provider.no_thinking_body rather than a fixed KIMI_NO_THINKING_BODY
    literal -- true for both the Kimi and the OpenRouter provider descriptor."""
    import openai as openai_module
    from backend.brain import router as router_mod

    captured = []

    class _FakeMessage:
        content = '{"ok": true}'
        reasoning_content = None
        reasoning = None

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResp:
        usage = None
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = _FakeChat()

    monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeAsyncOpenAI)

    # A01: clients come from providers.gateway now, which (correctly) refuses to build
    # one for a provider with no credential. Give both descriptors a key so this test
    # exercises the request body it is actually about, and keep the pool clean so a
    # cached fake cannot leak into another test.
    from backend.providers import gateway
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake-openrouter")
    gateway._reset_for_tests()

    for provider in (router_mod.OPENROUTER,):
        monkeypatch.setattr(research, "_provider", lambda p=provider: p)
        captured.clear()
        gateway._reset_for_tests()
        result = asyncio.run(research._llm("prompt"))
        assert result == '{"ok": true}'
        _assert_no_thinking(captured[0]["extra_body"], provider)
        assert captured[0]["model"] == provider.standard
    gateway._reset_for_tests()


# ---------------------------------------------------------------------------
# _continue_truncated — one follow-up call to recover a truncated synthesis
# response before note_update_allowed's shrink floor ever sees it. Shares
# is_likely_truncated/_CONTINUATION_INSTRUCTION with consolidation.py, whose
# own tests (test_consolidation_fields.py) cover the detection signal itself.
# ---------------------------------------------------------------------------

def test_continue_truncated_concatenates_partial_and_continuation(monkeypatch):
    async def fake_llm(prompt, max_tokens=0):
        # The continuation prompt must carry the partial text forward.
        assert "cut off mid" in prompt
        return '-sentence but now finished"}'
    monkeypatch.setattr(research, "_llm", fake_llm)

    result = asyncio.run(research._continue_truncated(
        "original synthesis prompt", '{"note": "cut off mid', max_tokens=100,
    ))
    assert result == '{"note": "cut off mid-sentence but now finished"}'


def test_continue_truncated_returns_partial_unchanged_when_continuation_empty(monkeypatch):
    async def empty_llm(prompt, max_tokens=0):
        return ""
    monkeypatch.setattr(research, "_llm", empty_llm)

    partial = '{"note": "cut off mid'
    result = asyncio.run(research._continue_truncated(
        "original synthesis prompt", partial, max_tokens=100,
    ))
    assert result == partial


# ---------------------------------------------------------------------------
# worth_attempting — the "nothing to do" gate that skips before the picker
# ---------------------------------------------------------------------------

def test_never_researched_backstop_still_attempted_with_nothing_recent():
    # The whole point of preserving the D-behavior backstop: an untouched
    # topic (score 0.35 from staleness alone) must still clear the gate even
    # when there's no fresh conversation to lean on.
    untouched_score = score_topic(0.0, staleness(None))
    assert research.worth_attempting(untouched_score, has_recent=False)


def test_degenerate_quiet_stretch_is_skipped():
    # The exact live scenario: everything just researched (staleness ~0),
    # nothing being discussed (freshness ~0) -- this is what produced the
    # 4-in-a-row picker failures. Should now be skipped before ever calling
    # the picker rather than forcing a doomed pick.
    near_zero_score = score_topic(0.0, staleness(0.5))
    assert not research.worth_attempting(near_zero_score, has_recent=False)


def test_recent_conversation_always_overrides_a_low_score():
    # An emerging subject has no note yet, hence no score at all -- fresh
    # conversation alone must be enough to justify attempting a tick.
    assert research.worth_attempting(0.0, has_recent=True)


def test_exactly_at_floor_is_worth_attempting():
    assert research.worth_attempting(research._MIN_SCORE_TO_ATTEMPT, has_recent=False)


def test_just_below_floor_without_recent_is_not():
    assert not research.worth_attempting(research._MIN_SCORE_TO_ATTEMPT - 0.001, has_recent=False)


def test_floor_set_below_the_staleness_backstop():
    # If this ever regresses (floor raised above 0.35), the never-researched
    # backstop from test_never_researched_backstop_still_attempted_with_nothing_recent
    # silently stops firing -- pin the relationship directly.
    assert research._MIN_SCORE_TO_ATTEMPT < 0.35


# ---------------------------------------------------------------------------
# fallback_pick — deterministic stand-in for a failed picker call
# ---------------------------------------------------------------------------

def test_fallback_picks_the_top_scored_candidate():
    candidates = [
        {"file": "a.md", "title": "Topic A", "score": 0.9, "freshness": 0.9, "staleness": 0.5},
        {"file": "b.md", "title": "Topic B", "score": 0.3, "freshness": 0.1, "staleness": 0.8},
    ]
    result = research.fallback_pick(candidates)
    assert result["subject"] == "Topic A"
    assert result["existing_file"] == "a.md"


def test_fallback_generates_nonempty_queries():
    candidates = [{"file": "a.md", "title": "Quantum Computing", "score": 0.5,
                   "freshness": 0.5, "staleness": 0.5}]
    result = research.fallback_pick(candidates)
    assert len(result["queries"]) >= 1
    assert all("Quantum Computing" in q for q in result["queries"])


def test_fallback_none_when_no_candidates():
    assert research.fallback_pick([]) is None


def test_fallback_shape_matches_real_picker_output():
    # _tick() treats a real pick and a fallback pick identically downstream --
    # same keys, same types.
    candidates = [{"file": "a.md", "title": "X", "score": 0.5, "freshness": 0.5, "staleness": 0.5}]
    result = research.fallback_pick(candidates)
    assert set(result.keys()) == {"subject", "existing_file", "angle", "why", "question", "queries"}
    assert result["question"]
    assert isinstance(result["queries"], list)
    assert result["angle"] in research._ANGLES


def test_fallback_rotates_angle_after_last_one():
    candidates = [{"file": "a.md", "title": "Camus", "score": 0.9,
                   "freshness": 0.9, "staleness": 0.5, "last_angle": "news"}]
    result = research.fallback_pick(candidates)
    assert result["angle"] == "deepen"
    # queries come from the rotated angle's templates, not "recent developments"
    assert not any("recent developments" in q for q in result["queries"])


def test_fallback_without_last_angle_avoids_repeating_news():
    # A researched note with no last_angle dates from the 1.0 era when every
    # pass was effectively "news" -- rotate away from it, don't repeat it.
    candidates = [{"file": "a.md", "title": "Camus", "score": 0.9,
                   "freshness": 0.9, "staleness": 0.5}]
    result = research.fallback_pick(candidates)
    assert result["angle"] == "deepen"


# ---------------------------------------------------------------------------
# Angles — registry sanity + rotation
# ---------------------------------------------------------------------------

def test_every_angle_has_desc_and_templates():
    for name, spec in research._ANGLES.items():
        assert spec["desc"].strip()
        assert len(spec["templates"]) >= 1
        assert all("{title}" in t for t in spec["templates"])
        # templates must actually format
        assert all("Camus" in t.format(title="Camus") for t in spec["templates"])


def test_angle_order_covers_exactly_the_registry():
    assert set(research._ANGLE_ORDER) == set(research._ANGLES.keys())


def test_next_angle_cycles_the_full_order():
    seen = []
    angle = "news"
    for _ in range(len(research._ANGLE_ORDER)):
        seen.append(angle)
        angle = research.next_angle(angle)
    assert sorted(seen) == sorted(research._ANGLE_ORDER)  # visits every angle once
    assert angle == "news"  # full cycle wraps around


def test_next_angle_unknown_or_missing_defaults_to_deepen():
    assert research.next_angle(None) == "deepen"
    assert research.next_angle("bogus") == "deepen"


# ---------------------------------------------------------------------------
# _valid_pick — placeholder-echo rejection (both shapes happened live: the
# loop researched the ellipsis punctuation mark and "search queries")
# ---------------------------------------------------------------------------

def test_valid_pick_accepts_a_real_pick():
    assert research._valid_pick({
        "subject": "albert camus", "existing_file": "albert-camus.md",
        "angle": "perspectives", "why": "contrast",
        "question": "How do two interpretations of revolt differ?",
        "queries": ["criticism of albert camus", "camus sartre dispute arguments"],
    })


def test_valid_pick_rejects_ellipsis_subject():
    # Live: subject "..." -> researched the ellipsis punctuation mark
    assert not research._valid_pick({
        "subject": "...", "existing_file": None, "queries": ["..."],
    })


def test_valid_pick_rejects_template_echo():
    # Live: the picker echoed the prompt template literally
    assert not research._valid_pick({
        "subject": "short subject name", "existing_file": None,
        "queries": ["1-2 focused web search queries"],
    })


def test_valid_pick_rejects_placeholder_queries_even_with_real_subject():
    assert not research._valid_pick({
        "subject": "albert camus", "existing_file": "albert-camus.md",
        "queries": ["one or two web search queries shaped by the angle"],
    })


def test_valid_pick_rejects_missing_fields():
    assert not research._valid_pick(None)
    assert not research._valid_pick({})
    assert not research._valid_pick({"subject": "x"})
    assert not research._valid_pick({"queries": ["y"]})
    assert not research._valid_pick({
        "subject": "albert camus", "queries": ["Camus revolt primary text"]})


def test_picker_and_synthesis_prompts_request_depth_without_forced_opposition():
    pick = research._PICK_PROMPT.format(
        angles="angles", shortlist="notes", recent="conversation", closed="none")
    synth = research._SYNTH_PROMPT.format(
        subject="Camus", angle="deepen", why="understand revolt",
        question="What does revolt mean in daily life?", angle_instruction="Explain it.",
        existing_note="(none)", findings="Source https://example.org/camus")
    assert "one concrete question" in pick
    assert '"skip": true' in pick
    assert "Do not look for an objection" in pick
    assert "everyday language" in synth
    assert "one concrete example" in synth
    assert "progress=false" in synth
    assert "Novelty and disagreement alone are not reasons" in synth


def test_open_question_is_given_to_fallback_question_pass():
    note = "# Camus\n\n## Open questions\n- Why does revolt require limits?\n"
    question = research._first_open_question(note)
    pick = research.fallback_pick([{
        "file": "camus.md", "title": "Camus", "score": 0.8,
        "last_angle": "connections", "open_question": question,
    }])
    assert pick["angle"] == "questions"
    assert pick["question"] == "Why does revolt require limits?"
    assert pick["queries"][0] == "Why does revolt require limits?"


# ---------------------------------------------------------------------------
# topic_in_cooldown — one hot topic must not monopolize every tick
# (live: ~20 albert-camus passes in a single night at pace=high)
# ---------------------------------------------------------------------------

def test_never_researched_topic_is_not_in_cooldown():
    assert not research.topic_in_cooldown(None)


def test_topic_researched_today_is_in_cooldown():
    assert research.topic_in_cooldown(0.0)
    assert research.topic_in_cooldown(0.2)   # ~5h ago


def test_topic_researched_days_ago_is_out_of_cooldown():
    assert not research.topic_in_cooldown(1.0)
    assert not research.topic_in_cooldown(2.0)


def test_cooldown_boundary():
    edge = research._TOPIC_COOLDOWN_HOURS / 24.0
    assert research.topic_in_cooldown(edge - 0.01)
    assert not research.topic_in_cooldown(edge)


# ---------------------------------------------------------------------------
# Research queue — deliberate requests, ahead of the picker
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_queue_file(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_QUEUE_FILE", tmp_path / "research-queue.md")
    return tmp_path / "research-queue.md"


def test_parse_queued_line_full_shape():
    item = research._parse_queued_line("[queued] 2026-07-05 | Albert Camus | perspectives | Sam loves this")
    assert item == {
        "date": "2026-07-05", "subject": "Albert Camus",
        "angle": "perspectives", "reason": "Sam loves this",
    }


def test_parse_queued_line_bare_subject_defaults_angle_none():
    item = research._parse_queued_line("[queued] 2026-07-05 | Albert Camus")
    assert item["angle"] is None
    assert item["reason"] == ""


def test_parse_queued_line_unknown_angle_becomes_none():
    item = research._parse_queued_line("[queued] 2026-07-05 | X | not-a-real-angle | y")
    assert item["angle"] is None   # 'auto' or garbage -> loop chooses


def test_parse_queued_line_rejects_done_lines_and_garbage():
    assert research._parse_queued_line("[done] 2026-07-05 14:00 | X [news] (auto) | wrote") is None
    assert research._parse_queued_line("# Research Queue") is None
    assert research._parse_queued_line("") is None


def test_pop_queued_is_fifo_and_removes_the_line(tmp_queue_file):
    research._write_queue_file([
        "[queued] 2026-07-01 | First Subject | | ",
        "[queued] 2026-07-02 | Second Subject | | ",
    ])
    first = research._pop_queued()
    assert first["subject"] == "First Subject"
    remaining = research._read_queue_file()
    assert not any("First Subject" in l for l in remaining)
    assert any("Second Subject" in l for l in remaining)
    second = research._pop_queued()
    assert second["subject"] == "Second Subject"
    assert research._pop_queued() is None


def test_pop_queued_empty_file_returns_none(tmp_queue_file):
    assert research._pop_queued() is None


def test_queued_to_pick_shape_and_angle_rotation():
    item = {"date": "2026-07-05", "subject": "Some New Subject", "angle": None, "reason": "came up today"}
    pick = research.queued_to_pick(item)
    assert pick["subject"] == "Some New Subject"
    assert pick["existing_file"] is None   # no matching vault note in this test env
    assert pick["angle"] in research._ANGLES
    assert pick["why"] == "came up today"
    assert len(pick["queries"]) >= 1


def test_queued_to_pick_respects_explicit_angle():
    item = {"date": "2026-07-05", "subject": "X", "angle": "connections", "reason": ""}
    pick = research.queued_to_pick(item)
    assert pick["angle"] == "connections"
    assert pick["why"] == "queued request"   # falls back when no reason given


def test_log_research_pass_appends_done_line(tmp_queue_file):
    research._log_research_pass("Albert Camus", "news", "wrote topics/albert-camus.md", queued=True)
    lines = research._read_queue_file()
    done = [l for l in lines if l.strip().startswith("[done]")]
    assert len(done) == 1
    assert "Albert Camus" in done[0] and "(queued)" in done[0] and "[news]" in done[0]


def test_log_research_pass_never_trims_old_entries(tmp_queue_file):
    # The done-log used to cap at 60 lines and silently delete the oldest --
    # found live already sitting at exactly 60, meaning real history was
    # already being lost. It must now be genuinely append-only: no count of
    # passes should ever cause an earlier one to disappear from storage.
    n = 75
    for i in range(n):
        research._log_research_pass(f"Subject {i}", "news", "wrote", queued=False)
    lines = research._read_queue_file()
    done = [l for l in lines if l.strip().startswith("[done]")]
    assert len(done) == n
    assert "Subject 0" in "\n".join(done)              # oldest still present
    assert f"Subject {n - 1}" in "\n".join(done)        # newest also present


def test_research_queue_status_display_is_bounded_but_storage_is_not(tmp_queue_file):
    for i in range(research._STATUS_RECENT_LINES + 5):
        research._log_research_pass(f"Subject {i}", "news", "wrote", queued=False)
    status = asyncio.run(research.research_queue("status"))
    shown = status.count("[done]")
    assert shown == research._STATUS_RECENT_LINES
    # The oldest passes are absent from the *display* but must still be on disk.
    stored = [l for l in research._read_queue_file() if l.strip().startswith("[done]")]
    assert len(stored) == research._STATUS_RECENT_LINES + 5
    assert "Subject 0" in "\n".join(stored)


def test_research_queue_add_then_status(tmp_queue_file):
    reply = asyncio.run(
        research.research_queue("add", subject="Albert Camus", angle="perspectives", reason="loves this")
    )
    assert "Albert Camus" in reply
    status = asyncio.run(research.research_queue("status"))
    assert "Albert Camus" in status


def test_research_queue_rejects_duplicate_subject(tmp_queue_file):
    asyncio.run(research.research_queue("add", subject="Albert Camus"))
    reply = asyncio.run(research.research_queue("add", subject="albert camus"))  # case-insensitive dup
    assert "already" in reply.lower()


def test_research_queue_rejects_invalid_angle(tmp_queue_file):
    reply = asyncio.run(research.research_queue("add", subject="X", angle="bogus-angle"))
    assert "Error" in reply


def test_research_queue_rejects_too_short_subject(tmp_queue_file):
    reply = asyncio.run(research.research_queue("add", subject=".."))
    assert "Error" in reply


def test_research_queue_enforces_cap(tmp_queue_file):
    for i in range(research._MAX_QUEUED):
        reply = asyncio.run(research.research_queue("add", subject=f"Subject {i}"))
        assert "Error" not in reply
    reply = asyncio.run(research.research_queue("add", subject="One Too Many"))
    assert "Error" in reply


def test_research_queue_unknown_action(tmp_queue_file):
    reply = asyncio.run(research.research_queue("nonsense"))
    assert "Error" in reply


def test_research_queue_status_when_empty(tmp_queue_file):
    reply = asyncio.run(research.research_queue("status"))
    assert "no pending" in reply.lower()


# ---------------------------------------------------------------------------
# Deferred notifications — findings held back by the min-notify gap, must
# survive to reach Sam through SOME door (DM later, or mentioned in
# person) instead of being silently lost, which is what happened live.
# ---------------------------------------------------------------------------

def test_defer_then_pop_round_trips(tmp_pace_file):
    research._defer_notification(
        "Albert Camus", "His archive was just digitized.\nSources: https://example.org/archive")
    text = research.pop_deferred_mentions()
    assert "Albert Camus" in text
    assert "digitized" in text


def test_pop_deferred_mentions_is_consumed_exactly_once(tmp_pace_file):
    research._defer_notification("X", "finding\nSources: https://example.org/x")
    first = research.pop_deferred_mentions()
    second = research.pop_deferred_mentions()
    assert first != ""
    assert second == ""


def test_pop_deferred_mentions_empty_when_nothing_pending(tmp_pace_file):
    assert research.pop_deferred_mentions() == ""


def test_defer_notification_caps_at_max_deferred(tmp_pace_file):
    for i in range(research._MAX_DEFERRED + 3):
        research._defer_notification(f"Subject {i}", f"finding {i}")
    state = research._load_state()
    assert len(state["deferred_notifies"]) == research._MAX_DEFERRED
    # newest survive, oldest dropped
    subjects = [p["subject"] for p in state["deferred_notifies"]]
    assert f"Subject {research._MAX_DEFERRED + 2}" in subjects
    assert "Subject 0" not in subjects


def test_flush_deferred_sends_once_gap_expired(tmp_pace_file, monkeypatch):
    sent_messages = []

    async def fake_notifier(text):
        sent_messages.append(text)
        return True

    research.set_notifier(fake_notifier)
    try:
        research._defer_notification("Camus", "finding one\nSources: https://example.org/camus")
        state = research._load_state()
        state["last_notify"] = 0.0   # long ago -- gap has expired
        research._save_state(state)
        asyncio.run(research._flush_deferred(time.time()))
        assert len(sent_messages) == 1
        assert "finding one" in sent_messages[0]
        assert research._load_state().get("deferred_notifies") == []
    finally:
        research.set_notifier(None)


def test_unsourced_legacy_deferred_finding_is_not_sent(tmp_pace_file, monkeypatch):
    sent = []
    async def fake_notifier(text):
        sent.append(text)
        return True
    research.set_notifier(fake_notifier)
    try:
        research._defer_notification("Unverified", "a claim with no source")
        asyncio.run(research._flush_deferred(time.time()))
        assert sent == []
        assert research._load_state().get("deferred_notifies") == []
    finally:
        research.set_notifier(None)


def test_flush_deferred_does_nothing_within_gap(tmp_pace_file, monkeypatch):
    sent_messages = []

    async def fake_notifier(text):
        sent_messages.append(text)
        return True

    research.set_notifier(fake_notifier)
    try:
        research._defer_notification("Camus", "finding one")
        now = time.time()
        state = research._load_state()
        state["last_notify"] = now   # just sent -- gap has NOT expired
        research._save_state(state)
        asyncio.run(research._flush_deferred(now))
        assert sent_messages == []
        assert len(research._load_state().get("deferred_notifies", [])) == 1  # still pending
    finally:
        research.set_notifier(None)


def test_flush_deferred_noop_without_notifier(tmp_pace_file):
    research.set_notifier(None)
    research._defer_notification("Camus", "finding")
    asyncio.run(research._flush_deferred(time.time()))  # should not raise
    assert len(research._load_state().get("deferred_notifies", [])) == 1


# ---------------------------------------------------------------------------
# _topic_title — the exact live cascade failure: a generic H2 section label
# mistaken for the note's actual subject
# ---------------------------------------------------------------------------

def test_h1_title_used_when_present():
    body = "# Olah Emotion Vectors and Phenomenology\n\nBody text here."
    assert _topic_title("olah-emotion-vectors.md", body) == "Olah Emotion Vectors and Phenomenology"


def test_generic_h2_section_label_rejected_falls_back_to_filename():
    # The exact live failure: this note's first heading is "## Context", a
    # section label, not a title -- picking it produced nonsense research
    # queries ("Context recent developments") that failed at synthesis too.
    body = "## Context\nForecast retrieved for Sam in Vincennes, Paris."
    title = _topic_title("paris-weather-2026-05-19.md", body)
    assert title != "Context"
    assert title == "paris weather 2026 05 19"


def test_no_heading_at_all_falls_back_to_filename():
    body = "Just prose, no heading of any kind."
    assert _topic_title("albert-camus.md", body) == "albert camus"


def test_h3_or_deeper_also_rejected():
    body = "### Deep subsection\nSome text."
    assert _topic_title("some-note.md", body) == "some note"


def test_leading_blank_lines_before_h1_still_recognized():
    body = "\n\n# Real Title\n\nBody."
    assert _topic_title("x.md", body) == "Real Title"


def test_every_real_topic_note_gets_a_sane_title(tmp_path):
    """Regression guard against the exact live cascade: run the real
    extraction against every actual note in the vault (not just synthetic
    cases) and confirm none of them produce one of the known-bad generic
    section labels that caused the failure."""
    from pathlib import Path as _Path
    from backend.brain.research import _strip_frontmatter

    topics_dir = _Path(__file__).parent.parent.parent / "jarvis-vault" / "topics"
    bad_titles = {"context", "core findings", "structure", "price action",
                  "overview", "summary", "notes"}
    if not topics_dir.exists():
        pytest.skip("no vault present in this environment")
    checked = 0
    for path in topics_dir.glob("*.md"):
        content = path.read_text(encoding="utf-8", errors="replace")
        title = _topic_title(path.name, _strip_frontmatter(content))
        assert title.strip().lower() not in bad_titles, f"{path.name} -> {title!r}"
        checked += 1
    if checked == 0:
        pytest.skip("vault has no topic notes yet")


# ---------------------------------------------------------------------------
# Synthesis token/char budgets — regression for the 2026-07-04 live failure:
# the Camus (~6K char) and Dostoevsky (~8.2K char) notes were fed to the
# synthesis prompt truncated at 4000 chars and asked to complete within 4000
# output tokens, guaranteeing either truncated/unparseable JSON or a
# shrink-floor rejection. Both budgets must actually cover real notes.
# ---------------------------------------------------------------------------

def test_pick_budget_smaller_than_synth_budget():
    # The picker only needs a few lines of JSON; synthesis must reproduce an
    # entire note plus additions inside JSON — it needs a much larger budget.
    assert _PICK_MAX_TOKENS < _SYNTH_MAX_TOKENS


def test_existing_note_cap_covers_every_real_topic_note():
    """The existing-note input cap must not truncate any real note before the
    synthesis model sees it -- the prompt explicitly instructs it to preserve
    everything, which is impossible for content it was never shown."""
    from pathlib import Path as _Path
    from backend.brain.research import _strip_frontmatter

    topics_dir = _Path(__file__).parent.parent.parent / "jarvis-vault" / "topics"
    if not topics_dir.exists():
        pytest.skip("no vault present in this environment")
    checked = 0
    for path in topics_dir.glob("*.md"):
        content = path.read_text(encoding="utf-8", errors="replace")
        body = _strip_frontmatter(content)
        assert len(body) <= _EXISTING_NOTE_CAP, (
            f"{path.name} is {len(body)} chars, exceeds _EXISTING_NOTE_CAP={_EXISTING_NOTE_CAP}"
        )
        checked += 1
    if checked == 0:
        pytest.skip("vault has no topic notes yet")


# Measured (not guessed) 2026-09-01 via scripts/measure_note_token_ratio.py,
# which calls Anthropic's real Messages API token counting against every vault
# topic note -- see that script's docstring for why this can't be measured
# live inside the test suite (it's a real API call, which this suite never
# makes). 3.82 was the worst (most token-dense) ratio observed across 31 real
# notes, average was 4.45 -- using the worst case, not the average, is what
# makes this a safety check rather than an optimistic one. Re-run the script
# and update this constant if the vault's content mix changes meaningfully
# (e.g. a lot more non-English or code-heavy notes, which tokenize denser).
_MEASURED_WORST_CHARS_PER_TOKEN = 3.82


def test_synth_budget_covers_largest_real_note_reproduction():
    """The output token budget must be large enough to reproduce the largest
    real note (at the worst real measured chars/token ratio) plus headroom
    for JSON escaping and additions -- not just barely fit it."""
    from pathlib import Path as _Path
    from backend.brain.research import _strip_frontmatter

    topics_dir = _Path(__file__).parent.parent.parent / "jarvis-vault" / "topics"
    if not topics_dir.exists() or not any(topics_dir.glob("*.md")):
        pytest.skip("no vault topic notes present in this environment")
    largest = max(
        len(_strip_frontmatter(p.read_text(encoding="utf-8", errors="replace")))
        for p in topics_dir.glob("*.md")
    )
    estimated_tokens_to_reproduce = largest / _MEASURED_WORST_CHARS_PER_TOKEN
    assert _SYNTH_MAX_TOKENS >= estimated_tokens_to_reproduce * 1.5  # headroom for additions/escaping


# ---------------------------------------------------------------------------
# _parse_json — must survive prose BEFORE and AFTER the JSON object
# ---------------------------------------------------------------------------

def test_parse_json_plain_object():
    assert research._parse_json('{"subject": "x", "queries": ["a"]}') == {
        "subject": "x", "queries": ["a"],
    }


def test_parse_json_prose_preamble_only():
    raw = 'Sure, here is the JSON: {"subject": "x", "queries": ["a"]}'
    assert research._parse_json(raw) == {"subject": "x", "queries": ["a"]}


def test_parse_json_prose_before_and_after():
    # The exact recurring live shape: the picker narrates its reasoning,
    # then the JSON, sometimes with more text (or a closing fence) after it.
    raw = (
        'The user wants me to act as JARVIS choosing a research subject. '
        'I need to:\n1. Look at the scored notes\n\n'
        '{"subject": "dostoevsky philosophy chronology", '
        '"existing_file": "dostoevsky-philosophy-chronology.md", '
        '"queries": ["a", "b"]}\n\nLet me know if you need anything else.'
    )
    parsed = research._parse_json(raw)
    assert parsed is not None
    assert parsed["subject"] == "dostoevsky philosophy chronology"
    assert parsed["queries"] == ["a", "b"]


def test_parse_json_fenced_with_trailing_prose():
    raw = '```json\n{"subject": "x", "queries": ["a"]}\n```\nHope that helps!'
    assert research._parse_json(raw) == {"subject": "x", "queries": ["a"]}


def test_parse_json_pure_prose_no_object_returns_none():
    raw = 'The user wants me to act as JARVIS choosing a research subject. I need to:\n\n'
    assert research._parse_json(raw) is None


def test_parse_json_empty_string_returns_none():
    assert research._parse_json("") is None


# ---------------------------------------------------------------------------
# Wikipedia URL detection — the API-extract bypass for the 403 scrape block
# ---------------------------------------------------------------------------

def test_wikipedia_url_matches_and_captures_lang_and_title():
    from backend.tools.browser import _WIKIPEDIA_RE
    m = _WIKIPEDIA_RE.match("https://en.wikipedia.org/wiki/Fyodor_Dostoevsky")
    assert m is not None
    assert m.group(1) == "en"
    assert m.group(2) == "Fyodor_Dostoevsky"


def test_wikipedia_url_strips_fragment_and_query():
    from backend.tools.browser import _WIKIPEDIA_RE
    m = _WIKIPEDIA_RE.match("https://en.wikipedia.org/wiki/Albert_Camus?action=history#Life")
    assert m is not None
    assert m.group(2) == "Albert_Camus"


def test_non_wikipedia_url_does_not_match():
    from backend.tools.browser import _WIKIPEDIA_RE
    assert _WIKIPEDIA_RE.match("https://en.wikipedia.org.evil.com/wiki/X") is None
    assert _WIKIPEDIA_RE.match("https://example.com/wiki/Fyodor_Dostoevsky") is None


# ---------------------------------------------------------------------------
# Subject families (2026-09-06 hardening) - grouping, cooldown/cap at family
# level, aggregation, and the closed-topic veto.
# ---------------------------------------------------------------------------

def test_topic_tokens_extracts_proper_nouns():
    assert research._topic_tokens("Viktor Frankl", "viktor-frankl.md") == {"viktor", "frankl"}


def test_topic_tokens_filters_stopwords():
    tokens = research._topic_tokens(
        "NVIDIA Stock Decline Analysis", "nvidia-stock-decline-analysis-2026-06.md"
    )
    assert "nvidia" in tokens
    assert "stock" not in tokens
    assert "decline" not in tokens
    assert "analysis" not in tokens


def test_topic_tokens_filters_short_words():
    assert "ai" not in research._topic_tokens("AI and Camus", "ai-and-camus.md")


def test_topic_tokens_no_shared_token_for_unrelated_finance_notes():
    # The real live collision this stoplist exists to prevent: NVIDIA and
    # Tesla notes both say "stock", which must NOT be a family-linking key.
    nvidia = research._topic_tokens("NVIDIA stock", "nvidia-stock-june-2025-correction.md")
    tesla = research._topic_tokens("Tesla stock", "tesla-stock-research-2026-06.md")
    assert not (nvidia & tesla)


def test_build_families_groups_by_shared_token():
    entries = [
        {"file": "viktor-frankl.md", "title": "Viktor Frankl"},
        {"file": "frankl-my-position.md", "title": "Frankl: My Position"},
        {"file": "albert-camus.md", "title": "Albert Camus"},
    ]
    families = research.build_families(entries)
    groups = {frozenset(g) for g in families}
    assert frozenset({"viktor-frankl.md", "frankl-my-position.md"}) in groups
    assert frozenset({"albert-camus.md"}) in groups


def test_build_families_groups_by_embedding_when_no_shared_token():
    entries = [
        {"file": "a.md", "title": "Something Unique One"},
        {"file": "b.md", "title": "Completely Different Two"},
    ]
    vectors = {"a.md": [1.0, 0.0], "b.md": [0.99, 0.14]}  # cosine ~0.99, no shared token
    families = research.build_families(entries, vectors)
    assert len(families) == 1
    assert set(families[0]) == {"a.md", "b.md"}


def test_build_families_does_not_merge_dissimilar_untokened_notes():
    entries = [
        {"file": "a.md", "title": "Something Unique One"},
        {"file": "b.md", "title": "Completely Different Two"},
    ]
    vectors = {"a.md": [1.0, 0.0], "b.md": [0.0, 1.0]}  # orthogonal, no shared token
    families = research.build_families(entries, vectors)
    assert len(families) == 2


def test_build_families_transitive_grouping():
    # a<->b via token, b<->c via a different token -- must still end up one family.
    entries = [
        {"file": "dostoevsky.md", "title": "Dostoevsky"},
        {"file": "fyodor-dostoevsky.md", "title": "Fyodor Dostoevsky"},
        {"file": "fyodor-dostoevsky-timeline.md", "title": "Fyodor Dostoevsky Timeline"},
    ]
    families = research.build_families(entries)
    assert len(families) == 1
    assert len(families[0]) == 3


def test_build_families_no_vectors_falls_back_to_token_only():
    entries = [
        {"file": "a.md", "title": "Camus"},
        {"file": "b.md", "title": "Camus Reconsidered"},
    ]
    families = research.build_families(entries, vectors=None)
    assert len(families) == 1


def test_family_in_cooldown_true_if_any_member_recent():
    members = [{"days_since": 10.0}, {"days_since": 0.1}]
    assert research.family_in_cooldown(members) is True


def test_family_in_cooldown_false_if_all_members_old():
    members = [{"days_since": 10.0}, {"days_since": 5.0}]
    assert research.family_in_cooldown(members) is False


def test_family_in_cooldown_none_days_since_is_never_in_cooldown():
    assert research.family_in_cooldown([{"days_since": None}]) is False


def test_family_weekly_count_counts_within_window():
    members = [{"days_since": 1.0}, {"days_since": 3.0}, {"days_since": 10.0}, {"days_since": None}]
    assert research.family_weekly_count(members) == 2


def test_family_in_weekly_cap_true_at_cap():
    members = [{"days_since": 1.0}, {"days_since": 3.0}]
    assert research.family_in_weekly_cap(members) is True


def test_family_in_weekly_cap_false_under_cap():
    members = [{"days_since": 1.0}]
    assert research.family_in_weekly_cap(members) is False


def _member(file, days_since, freshness=0.0, last_angle=None, title=None):
    return {
        "file": file, "title": title or file, "days_since": days_since,
        "freshness": freshness, "last_angle": last_angle, "digest": f"digest-{file}",
    }


def test_aggregate_family_picks_most_overdue_as_representative():
    members = [_member("a.md", days_since=2.0), _member("b.md", days_since=20.0)]
    agg = research._aggregate_family(members)
    assert agg["file"] == "b.md"


def test_aggregate_family_never_researched_member_is_most_overdue():
    members = [_member("a.md", days_since=20.0), _member("b.md", days_since=None)]
    agg = research._aggregate_family(members)
    assert agg["file"] == "b.md"
    assert agg["staleness"] == research.staleness(None)


def test_aggregate_family_freshness_is_family_max():
    members = [_member("a.md", days_since=5.0, freshness=0.2), _member("b.md", days_since=1.0, freshness=0.9)]
    agg = research._aggregate_family(members)
    assert agg["freshness"] == 0.9


def test_aggregate_family_last_angle_from_most_recently_researched_member():
    # b.md was researched more recently (smaller days_since) than a.md, even
    # though a.md is the write target (most overdue) -- rotation must move
    # off the family's actual last pass (b's angle), not a's.
    members = [
        _member("a.md", days_since=20.0, last_angle="deepen"),
        _member("b.md", days_since=1.0, last_angle="perspectives"),
    ]
    agg = research._aggregate_family(members)
    assert agg["last_angle"] == "perspectives"
    assert research.next_angle(agg["last_angle"]) != "perspectives"


def test_aggregate_family_single_member_matches_old_per_file_behavior():
    members = [_member("solo.md", days_since=3.0, freshness=0.4)]
    agg = research._aggregate_family(members)
    assert agg["file"] == "solo.md"
    assert agg["freshness"] == 0.4
    assert agg["staleness"] == research.staleness(3.0)


def test_aggregate_family_records_all_member_filenames():
    members = [_member("a.md", days_since=1.0), _member("b.md", days_since=2.0)]
    agg = research._aggregate_family(members)
    assert agg["family_files"] == ["a.md", "b.md"]


# ---------------------------------------------------------------------------
# Closed topics -- research_queue 'close'/'reopen', and the picker veto
# ---------------------------------------------------------------------------

def test_parse_research_closed_true():
    content = "---\ntype: topic\nresearch_closed: true\n---\n\nBody"
    assert research._parse_research_closed(content) is True


def test_parse_research_closed_false_when_absent():
    content = "---\ntype: topic\n---\n\nBody"
    assert research._parse_research_closed(content) is False


@pytest.fixture
def tmp_topics_dir(tmp_path, monkeypatch):
    topics = tmp_path / "topics"
    topics.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    return topics


def _write_note(topics_dir, filename, title, closed=False):
    fm = "research_closed: true\n" if closed else ""
    (topics_dir / filename).write_text(
        f"---\ntype: topic\ndate: 2026-07-01\n{fm}---\n\n# {title}\n\nBody text.\n",
        encoding="utf-8",
    )


def test_find_family_files_matches_shared_token(tmp_topics_dir):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl")
    _write_note(tmp_topics_dir, "frankl-my-position.md", "Frankl: My Position")
    _write_note(tmp_topics_dir, "albert-camus.md", "Albert Camus")
    files = set(research._find_family_files("Frankl"))
    assert files == {"viktor-frankl.md", "frankl-my-position.md"}


def test_find_family_files_exact_slug_match(tmp_topics_dir):
    _write_note(tmp_topics_dir, "albert-camus.md", "Albert Camus")
    assert research._find_family_files("Albert Camus") == ["albert-camus.md"]


def test_find_family_files_no_match_returns_empty(tmp_topics_dir):
    _write_note(tmp_topics_dir, "albert-camus.md", "Albert Camus")
    assert research._find_family_files("Nonexistent Subject") == []


def test_closed_family_conflict_detects_same_filename(tmp_topics_dir):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl", closed=True)
    hit = research._closed_family_conflict("Viktor Frankl", "viktor-frankl.md")
    assert hit == "viktor-frankl.md"


def test_closed_family_conflict_detects_new_file_matching_closed_family(tmp_topics_dir):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl", closed=True)
    # Picker proposes a "new" note under a different filename for the same subject.
    hit = research._closed_family_conflict("Frankl's Logotherapy Revisited", "frankl-s-logotherapy-revisited.md")
    assert hit == "viktor-frankl.md"


def test_closed_family_conflict_none_when_not_closed(tmp_topics_dir):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl", closed=False)
    assert research._closed_family_conflict("Viktor Frankl", "viktor-frankl.md") is None


def test_research_queue_close_sets_frontmatter_on_whole_family(tmp_topics_dir):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl")
    _write_note(tmp_topics_dir, "frankl-my-position.md", "Frankl: My Position")
    reply = asyncio.run(research.research_queue("close", subject="Frankl"))
    assert "Closed 2" in reply
    for fname in ("viktor-frankl.md", "frankl-my-position.md"):
        content = (tmp_topics_dir / fname).read_text(encoding="utf-8")
        assert research._parse_research_closed(content) is True


def test_research_queue_reopen_clears_frontmatter(tmp_topics_dir):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl", closed=True)
    reply = asyncio.run(research.research_queue("reopen", subject="Viktor Frankl"))
    assert "Reopened 1" in reply
    content = (tmp_topics_dir / "viktor-frankl.md").read_text(encoding="utf-8")
    assert research._parse_research_closed(content) is False


def test_research_queue_close_no_match(tmp_topics_dir):
    reply = asyncio.run(research.research_queue("close", subject="Nonexistent"))
    assert "No topic note found" in reply


def test_research_queue_close_rejects_short_subject(tmp_topics_dir):
    reply = asyncio.run(research.research_queue("close", subject=".."))
    assert "Error" in reply


def test_research_queue_status_lists_closed_topics(tmp_topics_dir, tmp_queue_file):
    _write_note(tmp_topics_dir, "viktor-frankl.md", "Viktor Frankl", closed=True)
    status = asyncio.run(research.research_queue("status"))
    assert "Closed to research" in status
    assert "viktor-frankl.md" in status


def _assert_no_thinking(extra_body, provider):
    """The cheap tier must disable reasoning; since 2026-09-21 the same body also
    carries the OpenRouter routing preference (router.with_provider_prefs), so the
    disable half is asserted exactly and the provider half alongside it.

    Provider-agnostic on purpose: Moonshot's disable key is `thinking`, OpenRouter's is
    `reasoning`, so this compares against whatever that provider's own no_thinking_body
    actually contains rather than hardcoding either spelling.
    """
    for k, v in provider.no_thinking_body.items():
        assert extra_body[k] == v, (k, extra_body.get(k), v)
    if provider.provider_prefs:
        assert extra_body["provider"] == provider.provider_prefs
    else:
        assert set(extra_body) == set(provider.no_thinking_body)


# ---------------------------------------------------------------------------
# Failure backoff (2026-09-28): topics/content-as-dosing.md failed five passes in a row
# because some failures left nothing behind and the note stayed the most overdue topic.
# ---------------------------------------------------------------------------

def test_failure_hold_grows_then_caps():
    hours = [research.failure_hold_s(n) / 3600 for n in (0, 1, 2, 3, 4, 9)]
    assert hours == [0, 24, 48, 96, 168, 168]


def test_in_failure_hold_is_pure_and_time_based():
    rec = {"fails": 2, "last": 1000.0}
    assert research.in_failure_hold(rec, 1000.0 + 47 * 3600)
    assert not research.in_failure_hold(rec, 1000.0 + 49 * 3600)
    assert not research.in_failure_hold(None, 1000.0)


def test_failed_passes_count_and_a_completed_one_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    for _ in range(3):
        research._note_pass_outcome("content-as-dosing.md", "failed: note has no traceable source", 5000.0)
    rec = research._load_state()[research._FAILURES_KEY]["content-as-dosing.md"]
    assert rec["fails"] == 3 and rec["last"] == 5000.0
    research._note_pass_outcome("content-as-dosing.md", "skipped: note changed during synthesis", 6000.0)
    assert research._load_state()[research._FAILURES_KEY]["content-as-dosing.md"]["fails"] == 3
    research._note_pass_outcome("content-as-dosing.md", "wrote topics/content-as-dosing.md — x", 7000.0)
    assert "content-as-dosing.md" not in research._load_state()[research._FAILURES_KEY]


def test_every_failure_path_in_tick_reports_its_note():
    """Each outcome of a pass carries the note it is about, or the bookkeeping cannot
    hold that note back -- the gap that let one note fail five times in a row."""
    import inspect
    src = inspect.getsource(research._tick)
    calls = src.count("_log_research_pass(")
    assert calls >= 8 and src.count('file=existing_file or f"{_slugify(subject)}.md"') == calls


def test_a_held_note_is_not_a_candidate(tmp_path, monkeypatch):
    topics = tmp_path / "topics"
    topics.mkdir()
    (topics / "content-as-dosing.md").write_text("# Content as dosing\nbody\n", encoding="utf-8")
    (topics / "stoicism.md").write_text("# Stoicism\nbody\n", encoding="utf-8")
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    from backend.brain import semindex, embeddings

    async def no_hits(*a, **k):
        return []

    async def no_vecs(texts):
        raise RuntimeError("no embedder in tests")
    monkeypatch.setattr(semindex, "search", no_hits)
    monkeypatch.setattr(embeddings, "embed_many", no_vecs)
    now = 1_800_000_000.0
    research._note_pass_outcome("content-as-dosing.md", "failed: all searches failed", now - 3600)
    files = [c["file"] for c in asyncio.run(research._gather_candidates(now))]
    assert files == ["stoicism.md"]
    files = [c["file"] for c in asyncio.run(research._gather_candidates(now + 2 * 86400))]
    assert sorted(files) == ["content-as-dosing.md", "stoicism.md"]
