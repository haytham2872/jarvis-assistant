"""A03 slice 1: source inventory, whole-request budget, workload profiles.

Nothing here is wired into a live turn yet. The roadmap's own migration guidance is to
"start by matching existing foreground content in offline shadow builds, then enable
budgeting/profile improvements against representative continuity cases" — so this slice
builds the machinery and proves it reproduces today's content, rather than changing what
JARVIS sees.
"""
import pytest

from backend.context import budget, profiles, sources


def frag(sid, cls=sources.MEMORY, text="x", **kw):
    return sources.fragment(sid, cls, text, **kw)


# ------------------------------------------------------------------- fragments
def test_empty_sources_do_not_become_fragments():
    """No letter yet, no relevant skill: a normal outcome, not a considered-and-dropped
    one. It must not occupy a slot or appear in the manifest."""
    assert sources.fragment("memory:letter", sources.MEMORY, "") is None
    assert sources.fragment("memory:letter", sources.MEMORY, "   \n ") is None
    assert sources.fragment("memory:letter", sources.MEMORY, "real") is not None


def test_manifest_carries_provenance_without_content():
    """Ordinary observability needs metadata, not private text dumps."""
    snap = sources.Snapshot(profile="foreground", fragments=[
        sources.Fragment("memory:owner", sources.MEMORY, "SECRET PORTRAIT", version="h1"),
        sources.Fragment("evidence:search", sources.EVIDENCE, "web text", trusted=False),
    ], token_estimate=42)
    manifest = snap.manifest()
    blob = repr(manifest)
    assert "SECRET PORTRAIT" not in blob and "web text" not in blob
    assert manifest["sources"][0]["version"] == "h1"
    assert manifest["sources"][0]["class"] == "memory"
    assert manifest["sources"][1]["trusted"] is False
    assert manifest["token_estimate"] == 42


# ---------------------------------------------------------------------- budget
def test_token_estimate_uses_the_measured_ratio():
    """3.82 chars/token is measured against the real tokenizer, not guessed -- see
    scripts/measure_note_token_ratio.py."""
    assert budget.CHARS_PER_TOKEN == 3.82
    assert budget.estimate_tokens("") == 0
    assert budget.estimate_tokens("a" * 382) == 101      # 100 + 1 rounding


def test_estimate_is_conservative_rather_than_optimistic():
    """Overestimating spends the budget early; underestimating gets the request
    rejected by the provider. The worst-case ratio is the deliberate choice."""
    text = "# Heading with **bold** and [[wiki-links]] — em-dashes" * 20
    assert budget.estimate_tokens(text) >= len(text) / 4.45   # denser than the average


def test_reservation_covers_the_whole_request_not_just_context():
    r = budget.DEFAULT_RESERVATION
    assert r.total == r.tools + r.messages + r.answer + r.reasoning + r.margin
    assert r.answer > 0 and r.tools > 0 and r.margin > 0


def test_everything_fits_when_there_is_room():
    cands = [frag("a"), frag("b"), frag("c")]
    result = budget.select(cands)
    assert [f.source_id for f in result.selected] == ["a", "b", "c"]
    assert result.omissions == []


def test_lower_precedence_is_dropped_first():
    big = "x" * 4000
    cands = [
        frag("evidence", sources.EVIDENCE, big),
        frag("memory", sources.MEMORY, big),
        frag("instruction", sources.INSTRUCTION, big),
    ]
    # Room for roughly two of the three.
    result = budget.select(cands, context_window=budget.DEFAULT_RESERVATION.total + 2100)
    kept = [f.source_id for f in result.selected]
    assert "instruction" in kept
    assert "evidence" not in kept, "lowest precedence should go first"
    assert [o.reason for o in result.omissions] == ["budget-exhausted"]


def test_a_large_drop_does_not_starve_smaller_later_sources():
    """Skipping rather than truncating, and continuing rather than stopping: losing one
    big optional note must not also cost three small ones."""
    cands = [
        frag("huge", sources.MEMORY, "x" * 50000),
        frag("small1", sources.MEMORY, "a"),
        frag("small2", sources.MEMORY, "b"),
    ]
    result = budget.select(cands, context_window=budget.DEFAULT_RESERVATION.total + 500)
    kept = [f.source_id for f in result.selected]
    assert kept == ["small1", "small2"]
    assert [o.source_id for o in result.omissions] == ["huge"]


def test_mandatory_overflow_is_explicit_not_silent():
    """The roadmap: mandatory content that alone exceeds the budget must be reported,
    never silently trimmed."""
    cands = [sources.Fragment("instruction:core", sources.INSTRUCTION,
                              "x" * 500000, droppable=False)]
    result = budget.select(cands, context_window=budget.DEFAULT_RESERVATION.total + 100)
    assert result.overflowed is True
    assert result.selected[0].source_id == "instruction:core"   # still included


