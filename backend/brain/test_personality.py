"""Tests for backend/brain/personality.py — self-model + runtime-status
trigger detection.

Scoped to the "runtime" (vital signs) trigger added alongside proprioception,
since the existing _SECTION_TRIGGERS/_SELF_PHRASES matching already has no
dedicated test file and is exercised indirectly elsewhere. Run with:
    .venv\\Scripts\\pytest backend/brain/test_personality.py -v
"""

import pytest

from backend.brain import personality


# ---------------------------------------------------------------------------
# _RUNTIME_TRIGGERS_RE — pure pattern matching
# ---------------------------------------------------------------------------

def test_runtime_trigger_matches_canonical_phrase():
    assert personality._RUNTIME_TRIGGERS_RE.search("how are you running right now?")


def test_runtime_trigger_matches_cost_question():
    assert personality._RUNTIME_TRIGGERS_RE.search("what's your session cost so far")


def test_runtime_trigger_matches_uptime_question():
    assert personality._RUNTIME_TRIGGERS_RE.search("what's your uptime been like")


def test_runtime_trigger_no_false_positive_on_unrelated_status_word():
    # "status" alone is an everyday word ("what's the status of my order") --
    # only the specific phrases in _RUNTIME_TRIGGERS should match.
    assert not personality._RUNTIME_TRIGGERS_RE.search("what is the status of my order")


def test_runtime_trigger_no_false_positive_on_unrelated_cost_word():
    assert not personality._RUNTIME_TRIGGERS_RE.search("how much did that cost you emotionally")


def test_runtime_trigger_no_false_positive_on_health_smalltalk():
    assert not personality._RUNTIME_TRIGGERS_RE.search("hope you're staying healthy")


# ---------------------------------------------------------------------------
# _detect_self_model_sections — runtime block injection, independent of the
# self-model digest's readiness (the whole reason this trigger is handled
# separately from _SECTION_TRIGGERS below is that it's live process state,
# not a CLAUDE.md slice).
# ---------------------------------------------------------------------------

def test_runtime_identity_reaches_an_ordinary_turn(monkeypatch):
    from backend.brain import runtime_status

    monkeypatch.setattr(runtime_status.platform, "system", lambda: "Linux")
    _stable, volatile = personality.build_system_prompt("hello")
    assert "[Runtime identity]" in volatile
    assert "Host: Linux" in volatile
    assert f"Model: {runtime_status.OPENROUTER_STANDARD} via OpenRouter" in volatile
    assert "[Runtime status]" not in volatile


def test_runtime_trigger_injects_status_block(monkeypatch):
    monkeypatch.setattr(
        "backend.brain.runtime_status.runtime_status_text",
        lambda: "[Runtime status]\nfake status block",
    )
    result = personality._detect_self_model_sections("how are you running right now?")
    assert "[Runtime status]" in result
    assert "fake status block" in result


# ---------------------------------------------------------------------------
# _OPINION_TRIGGERS_RE / _CALIBRATION_TRIGGERS_RE — pure pattern matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what do you think about free will",
    "your position on nietzsche",
    "do you believe in god",
    "have you changed your mind about camus",
    "what's your take on this",
    "where do you stand on suffering",
])
def test_opinion_trigger_matches(text):
    assert personality._OPINION_TRIGGERS_RE.search(text.lower())


@pytest.mark.parametrize("text", [
    "I think we should go out tonight",
    "thinking about lunch",
    "I believe it's Tuesday",
    "what's the weather like",
])
def test_opinion_trigger_no_false_positive(text):
    assert not personality._OPINION_TRIGGERS_RE.search(text.lower())


# ---------------------------------------------------------------------------
# is_persistence_request() / _PERSISTENCE_TRIGGERS_RE — anti-confabulation
# guard (2026-09-08 FOMC scheduling-failure fix)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "schedule a task to report the FOMC decision after the meeting",
    "add that to the queue for me",
    "put this in the queue please",
    "remind me to call the bank tomorrow at 10",
    "save that note for later",
    "write that down",
    "queue a night task to research this",
    "add that to notion",
    "add this to my ideas",
    "add a task to renew the car insurance",
    "put this on my to-do list",
    "mark that as done",
    "check that off",
])
def test_persistence_trigger_matches(text):
    assert personality.is_persistence_request(text)


