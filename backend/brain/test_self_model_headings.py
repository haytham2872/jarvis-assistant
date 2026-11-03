"""Heading resolution for the self-model digest.

`_find_heading` tolerates small heading edits by substring match in either direction,
but took the FIRST match -- so `## Architecture`, near the top of CLAUDE.md, captured
"Architecture Roadmap — review pass" and would have injected the wrong section.
"""
from backend.brain import self_model as sm


def test_a_heading_that_extends_the_name_beats_a_shorter_one_found_earlier():
    parsed = {"Architecture": "top-level diagram",
              "Architecture Roadmap — review pass (2026-09-22)": "the review"}
    assert sm._find_heading(parsed, "Architecture Roadmap — review pass") == \
        "Architecture Roadmap — review pass (2026-09-22)"


def test_the_shorter_fallback_picks_the_most_specific_heading():
    """Renamed-shorter headings still resolve, to the closest one, not the first."""
    parsed = {"Neural": "vague", "Neural sphere": "the right one"}
    assert sm._find_heading(parsed, "UI States — Neural Sphere") == "Neural sphere"


def test_an_exact_match_always_wins():
    parsed = {"Vault tools extended": "x", "Vault": "exact"}
    assert sm._find_heading(parsed, "Vault") == "Vault"


def test_every_mapped_heading_resolves_in_the_real_claude_md():
    """A mapping that resolves to nothing is a section JARVIS silently stops knowing."""
    parsed = sm._parse_claude_md()
    missing = [(sec, w) for sec, wants in sm._SECTION_HEADINGS.items()
               for w in wants if sm._find_heading(parsed, w) is None]
    assert not missing, f"unresolved self-model headings: {missing}"


def test_no_real_mapping_is_captured_by_the_top_level_architecture_heading():
    parsed = sm._parse_claude_md()
    for sec, wants in sm._SECTION_HEADINGS.items():
        for w in wants:
            if sm._normalise(w) != "architecture":
                assert sm._find_heading(parsed, w) != "Architecture", (sec, w)


def test_live_digest_uses_current_contract_instead_of_historical_first_slices():
    digest = sm._build_digest_from_claudemd()
    assert set(digest) == set(sm._SECTION_HEADINGS)
    assert "no shell, terminal, Git" in digest["tools"]
    assert "no terminal" in digest["personality"]
    assert "canonical exchanges" in digest["memory"]
    assert "Linux backend reached through Discord" in digest["main"]
    assert "claude-haiku" not in digest["memory"]
    assert "screen/computer control" not in digest["main"]
