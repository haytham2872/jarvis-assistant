"""Tests for backend/brain/predictions.py — the predictive-portrait journal.

Pure-function focused: predictions.md is machine-owned (the LLM only emits
verdicts/new-prediction text; every byte of file surgery happens here in
code), so the interesting surface is parse/render/apply_scoring/append_new,
not I/O. Run with:
    .venv\\Scripts\\pytest backend/brain/test_predictions.py -v
"""

from backend.brain import predictions as p


# ---------------------------------------------------------------------------
# parse_file / render_file — round-trip
# ---------------------------------------------------------------------------

def test_parse_render_round_trip():
    text = p.render_file(
        {"right": 2, "wrong": 1, "unclear": 0},
        [
            p.Prediction(status="pending", date="2026-07-16", sid="ab12cd34", text="He'll ask about X."),
            p.Prediction(status="right", date="2026-07-14", sid="9f00aa11", text="He'd like Y — evidence: he did."),
        ],
    )
    counters, preds = p.parse_file(text)
    assert counters == {"right": 2, "wrong": 1, "unclear": 0}
    assert len(preds) == 2
    assert preds[0].status == "pending"
    assert preds[1].status == "right"
    assert preds[1].sid == "9f00aa11"


def test_parse_file_empty_string_degrades_cleanly():
    counters, preds = p.parse_file("")
    assert counters == {"right": 0, "wrong": 0, "unclear": 0}
    assert preds == []


def test_parse_file_malformed_content_ignores_bad_lines():
    text = "not a real line\n[pending] not-a-date | sid:x | broken\n<!-- calibration: right=3 wrong=1 unclear=2 -->"
    counters, preds = p.parse_file(text)
    assert counters == {"right": 3, "wrong": 1, "unclear": 2}
    assert preds == []  # neither garbage line matches _LINE_RE


def test_counters_are_read_from_header_not_recomputed():
    # Header claims 5/0/0 even though only one scored line is present --
    # render_file must trust the counters, not recompute from visible lines
    # (this is what makes capping scored lines lossless).
    text = p.render_file(
        {"right": 5, "wrong": 0, "unclear": 0},
        [p.Prediction(status="right", date="2026-07-01", sid="11111111", text="only one visible")],
    )
    counters, _ = p.parse_file(text)
    assert counters["right"] == 5


# ---------------------------------------------------------------------------
# pending()
# ---------------------------------------------------------------------------

def test_pending_filters_only_pending_status():
    preds = [
        p.Prediction(status="pending", date="2026-07-16", sid="a", text="1"),
        p.Prediction(status="right", date="2026-07-14", sid="b", text="2"),
        p.Prediction(status="pending", date="2026-07-16", sid="a", text="3"),
    ]
    result = p.pending(preds)
    assert [x.text for x in result] == ["1", "3"]


# ---------------------------------------------------------------------------
# render_file — scored cap keeps newest, counters intact
# ---------------------------------------------------------------------------

def test_scored_cap_keeps_newest_and_preserves_counters():
    scored = [
        p.Prediction(status="right", date="2026-01-01", sid="00000001", text=f"pred {i}")
        for i in range(40)
    ]
    text = p.render_file({"right": 999, "wrong": 1, "unclear": 2}, scored)
    counters, preds = p.parse_file(text)
    # counters preserved even though only 30 lines are rendered
    assert counters == {"right": 999, "wrong": 1, "unclear": 2}
    rendered_scored = [x for x in preds if x.status != "pending"]
    assert len(rendered_scored) == p._SCORED_CAP
    # newest kept (highest index = appended last = "newest")
    assert rendered_scored[-1].text == "pred 39"
    assert rendered_scored[0].text == "pred 10"  # 40 - 30 = index 10 onward


def test_render_file_pending_uncapped():
    pend = [
        p.Prediction(status="pending", date="2026-07-16", sid="a", text=f"p{i}")
        for i in range(6)
    ]
    text = p.render_file({"right": 0, "wrong": 0, "unclear": 0}, pend)
    _, preds = p.parse_file(text)
    assert len([x for x in preds if x.status == "pending"]) == 6


# ---------------------------------------------------------------------------
# format_pending_numbered
# ---------------------------------------------------------------------------

def test_format_pending_numbered():
    preds = [
        p.Prediction(status="pending", date="2026-07-14", sid="a", text="He'll bring up X."),
        p.Prediction(status="right", date="2026-07-01", sid="b", text="scored, not shown"),
        p.Prediction(status="pending", date="2026-07-15", sid="a", text="He'll be annoyed by Y."),
    ]
    block = p.format_pending_numbered(preds)
    assert block == (
        "1. (made 2026-07-14) He'll bring up X.\n"
        "2. (made 2026-07-15) He'll be annoyed by Y."
    )


def test_format_pending_numbered_empty():
    assert p.format_pending_numbered([]) == ""


# ---------------------------------------------------------------------------
# apply_scoring — moves pending -> scored, bumps counters, ignores garbage
# ---------------------------------------------------------------------------