@pytest.mark.parametrize("text", [
    "what's in the queue right now",
    "I have a scheduling conflict tomorrow",
    "what time is it",
    "tell me about the FOMC meeting",
    "what's on my Notion to-do list",
    "how does the check work",
])
def test_persistence_trigger_no_false_positive(text):
    assert not personality.is_persistence_request(text)


def test_build_system_prompt_injects_persistence_frame_on_trigger():
    stable, volatile = personality.build_system_prompt("schedule that for tonight")
    assert "Persistence check" in volatile


def test_build_system_prompt_no_persistence_frame_without_trigger():
    stable, volatile = personality.build_system_prompt("what's the weather like")
    assert "Persistence check" not in volatile


# ---------------------------------------------------------------------------
# is_recall_precision_request() / _RECALL_PRECISION_TRIGGERS_RE — nudge to
# actively check memory tools rather than guess a previously-stated detail
# (2026-09-09, complementary to the semindex.py passive-recall floor fix)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "like I said, I have about 540 euros in it",
    "as I said, it rose since june",
    "I already told you the amount",
    "what did I say the number was",
    "the amount I gave you earlier",
    "how much did I say I put in",
    "I said earlier it was around 500",
])
def test_recall_precision_trigger_matches(text):
    assert personality.is_recall_precision_request(text)


@pytest.mark.parametrize("text", [
    "I said hello to my neighbor today",
    "what's the amount of rain expected tomorrow",
    "tell me a number between one and ten",
    "what did you say your name was",
])
def test_recall_precision_trigger_no_false_positive(text):
    assert not personality.is_recall_precision_request(text)


def test_build_system_prompt_injects_recall_precision_frame_when_recall_empty():
    stable, volatile = personality.build_system_prompt(
        "like I said, what was the amount again",
        vault_passage="", exchange="",
    )
    assert "Recall check" in volatile


def test_build_system_prompt_no_recall_precision_frame_when_exchange_present():
    # Passive recall already found something relevant -- no need to nudge
    # toward an active tool call on top of it.
    stable, volatile = personality.build_system_prompt(
        "like I said, what was the amount again",
        exchange="past exchange text with the real number",
    )
    assert "Recall check" not in volatile


def test_build_system_prompt_no_recall_precision_frame_when_vault_passage_present():
    stable, volatile = personality.build_system_prompt(
        "like I said, what was the amount again",
        vault_passage="relevant vault passage",
    )
    assert "Recall check" not in volatile


def test_build_system_prompt_no_recall_precision_frame_without_trigger():
    stable, volatile = personality.build_system_prompt(
        "what's the weather like", vault_passage="", exchange="",
    )
    assert "Recall check" not in volatile


def test_calibration_trigger_matches():
    assert personality._CALIBRATION_TRIGGERS_RE.search("how well do you know me")


def test_calibration_trigger_no_false_positive_on_unrelated_knowledge_question():
    assert not personality._CALIBRATION_TRIGGERS_RE.search("do you know the capital of Peru")


# ---------------------------------------------------------------------------
# build_system_prompt — positions / predictions / calibration injection
# ---------------------------------------------------------------------------

def test_build_system_prompt_injects_pending_predictions_when_present():
    _, volatile = personality.build_system_prompt(
        "how's it going", pending_predictions="1. (made 2026-07-14) He'll bring up X."
    )
    assert "He'll bring up X." in volatile
    assert personality._PREDICTIONS_FRAME in volatile


def test_build_system_prompt_omits_predictions_block_when_empty():
    _, volatile = personality.build_system_prompt("how's it going", pending_predictions="")
    assert personality._PREDICTIONS_FRAME not in volatile


def test_build_system_prompt_injects_positions_on_trigger(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "positions.md").write_text("## nietzsche\n**Stance:** ...", encoding="utf-8")

    _, volatile = personality.build_system_prompt("what do you think about nietzsche")
    assert "nietzsche" in volatile
    assert personality._POSITIONS_FRAME in volatile


