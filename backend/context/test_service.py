"""A03 slice 2: the service reproduces today's prompt byte-for-byte.

The migration rule for this package: match current behavior first, so a change to what
JARVIS sees can never be an accident. These assert equality against
`personality.build_system_prompt` called directly, which is the thing `chat()` uses.
"""
import pytest

from backend.context import budget, profiles, service, sources


@pytest.fixture(autouse=True)
def _freeze_now_line(monkeypatch):
    """Pin the [Now] line for every prompt comparison in this module.

    `personality.build_system_prompt` injects `chronos.now_line()`, which has MINUTE
    granularity. These tests byte-compare two separate assemblies, so any of them fails
    if the minute happens to tick between the two calls -- a real intermittent failure
    observed in a full-suite run. The clock is not what these tests are about.
    """
    from backend.brain import chronos
    monkeypatch.setattr(chronos, "now_line",
                        lambda: "[Now] Monday 2026-01-01 00:00 (Europe/Paris)")


USER = "what did we decide about the stock positions?"

CONTENT = {
    profiles.OWNER: "Sam is direct and dislikes hedging.",
    profiles.QUEUE: "[open] 2026-09-01 — review the NVDA entry",
    profiles.TOPIC: "NVDA: bought at 118, thesis is datacenter demand.",
    profiles.LETTER: "You left the position sizing question unresolved.",
    profiles.FACTS: "- Sam holds NVDA and Take Two",
    profiles.SLEEP: "[Last sleep] 6h ago",
    profiles.EXCHANGE: "earlier: we discussed the entry price",
}


# ------------------------------------------------------------- byte-for-byte
def test_rendered_prompt_is_identical_to_calling_build_system_prompt():
    report = service.shadow_diff(USER, content=CONTENT)
    assert report["omissions"] == []
    assert report["identical"] is True, (
        f"stable_match={report['stable_match']} volatile_match={report['volatile_match']}"
    )


def test_identical_with_no_optional_content_at_all():
    """The cold-start shape: first turn of a fresh session, nothing retrieved yet."""
    report = service.shadow_diff(USER, content={})
    assert report["identical"] is True


def test_identical_for_each_source_in_isolation():
    """Catches a mis-mapped kwarg that a combined test could mask."""
    for source_id, text in CONTENT.items():
        report = service.shadow_diff(USER, content={source_id: text})
        assert report["identical"] is True, f"{source_id} renders differently"


def test_every_mapped_source_actually_reaches_the_prompt():
    """A source_id mapped to the wrong kwarg would still render 'identically' if the
    kwarg were ignored by both paths -- so check the text really appears."""
    snap = service.build_foreground(USER, **CONTENT)
    stable, volatile = service.render_foreground(snap, USER)
    rendered = stable + volatile
    for source_id, text in CONTENT.items():
        assert text in rendered, f"{source_id} content never reached the prompt"


# ------------------------------------------------------------------ budgeting
def test_dropped_source_is_absent_and_explained():
    """When the budget does drop something, the difference is exactly that absence."""
    content = dict(CONTENT)
    content[profiles.TOPIC] = "z" * 200000          # far too large to fit
    snap = service.build_foreground(USER, **content)

    assert [o.source_id for o in snap.omissions] == [profiles.TOPIC]
    assert snap.omissions[0].reason == "budget-exhausted"

    stable, volatile = service.render_foreground(snap, USER)
    assert "z" * 200000 not in (stable + volatile)
    # ...and the sources that DID fit are still there.
    assert CONTENT[profiles.OWNER] in (stable + volatile)


def test_user_request_is_not_droppable():
    snap = service.build_foreground(USER, **CONTENT)
    request = [f for f in snap.fragments if f.source_id == profiles.USER_REQUEST]
    assert request and request[0].droppable is False


def test_profile_filters_out_a_source_foreground_should_not_see():
    """build_foreground only accepts known foreground sources; anything else is a
    programming error, not a silent drop."""
    with pytest.raises(KeyError, match="unknown context source"):
        service.build_foreground(USER, **{"evidence:search": "web text"})


def test_empty_strings_do_not_become_fragments():
    snap = service.build_foreground(USER, **{profiles.OWNER: "", profiles.QUEUE: "  "})
    assert profiles.OWNER not in snap.source_ids()
    assert profiles.QUEUE not in snap.source_ids()


def test_manifest_reports_what_was_used_without_the_text():
    snap = service.build_foreground(USER, **CONTENT)
    manifest = snap.manifest()
    assert CONTENT[profiles.OWNER] not in repr(manifest)
    assert profiles.OWNER in [s["id"] for s in manifest["sources"]]
    assert manifest["token_estimate"] > 0


def test_shadow_diff_reports_size_for_comparison():
    report = service.shadow_diff(USER, content=CONTENT)
    assert report["old_chars"] == report["new_chars"]
    assert report["token_estimate"] > 0


# ----------------------------------------------------------------- task brief
def test_task_brief_carries_goal_constraints_and_evidence():
    """The roadmap's headline A03 motivation: a background task should know why it
    exists, instead of receiving one sentence."""
    brief = service.build_task_brief(
        objective="Compare NVDA and AMD datacenter revenue",
        origin_request="is my NVDA thesis still intact?",
        constraints="Do not send email. Cite sources with dates.",
        evidence="topics/nvda.md says entry at 118",
        completion="A written comparison saved to the vault",
    )
    for expected in ("OBJECTIVE", "ORIGINATING REQUEST", "CONSTRAINTS ACCEPTED",
                     "EVIDENCE ALREADY GATHERED", "DONE WHEN"):
        assert expected in brief
    assert "Do not send email" in brief
    assert "entry at 118" in brief


def test_task_brief_omits_empty_sections():
    brief = service.build_task_brief(objective="Just do the thing")
    assert brief.startswith("OBJECTIVE")
    assert "CONSTRAINTS" not in brief
    assert "EVIDENCE" not in brief


def test_task_brief_is_plain_text_for_a_prompt():
    """background._BG_SYSTEM expects text; a structured object would need a consumer
    that does not exist yet."""
    assert isinstance(service.build_task_brief(objective="x"), str)
