"""Tests for backend/brain/positions.py — patched, not rewritten, positions.md.

Pure-function focused: positions.md is machine-owned (the LLM only emits small
add/revise patch ops; every byte of file surgery happens here in code), so the
interesting surface is parse_file/render_file/apply_patch, not I/O. This is
what replaced the old full-rewrite + shrink-floor design in consolidation.py —
see CLAUDE.md's Hardening 2026-09-01 note. Run with:
    .venv\\Scripts\\pytest backend/brain/test_positions.py -v
"""

from backend.brain import positions as pos


# ---------------------------------------------------------------------------
# parse_file / render_file — round-trip
# ---------------------------------------------------------------------------

def test_parse_render_round_trip():
    text = pos.render_file({
        "nietzsche": pos.Position(
            topic="nietzsche", stance="Eternal recurrence as a practical test.",
            sources="[[topics/nietzsche]]", formed="2026-07-01",
            revisions=["2026-07-20 — softened the claim after rereading Beyond Good and Evil"],
        ),
        "camus": pos.Position(
            topic="camus", stance="Absurdism is a starting point, not a resting place.",
            sources="", formed="2026-08-01", revisions=[],
        ),
    })
    parsed = pos.parse_file(text)
    assert set(parsed) == {"nietzsche", "camus"}
    assert parsed["nietzsche"].topic == "nietzsche"
    assert parsed["nietzsche"].stance == "Eternal recurrence as a practical test."
    assert parsed["nietzsche"].sources == "[[topics/nietzsche]]"
    assert parsed["nietzsche"].formed == "2026-07-01"
    assert parsed["nietzsche"].revisions == [
        "2026-07-20 — softened the claim after rereading Beyond Good and Evil"
    ]
    assert parsed["camus"].sources == ""
    assert parsed["camus"].revisions == []


def test_parse_file_empty_string_degrades_cleanly():
    assert pos.parse_file("") == {}


def test_parse_file_ignores_preamble_before_first_heading():
    text = "# Positions\n\nsome stray line\n\n## nietzsche\n**Stance:** x\n**Formed:** 2026-07-01\n"
    parsed = pos.parse_file(text)
    assert list(parsed.keys()) == ["nietzsche"]


def test_render_file_empty_dict():
    text = pos.render_file({})
    assert "(none yet)" in text
    assert pos.parse_file(text) == {}


def test_topic_lookup_is_case_insensitive():
    text = pos.render_file({"nietzsche": pos.Position(
        topic="Nietzsche", stance="x", sources="", formed="2026-07-01",
    )})
    parsed = pos.parse_file(text)
    assert "nietzsche" in parsed
    assert parsed["nietzsche"].topic == "Nietzsche"   # original case preserved


# ---------------------------------------------------------------------------
# apply_patch — add
# ---------------------------------------------------------------------------

def test_apply_patch_add_creates_new_position():
    new_text, applied = pos.apply_patch("", [
        {"topic": "nietzsche", "action": "add", "stance": "A new stance.", "sources": "[[topics/nietzsche]]"},
    ], today="2026-09-01")
    assert applied == 1
    parsed = pos.parse_file(new_text)
    assert parsed["nietzsche"].stance == "A new stance."
    assert parsed["nietzsche"].formed == "2026-09-01"
    assert parsed["nietzsche"].revisions == []


def test_apply_patch_add_ignored_when_topic_already_exists():
    existing = pos.render_file({"nietzsche": pos.Position(
        topic="nietzsche", stance="original", sources="", formed="2026-07-01",
    )})
    new_text, applied = pos.apply_patch(existing, [
        {"topic": "nietzsche", "action": "add", "stance": "should not clobber"},
    ], today="2026-09-01")
    assert applied == 0
    parsed = pos.parse_file(new_text)
    assert parsed["nietzsche"].stance == "original"   # untouched


# ---------------------------------------------------------------------------
# apply_patch — revise
# ---------------------------------------------------------------------------

def test_apply_patch_revise_updates_stance_and_appends_revision():
    existing = pos.render_file({"nietzsche": pos.Position(
        topic="nietzsche", stance="original stance", sources="[[topics/nietzsche]]", formed="2026-07-01",
    )})
    new_text, applied = pos.apply_patch(existing, [
        {"topic": "nietzsche", "action": "revise", "stance": "softened stance",
         "revision_note": "softened after rereading the source"},
    ], today="2026-09-01")
    assert applied == 1
    parsed = pos.parse_file(new_text)
    p = parsed["nietzsche"]
    assert p.stance == "softened stance"
    assert p.sources == "[[topics/nietzsche]]"        # untouched field preserved
    assert p.formed == "2026-07-01"                    # original formed date preserved
    assert p.revisions == ["2026-09-01 — softened after rereading the source"]


def test_apply_patch_revise_without_note_updates_stance_only():
    existing = pos.render_file({"nietzsche": pos.Position(
        topic="nietzsche", stance="original", sources="", formed="2026-07-01",
    )})
    new_text, applied = pos.apply_patch(existing, [
        {"topic": "nietzsche", "action": "revise", "stance": "updated, no note given"},
    ], today="2026-09-01")
    assert applied == 1
    parsed = pos.parse_file(new_text)
    assert parsed["nietzsche"].stance == "updated, no note given"
    assert parsed["nietzsche"].revisions == []


def test_apply_patch_revise_ignored_for_unknown_topic():
    existing = pos.render_file({"nietzsche": pos.Position(
        topic="nietzsche", stance="original", sources="", formed="2026-07-01",
    )})
    new_text, applied = pos.apply_patch(existing, [
        {"topic": "camus", "action": "revise", "stance": "should be a no-op"},
    ], today="2026-09-01")
    assert applied == 0
    assert new_text == existing


def test_apply_patch_preserves_existing_topic_order_and_appends_new():
    existing = pos.render_file({
        "nietzsche": pos.Position(topic="nietzsche", stance="a", sources="", formed="2026-07-01"),
        "camus": pos.Position(topic="camus", stance="b", sources="", formed="2026-07-02"),
    })
    new_text, applied = pos.apply_patch(existing, [
        {"topic": "dostoevsky", "action": "add", "stance": "c"},
    ], today="2026-09-01")
    assert applied == 1
    assert list(pos.parse_file(new_text).keys()) == ["nietzsche", "camus", "dostoevsky"]


# ---------------------------------------------------------------------------
# apply_patch — malformed entries never raise, never corrupt the file
# ---------------------------------------------------------------------------

def test_apply_patch_skips_malformed_entries():
    existing = pos.render_file({"nietzsche": pos.Position(
        topic="nietzsche", stance="original", sources="", formed="2026-07-01",
    )})
    new_text, applied = pos.apply_patch(existing, [
        {},                                              # missing everything
        {"topic": "nietzsche"},                           # missing action/stance
        {"topic": "x", "action": "delete", "stance": "y"}, # unrecognized action
        {"topic": "", "action": "add", "stance": "y"},     # empty topic
        "not even a dict",
        None,
    ], today="2026-09-01")
    assert applied == 0
    assert new_text == existing


def test_apply_patch_empty_list_is_noop():
    existing = pos.render_file({"nietzsche": pos.Position(
        topic="nietzsche", stance="original", sources="", formed="2026-07-01",
    )})
    new_text, applied = pos.apply_patch(existing, [], today="2026-09-01")
    assert applied == 0
    assert new_text == existing


def test_apply_patch_none_is_noop():
    new_text, applied = pos.apply_patch("", None, today="2026-09-01")
    assert applied == 0
    assert pos.parse_file(new_text) == {}