def test_build_system_prompt_no_positions_block_without_trigger(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "positions.md").write_text("## nietzsche\n**Stance:** ...", encoding="utf-8")

    _, volatile = personality.build_system_prompt("what's the weather like")
    assert personality._POSITIONS_FRAME not in volatile


def test_build_system_prompt_positions_trigger_but_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    # self/ exists but positions.md does not
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)

    _, volatile = personality.build_system_prompt("what do you think about nietzsche")
    assert personality._POSITIONS_FRAME not in volatile


# ---------------------------------------------------------------------------
# build_system_prompt — history_bridge injection (bridges the 40-turn cliff,
# see backend/brain/history_bridge.py)
# ---------------------------------------------------------------------------

def test_build_system_prompt_injects_history_bridge_when_present():
    _, volatile = personality.build_system_prompt(
        "what were we just talking about",
        history_bridge="User: earlier question | JARVIS: earlier answer",
    )
    assert "earlier question" in volatile
    assert personality._HISTORY_BRIDGE_FRAME in volatile


def test_build_system_prompt_omits_history_bridge_when_empty():
    _, volatile = personality.build_system_prompt("hello", history_bridge="")
    assert personality._HISTORY_BRIDGE_FRAME not in volatile


def test_runtime_trigger_works_even_when_self_model_not_ready(monkeypatch):
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    monkeypatch.setattr(
        "backend.brain.runtime_status.runtime_status_text",
        lambda: "[Runtime status]\nfake status block",
    )
    result = personality._detect_self_model_sections("what's your uptime")
    assert "[Runtime status]" in result


def test_no_trigger_returns_empty_string(monkeypatch):
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    result = personality._detect_self_model_sections("what's the weather like today")
    assert result == ""


def test_runtime_status_failure_degrades_silently(monkeypatch):
    def _broken():
        raise RuntimeError("boom")
    monkeypatch.setattr("backend.brain.runtime_status.runtime_status_text", _broken)
    result = personality._detect_self_model_sections("how are you running right now?")
    assert "[Runtime status]" not in result   # degrades to nothing, doesn't raise


# ---------------------------------------------------------------------------
# Semantic gate wiring — intent.IntentResult as an OR'd second signal.
# Every trigger below must fire on a semantic-only hit (no keyword overlap at
# all -- the exact case the whole rewrite exists for) AND must not regress
# the pure-keyword path when intent_result is None (embedder cold/unavailable).
# ---------------------------------------------------------------------------

from backend.brain.intent import IntentResult


def _result(**scores) -> IntentResult:
    return IntentResult(scores=scores, query_vec=[])


def test_runtime_trigger_fires_on_semantic_hit_with_no_keyword_overlap(monkeypatch):
    monkeypatch.setattr(
        "backend.brain.runtime_status.runtime_status_text",
        lambda: "[Runtime status]\nfake status block",
    )
    text = "everything smooth on your end"   # shares no _RUNTIME_TRIGGERS phrase
    assert not personality._RUNTIME_TRIGGERS_RE.search(text.lower())
    result = personality._detect_self_model_sections(text, _result(runtime=0.90))
    assert "[Runtime status]" in result


def test_self_reflection_fires_on_semantic_hit_and_still_includes_a_real_section_match(monkeypatch):
    # Hardening (2026-09-09): this used to assert the OPPOSITE -- that a
    # genuine section match ("tools" here) got silently dropped whenever
    # self_reflection fired. That was the exact live bug (see
    # _detect_self_model_sections' 2026-09-09 comment): "self model" is a
    # bare _SELF_PHRASES entry, so any message containing it short-circuited
    # to personality-only even when the message also clearly asked about a
    # specific section (rumination -> memory, in the real incident). Fixed
    # so self_hit always includes personality (a generic self-model ask
    # should surface identity) but no longer discards a real co-occurring
    # section match.
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda s: f"[[digest for {s}]]")
    monkeypatch.setattr(personality, "_load_self_vault_files", lambda: "")
    text = "what is it like being what you are"   # no _SELF_PHRASES overlap
    result = personality._detect_self_model_sections(
        text, _result(self_reflection=0.95, **{"sm:tools": 0.90})
    )
    assert "digest for personality" in result
    assert "digest for tools" in result