def test_apply_scoring_moves_pending_to_scored_with_evidence():
    text = p.render_file(
        {"right": 0, "wrong": 0, "unclear": 0},
        [p.Prediction(status="pending", date="2026-07-14", sid="ab12cd34", text="He'll push back on X.")],
    )
    verdicts = [{"n": 1, "verdict": "wrong", "evidence": "he agreed immediately"}]
    updated = p.apply_scoring(text, verdicts)
    counters, preds = p.parse_file(updated)
    assert counters == {"right": 0, "wrong": 1, "unclear": 0}
    assert len(preds) == 1
    assert preds[0].status == "wrong"
    assert "evidence: he agreed immediately" in preds[0].text


def test_apply_scoring_ignores_out_of_range_n():
    text = p.render_file(
        {"right": 0, "wrong": 0, "unclear": 0},
        [p.Prediction(status="pending", date="2026-07-14", sid="a", text="pred 1")],
    )
    updated = p.apply_scoring(text, [{"n": 5, "verdict": "right", "evidence": "x"}])
    counters, preds = p.parse_file(updated)
    assert counters == {"right": 0, "wrong": 0, "unclear": 0}
    assert preds[0].status == "pending"  # untouched


def test_apply_scoring_ignores_bad_verdict_value():
    text = p.render_file(
        {"right": 0, "wrong": 0, "unclear": 0},
        [p.Prediction(status="pending", date="2026-07-14", sid="a", text="pred 1")],
    )
    updated = p.apply_scoring(text, [{"n": 1, "verdict": "maybe", "evidence": "x"}])
    counters, preds = p.parse_file(updated)
    assert counters == {"right": 0, "wrong": 0, "unclear": 0}
    assert preds[0].status == "pending"


def test_apply_scoring_leaves_extra_pendings_untouched():
    text = p.render_file(
        {"right": 0, "wrong": 0, "unclear": 0},
        [
            p.Prediction(status="pending", date="2026-07-14", sid="a", text="pred 1"),
            p.Prediction(status="pending", date="2026-07-14", sid="a", text="pred 2"),
        ],
    )
    updated = p.apply_scoring(text, [{"n": 1, "verdict": "right", "evidence": "yes"}])
    _, preds = p.parse_file(updated)
    statuses = {x.text.split(" —")[0]: x.status for x in preds}
    assert statuses["pred 1"] == "right"
    assert statuses["pred 2"] == "pending"


def test_apply_scoring_no_evidence_omits_dash():
    text = p.render_file(
        {"right": 0, "wrong": 0, "unclear": 0},
        [p.Prediction(status="pending", date="2026-07-14", sid="a", text="pred 1")],
    )
    updated = p.apply_scoring(text, [{"n": 1, "verdict": "unclear", "evidence": ""}])
    _, preds = p.parse_file(updated)
    assert preds[0].text == "pred 1"


# ---------------------------------------------------------------------------
# append_new
# ---------------------------------------------------------------------------

def test_append_new_formats_lines():
    text = p.render_file({"right": 0, "wrong": 0, "unclear": 0}, [])
    updated = p.append_new(text, ["He'll mention Y.", "He'll be tired."], "9f00aa11", "2026-07-16")
    _, preds = p.parse_file(updated)
    assert len(preds) == 2
    assert all(x.status == "pending" and x.sid == "9f00aa11" and x.date == "2026-07-16" for x in preds)
    assert preds[0].text == "He'll mention Y."


def test_append_new_skips_blank_entries():
    text = p.render_file({"right": 0, "wrong": 0, "unclear": 0}, [])
    updated = p.append_new(text, ["", "  ", "real one"], "abcd1234", "2026-07-16")
    _, preds = p.parse_file(updated)
    assert len(preds) == 1
    assert preds[0].text == "real one"


# ---------------------------------------------------------------------------
# calibration_summary
# ---------------------------------------------------------------------------

def test_calibration_summary_empty_when_nothing_scored():
    text = p.render_file({"right": 0, "wrong": 0, "unclear": 0}, [])
    assert p.calibration_summary(text) == ""


def test_calibration_summary_zero_decided_but_some_unclear():
    text = p.render_file(
        {"right": 0, "wrong": 0, "unclear": 2},
        [p.Prediction(status="unclear", date="2026-07-01", sid="a", text="x")],
    )
    summary = p.calibration_summary(text)
    assert "no decided predictions yet" in summary


def test_calibration_summary_hit_rate_and_recent():
    text = p.render_file(
        {"right": 3, "wrong": 1, "unclear": 0},
        [
            p.Prediction(status="right", date="2026-07-01", sid="a", text="one"),
            p.Prediction(status="wrong", date="2026-07-02", sid="a", text="two"),
        ],
    )
    summary = p.calibration_summary(text)
    assert "3 right / 1 wrong / 0 unclear" in summary
    assert "75%" in summary
    assert "[wrong] two" in summary


def test_calibration_summary_malformed_file_degrades_to_empty():
    assert p.calibration_summary("garbage\nnot a predictions file at all") == ""
