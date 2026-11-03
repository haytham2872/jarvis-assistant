"""Tests for vault_read's section/table-of-contents behavior (2026-09-06).

A long, section-structured note used to have to be read in full even for one
question about it -- research topics/ notes commonly run 3000-5000+ tokens.
vault_read now returns a table of contents for long, "## "-sectioned notes
by default, and can return just one section on request. Short/unsectioned
notes are unaffected. Real filesystem (tmp_path), no real Qdrant/embedder --
vault_read never indexes anything, only vault_write does.
"""

import asyncio

import pytest

from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    return tmp_path


def _write(tmp_vault, folder, filename, content):
    d = tmp_vault / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(content, encoding="utf-8")


_LONG_SECTIONED = (
    "---\ntype: topic\ndate: 2026-07-01\n---\n\n"
    "# A Long Note\n\nSome intro text here, before any section heading.\n\n"
    + "## Overview\n\n" + ("Overview content. " * 150) + "\n\n"
    + "## Perspectives & Counterpoints\n\n" + ("Counterpoint content. " * 150) + "\n\n"
    + "## Open questions\n\n- Question one\n- Question two\n"
)


def test_short_note_returns_in_full_with_no_section(tmp_vault):
    _write(tmp_vault, "people", "sam.md", "# Sam\n\nA short portrait.\n")
    result = run(vault.vault_read("sam.md", "people"))
    assert result == "# Sam\n\nA short portrait.\n"


def test_long_unsectioned_note_returns_in_full(tmp_vault):
    # Long but with fewer than _TOC_MIN_HEADINGS "## " headings -- no TOC gate.
    body = "# Letter\n\n" + ("Just prose, no sections. " * 400)
    _write(tmp_vault, "letters", "long.md", body)
    result = run(vault.vault_read("long.md", "letters"))
    assert result == body


def test_long_sectioned_note_returns_toc_by_default(tmp_vault):
    _write(tmp_vault, "topics", "long-topic.md", _LONG_SECTIONED)
    result = run(vault.vault_read("long-topic.md", "topics"))
    assert "Overview" in result
    assert "Perspectives & Counterpoints" in result
    assert "Open questions" in result
    assert 'section="<heading>"' in result
    # The TOC itself must be much smaller than the full note -- that's the point.
    assert len(result) < len(_LONG_SECTIONED) / 4


def test_section_param_returns_just_that_section(tmp_vault):
    _write(tmp_vault, "topics", "long-topic.md", _LONG_SECTIONED)
    result = run(vault.vault_read("long-topic.md", "topics", section="Open questions"))
    assert "Question one" in result
    assert "Overview content" not in result
    assert "Counterpoint content" not in result


def test_section_param_is_case_insensitive_and_partial(tmp_vault):
    _write(tmp_vault, "topics", "long-topic.md", _LONG_SECTIONED)
    result = run(vault.vault_read("long-topic.md", "topics", section="perspectives"))
    assert "Counterpoint content" in result


def test_section_param_unknown_lists_available_sections(tmp_vault):
    _write(tmp_vault, "topics", "long-topic.md", _LONG_SECTIONED)
    result = run(vault.vault_read("long-topic.md", "topics", section="Nonexistent Heading"))
    assert "No section matching" in result
    assert "Overview" in result and "Open questions" in result


def test_section_star_forces_full_text_of_long_note(tmp_vault):
    _write(tmp_vault, "topics", "long-topic.md", _LONG_SECTIONED)
    result = run(vault.vault_read("long-topic.md", "topics", section="*"))
    assert result == _LONG_SECTIONED


def test_section_on_short_note_with_no_sections_lists_no_sections(tmp_vault):
    _write(tmp_vault, "people", "sam.md", "# Sam\n\nA short portrait.\n")
    result = run(vault.vault_read("sam.md", "people", section="Overview"))
    assert "No section matching" in result
    assert "no \"## \" sections" in result


def test_not_found_file_unaffected_by_section_logic(tmp_vault):
    result = run(vault.vault_read("missing.md", "topics", section="Overview"))
    assert result.startswith("Not found")


def test_split_sections_captures_lead_before_first_heading():
    body = "Intro text.\n\n## First\n\nFirst content.\n\n## Second\n\nSecond content.\n"
    sections = vault._split_sections(body)
    assert sections[0] == ("", "Intro text.")
    assert sections[1] == ("First", "First content.")
    assert sections[2] == ("Second", "Second content.")


def test_split_sections_ignores_h3_as_a_boundary():
    body = "## Only Section\n\nSome text.\n\n### A subheading\n\nMore text under it.\n"
    sections = vault._split_sections(body)
    assert len(sections) == 1
    assert sections[0][0] == "Only Section"
    assert "### A subheading" in sections[0][1]


def test_strip_frontmatter_removes_yaml_block():
    content = "---\ntype: topic\ndate: 2026-01-01\n---\n\nBody here.\n"
    assert vault._strip_frontmatter(content) == "\nBody here."


def test_strip_frontmatter_no_frontmatter_returns_unchanged():
    content = "# Just a note\n\nNo frontmatter at all.\n"
    assert vault._strip_frontmatter(content) == content