def test_self_reflection_with_no_extra_section_match_stays_personality_only(monkeypatch):
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda s: f"[[digest for {s}]]")
    monkeypatch.setattr(personality, "_load_self_vault_files", lambda: "")
    text = "who are you, really"   # generic identity question, no specific-section signal
    result = personality._detect_self_model_sections(text, _result(self_reflection=0.95))
    assert "digest for personality" in result
    assert "digest for tools" not in result
    assert "digest for memory" not in result


def test_self_hit_keyword_alongside_specific_section_keyword_includes_both(monkeypatch):
    # The exact shape of the live incident: a message containing the bare
    # phrase "self model" (a _SELF_PHRASES entry) that ALSO names a specific,
    # real section topic ("rumination", a real _SECTION_TRIGGERS["memory"]
    # keyword) must get both, not just personality.
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda s: f"[[digest for {s}]]")
    monkeypatch.setattr(personality, "_load_self_vault_files", lambda: "")
    text = "what happens when rumination launches at night? check your code graph and your self model"
    result = personality._detect_self_model_sections(text)
    assert "digest for personality" in result
    assert "digest for memory" in result


def test_section_route_fires_on_semantic_hit_alone(monkeypatch):
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda s: f"[[digest for {s}]]")
    text = "what's rattling around in that head of yours"   # no keyword overlap
    for section, pattern in personality._SECTION_TRIGGER_RES:
        assert not pattern.search(text.lower())
    result = personality._detect_self_model_sections(text, _result(**{"sm:memory": 0.90}))
    assert "digest for memory" in result


def test_keyword_and_semantic_sections_are_unioned_and_capped(monkeypatch):
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda s: f"[[digest for {s}]]")
    # "voice" is a keyword hit for tts; semantic supplies stt and router on top.
    result = personality._detect_self_model_sections(
        "what about your voice", _result(**{"sm:stt": 0.95, "sm:router": 0.93})
    )
    sections_present = [s for s in ("tts", "stt", "router") if f"digest for {s}" in result]
    assert len(sections_present) == personality._MAX_SELF_SECTIONS


def test_none_result_falls_back_to_pure_keyword_behavior(monkeypatch):
    """The exact pre-rewrite call shape must still work -- callers with a
    cold embedder pass no result at all."""
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    result = personality._detect_self_model_sections("what's the weather like today", None)
    assert result == ""


def test_opinion_fires_on_semantic_hit_with_no_keyword_overlap(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "positions.md").write_text("## camus\n**Stance:** ...", encoding="utf-8")
    text = "does that sit right with you"   # no _OPINION_TRIGGERS overlap
    assert not personality._OPINION_TRIGGERS_RE.search(text.lower())
    _, volatile = personality.build_system_prompt(text, intent_result=_result(opinion=0.90))
    assert personality._POSITIONS_FRAME in volatile


def test_calibration_fires_on_semantic_hit_with_no_keyword_overlap(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "people").mkdir(parents=True, exist_ok=True)
    (tmp_path / "people" / "predictions.md").write_text(
        "<!-- calibration: right=3 wrong=1 unclear=0 -->\n", encoding="utf-8",
    )
    text = "am I an open book to you"   # no _CALIBRATION_TRIGGERS overlap
    assert not personality._CALIBRATION_TRIGGERS_RE.search(text.lower())
    _, volatile = personality.build_system_prompt(text, intent_result=_result(calibration=0.90))
    assert personality._CALIBRATION_FRAME in volatile


def test_build_system_prompt_none_intent_result_is_backward_compatible():
    """The default (no intent_result kwarg at all) must behave exactly like
    the pre-rewrite signature -- every existing caller omits it."""
    _, volatile = personality.build_system_prompt("what's the weather like")
    assert personality._POSITIONS_FRAME not in volatile
    assert personality._CALIBRATION_FRAME not in volatile