def test_selection_preserves_the_callers_rendering_order():
    """Precedence decides what survives; it must not reorder the prompt, which would
    change the cached stable prefix."""
    cands = [frag("memory", sources.MEMORY), frag("core", sources.INSTRUCTION)]
    result = budget.select(cands)
    assert [f.source_id for f in result.selected] == ["memory", "core"]


def test_build_snapshot_ignores_none_candidates():
    snap = budget.build_snapshot("foreground", [frag("a"), None, frag("b")])
    assert snap.source_ids() == ["a", "b"]
    assert snap.profile == "foreground"
    assert snap.token_estimate > 0


# -------------------------------------------------------------------- profiles
def test_every_profile_is_retrievable_by_name():
    for name in ("foreground", "planning", "background", "research",
                 "consolidation", "rumination", "brief", "extraction"):
        assert profiles.get(name).name == name
    with pytest.raises(KeyError):
        profiles.get("nope")


def test_profiles_never_both_allow_and_exclude_a_source():
    for profile in profiles.ALL.values():
        overlap = (set(profile.required) | set(profile.optional)) & set(profile.excluded)
        assert not overlap, f"{profile.name} both allows and excludes {overlap}"


def test_extraction_cannot_see_conversation_or_identity():
    """Extraction feeds long-term memory. The roadmap: "No raw reasoning or unrelated
    retrieved material"; a model's deliberation must never become a stored user fact."""
    p = profiles.EXTRACTION
    for banned in (profiles.EXCHANGE, profiles.HISTORY_BRIDGE, profiles.SELF_MODEL,
                   profiles.SEARCH_EVIDENCE, profiles.CORE):
        assert not p.allows(banned), f"extraction must not see {banned}"


def test_background_gets_a_brief_not_a_conversation_dump():
    p = profiles.BACKGROUND
    assert profiles.TASK_BRIEF in p.required
    assert not p.allows(profiles.EXCHANGE)
    assert not p.allows(profiles.HISTORY_BRIDGE)


def test_planning_keeps_goal_and_plan_but_drops_identity_layers():
    p = profiles.PLANNING
    assert p.allows(profiles.PLAN) and p.allows(profiles.QUEUE)
    assert not p.allows(profiles.SELF_MODEL)
    assert not p.allows(profiles.MOOD)


def test_filter_drops_disallowed_sources_and_keeps_order():
    p = profiles.RESEARCH
    frags = [frag(profiles.TOPIC), frag(profiles.EXCHANGE), frag(profiles.NOW)]
    kept = [f.source_id for f in p.filter(frags)]
    assert kept == [profiles.TOPIC, profiles.NOW]


def test_missing_required_sources_are_reported():
    p = profiles.BACKGROUND
    assert p.missing([frag(profiles.NOW)]) == [profiles.TASK_BRIEF]
    assert p.missing([frag(profiles.TASK_BRIEF)]) == []


# -------------------------------------------- shadow: foreground matches today
def test_foreground_profile_covers_every_source_build_system_prompt_uses():
    """The migration rule: match current behavior first, so a personality change can
    never happen by accident. Every block `build_system_prompt` can inject must have a
    home in the foreground profile -- otherwise routing a turn through this service
    would silently drop it.
    """
    import inspect

    from backend.brain import personality

    signature = inspect.signature(personality.build_system_prompt)
    # Parameters that carry prompt CONTENT (the rest are control/compat).
    content_params = {
        "facts": profiles.FACTS,
        "sleep_info": profiles.SLEEP,
        "owner_md": profiles.OWNER,
        "vault_letter": profiles.LETTER,
        "intent": profiles.QUEUE,
        "relevant_topic": profiles.TOPIC,
        "vault_passage": profiles.TOPIC,
        "exchange": profiles.EXCHANGE,
        "pending_predictions": profiles.PREDICTIONS,
        "portfolio_context": profiles.PORTFOLIO,
        "history_bridge": profiles.HISTORY_BRIDGE,
    }
    for param, source_id in content_params.items():
        assert param in signature.parameters, (
            f"build_system_prompt lost the {param!r} parameter -- update this mapping "
            "so the foreground profile stays an accurate description of it"
        )
        assert profiles.FOREGROUND.allows(source_id), (
            f"{param!r} has no home in the foreground profile ({source_id})"
        )


def test_foreground_allows_the_blocks_injected_by_trigger():
    """Positions, calibration and the self-model digest are trigger-gated rather than
    always present, but they are still foreground content."""
    for source_id in (profiles.POSITIONS, profiles.SELF_MODEL, profiles.EVOLUTION, profiles.MOOD,
                      profiles.RUNTIME_IDENTITY, profiles.RUNTIME_STATUS, profiles.NOW,
                      profiles.VAULT_SUMMARY):
        assert profiles.FOREGROUND.allows(source_id)