# ---------------------------------------------------------------------------
# "What's new" -> evolution.md — added after a real incident (2026-08-28):
# asked about a feature added earlier the same day, JARVIS had no dated
# record to check and confabulated a false "I've always had this" timeline.
# ---------------------------------------------------------------------------

def test_whats_new_trigger_matches_canonical_phrases():
    for phrase in ["what's new", "anything new", "what changed", "how long have you had this",
                   "tell me the latest updates you just pushed to your branch"]:
        assert personality._WHATS_NEW_TRIGGERS_RE.search(phrase.lower()), phrase


def test_whats_new_no_false_positive_on_unrelated_new():
    # "new" alone is an everyday word ("I bought a new phone") -- only the
    # specific phrases in _WHATS_NEW_TRIGGERS should match.
    assert not personality._WHATS_NEW_TRIGGERS_RE.search("I bought a new phone today")


def test_whats_new_injects_evolution_md_keyword_only(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    monkeypatch.setattr(personality, "is_ready", lambda: False)   # must work even before digest is ready
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text(
        "## 2026-08-27 — v1.18.0\n- code_graph tool added\n", encoding="utf-8",
    )
    result = personality._detect_self_model_sections("what's new with you?")
    assert "code_graph tool added" in result
    assert "you cannot run Git commands" in result
    assert personality._read_self_file.__name__ == "_read_self_file"   # sanity: helper exists


def test_just_pushed_question_uses_evolution_even_before_self_model_warms(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    (tmp_path / "self").mkdir(parents=True)
    (tmp_path / "self" / "evolution.md").write_text(
        "## 2026-09-23 — v1.27.0\n- `abc12345` Fix the update reader\n",
        encoding="utf-8",
    )
    _, volatile = personality.build_system_prompt(
        "Tell me about the latest updates you just pushed to your branch"
    )
    assert "Fix the update reader" in volatile
    assert "you cannot run Git commands" in volatile


def test_whats_new_fires_on_semantic_hit_with_no_keyword_overlap(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text("## dated entry\n- real change\n", encoding="utf-8")

    text = "how long have you had this ability"   # matches _WHATS_NEW_TRIGGERS too (edge overlap
                                                     # is fine); confirm semantic-only path also works
    from backend.brain.intent import IntentResult
    result_no_kw = personality._detect_self_model_sections(
        "did you get upgraded recently",   # no literal keyword overlap
        IntentResult(scores={"whats_new": 0.95}, query_vec=[]),
    )
    assert "real change" in result_no_kw


def test_whats_new_empty_file_produces_no_block(monkeypatch, tmp_path):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    # self/ exists but evolution.md does not
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    result = personality._detect_self_model_sections("what's new with you?")
    assert "cannot verify the latest changes yet" in result
    assert "[Self: evolution]" not in result


def test_whats_new_is_additive_not_exclusive(monkeypatch, tmp_path):
    """Unlike self_hit's short-circuit, whats_new must not prevent a
    section-specific match from also being included."""
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    monkeypatch.setattr(personality, "is_ready", lambda: True)
    monkeypatch.setattr(personality, "get_section", lambda s: f"[[digest for {s}]]")
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text("- a real change\n", encoding="utf-8")

    # "what's new" + a tools-section keyword ("capabilities") in one message
    result = personality._detect_self_model_sections("what's new — what are your capabilities now?")
    assert "a real change" in result
    assert "digest for tools" in result


def test_whats_new_works_even_when_digest_not_ready(monkeypatch, tmp_path):
    """evolution.md is a plain vault read, not digest-cache-dependent --
    same independence-from-is_ready() property as the runtime block."""
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    monkeypatch.setattr(personality, "is_ready", lambda: False)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text("- pre-ready change\n", encoding="utf-8")
    result = personality._detect_self_model_sections("anything new?")
    assert "pre-ready change" in result


def test_read_self_file_returns_empty_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    assert personality._read_self_file("evolution.md", "Self: evolution") == ""


def test_read_self_file_caps_length(tmp_path, monkeypatch):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text("x" * 5000, encoding="utf-8")
    result = personality._read_self_file("evolution.md", "Self: evolution")
    assert len(result) < 3100   # 3000 content cap + "[label]\n" wrapper


def test_read_self_file_keeps_newest_entries_not_oldest(tmp_path, monkeypatch):
    """The live 2026-09-01 failure: evolution.md is append-only newest-LAST,
    and the old [:3000] head-slice cut away everything recent once the file
    outgrew the cap -- JARVIS's 'what's new' window ended at mid-July and he
    dated brand-new features to the newest dates he could still see. The cap
    must keep the tail (newest entries), dropping the oldest instead."""
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    old_entry = "## 2026-05-12 — oldest\n" + ("old filler line\n" * 200)
    new_entry = "## 2026-09-01 — newest\n- code_graph: added today\n"
    (tmp_path / "self" / "evolution.md").write_text(old_entry + new_entry, encoding="utf-8")

    result = personality._read_self_file("evolution.md", "Self: evolution")
    assert "2026-09-01" in result          # newest entry survives
    assert "code_graph: added today" in result
    assert "2026-05-12" not in result       # oldest is what gets dropped


def test_read_self_file_tail_starts_on_entry_boundary(tmp_path, monkeypatch):
    """The tail window must be aligned forward to the next '## ' heading so
    the injected block never opens mid-sentence of a half-visible entry."""
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    entries = "".join(
        f"## 2026-08-{d:02d} — entry {d}\n" + ("filler text line\n" * 20)
        for d in range(1, 20)
    )
    (tmp_path / "self" / "evolution.md").write_text(entries, encoding="utf-8")

    result = personality._read_self_file("evolution.md", "Self: evolution")
    # First content line after the [label] wrapper must be a heading, not filler.
    first_content_line = result.split("\n", 1)[1].split("\n", 1)[0]
    assert first_content_line.startswith("## "), f"window starts mid-entry: {first_content_line!r}"


def test_read_self_file_preserves_latest_date_when_one_entry_exceeds_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text(
        "## 2026-09-22 — older\n- prior change\n\n"
        "## 2026-09-23 — newest\n" + ("long reflection\n" * 250)
        + "- `abc12345` Most recent commit\n",
        encoding="utf-8",
    )

    result = personality._read_self_file("evolution.md", "Self: evolution")
    assert "## 2026-09-23 — newest" in result
    assert "Most recent commit" in result
    assert "2026-09-22" not in result
    assert len(result) < 3100


def test_load_self_vault_files_still_reads_both_files(tmp_path, monkeypatch):
    """Refactor regression check: _load_self_vault_files() must still
    combine evolution.md AND mood-patterns.md, unlike the new whats_new
    path which reads evolution.md alone."""
    monkeypatch.setattr(personality, "_VAULT", tmp_path)
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "evolution.md").write_text("evo content", encoding="utf-8")
    (tmp_path / "self" / "mood-patterns.md").write_text("mood content", encoding="utf-8")
    result = personality._load_self_vault_files()
    assert "evo content" in result
    assert "mood content" in result


# ---------------------------------------------------------------------------
# Step 5 (2026-09-09) -- invariant core refresh: the [Mood] label, the vault
# snapshot injection, and the tool-mention drift guard.
# ---------------------------------------------------------------------------

class _FakeMoodState:
    def __init__(self, tone="engaged", intensity=0.7, trajectory="stable"):
        self.tone = tone
        self.intensity = intensity
        self.trajectory = trajectory


def test_mood_nudge_labeled_mood_not_tonal_register(monkeypatch):
    """The invariant core's MEMORY AND CONTINUITY and MUSIC sections both
    tell him to read a "[Mood] line" -- the injected label has to match
    that string exactly or the instruction points at nothing."""
    monkeypatch.setattr(personality, "get_mood", lambda: _FakeMoodState())
    nudge = personality._mood_nudge()
    assert nudge.startswith("[Mood]")
    assert "[Tonal register]" not in nudge


def test_mood_nudge_below_noise_floor_is_empty(monkeypatch):
    monkeypatch.setattr(personality, "get_mood", lambda: _FakeMoodState(intensity=0.1))
    assert personality._mood_nudge() == ""


def test_invariant_core_references_mood_label_that_actually_exists():
    """Static wiring check: whatever string the core tells him to look for
    must be exactly what _mood_nudge() actually emits."""
    assert "[Mood]" in personality._INVARIANT_CORE


# ---------------------------------------------------------------------------
# Vault snapshot, every turn (5a). It lives in the VOLATILE block: it names the
# newest files per folder, so any note written anywhere changes it, and in the
# stable block that invalidated the cached prefix behind it several times a day.
# ---------------------------------------------------------------------------

def test_build_system_prompt_injects_vault_snapshot_into_volatile_block(monkeypatch):
    monkeypatch.setattr(personality, "vault_summary", lambda: "topics/ (3 files — newest: a.md)")
    stable, volatile = personality.build_system_prompt("hello")
    assert "topics/ (3 files" in volatile
    assert personality._VAULT_SUMMARY_FRAME in volatile


def test_a_new_vault_note_does_not_change_the_cached_block(monkeypatch):
    monkeypatch.setattr(personality, "vault_summary", lambda: "topics/ (3 files — newest: a.md)")
    before, _ = personality.build_system_prompt("hello")
    monkeypatch.setattr(personality, "vault_summary", lambda: "topics/ (4 files — newest: b.md)")
    after, _ = personality.build_system_prompt("hello")
    assert before == after


def test_build_system_prompt_omits_vault_snapshot_when_empty(monkeypatch):
    monkeypatch.setattr(personality, "vault_summary", lambda: "")
    stable, volatile = personality.build_system_prompt("hello")
    assert personality._VAULT_SUMMARY_FRAME not in stable + volatile


def test_build_system_prompt_survives_vault_summary_raising(monkeypatch):
    def _broken():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(personality, "vault_summary", _broken)
    stable, volatile = personality.build_system_prompt("hello")   # must not raise
    assert personality._VAULT_SUMMARY_FRAME not in stable + volatile


# ---------------------------------------------------------------------------
# Drift guard (5e) -- every registered tool must be named in the invariant
# core, or explicitly allowlisted as deliberately unmentioned. A warning in
# a passing suite is a warning nobody reads (that's precisely how the core
# drifted out of date the first time); this fails the build instead.
# ---------------------------------------------------------------------------

# Tools intentionally left out of the invariant core, with why. Empty today
# -- every registered tool is named by its exact snake_case name somewhere
# in _INVARIANT_CORE -- but the escape hatch exists for a future tool that
# genuinely shouldn't be advertised in the always-on prompt (e.g. something
# purely internal/diagnostic with no reason for him to reach for it
# unprompted). Adding a name here must come with a real reason, not just to
# silence this test.
_CORE_DELIBERATELY_UNMENTIONED_TOOLS: frozenset[str] = frozenset()


def test_every_registered_tool_is_named_in_the_core_or_allowlisted():
    from backend.tools.registry import TOOLS
    core = personality._INVARIANT_CORE
    missing = [
        t["name"] for t in TOOLS
        if t["name"] not in core
        and t["name"] not in _CORE_DELIBERATELY_UNMENTIONED_TOOLS
    ]
    assert missing == [], (
        f"tool(s) registered but never mentioned in the invariant core and not "
        f"allowlisted: {missing} -- either mention them by name in "
        f"personality._INVARIANT_CORE, or add them to "
        f"_CORE_DELIBERATELY_UNMENTIONED_TOOLS with a comment explaining why."
    )


def test_allowlist_names_are_actually_real_registered_tools():
    """Catches a stale allowlist entry left behind after a tool was renamed
    or removed -- it would otherwise silently stop covering anything."""
    from backend.tools.registry import TOOLS
    real_names = {t["name"] for t in TOOLS}
    stale = _CORE_DELIBERATELY_UNMENTIONED_TOOLS - real_names
    assert stale == set(), f"allowlist references tool(s) that no longer exist: {stale}"
