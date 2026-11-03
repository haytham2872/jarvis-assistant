"""A phone-started job's conclusion: the message under its card (Sam, 15 October 2026:
the whole research stays folded in the card, only the conclusion is a normal message)."""
import asyncio

import pytest

from backend.brain import conclusion as C

BODY = ("The ECB held rates at 2.00% on 11 September. Inflation came in at 2.1% in August, "
        "close to target, and growth forecasts were trimmed to 0.9% for 2026. ") * 6

REPORT_WITH_SECTION = f"""# ECB outlook

## Background
{BODY}

## Conclusion
The ECB is likely to hold in October. A cut is unlikely before December unless growth slips.

## Sources
1. https://www.ecb.europa.eu/press
"""


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def model(monkeypatch):
    calls = []

    async def fake(result):
        calls.append(result)
        return "The ECB will most likely hold in October."
    monkeypatch.setattr(C, "_ask_model", fake)
    return calls


def test_a_short_result_is_its_own_conclusion(model):
    assert run(C.of("NVDA closed at $181.40.")) == "NVDA closed at $181.40."
    assert model == []


def test_the_reports_own_conclusion_section_is_used_without_a_model_call(model):
    out = run(C.of(REPORT_WITH_SECTION))
    assert out == "The ECB is likely to hold in October. A cut is unlikely before December unless growth slips."
    assert model == []


@pytest.mark.parametrize("head", ["## TL;DR", "### Bottom line:", "**Verdict**", "**In short:**", "# Summary"])
def test_other_conclusion_headings_count(model, head):
    text = f"## Findings\n{BODY}\n\n{head}\nHold through October.\n\n## Method\nRead three sources."
    assert run(C.of(text)) == "Hold through October."


def test_a_bold_label_with_its_text_on_the_same_line(model):
    text = f"{BODY}\n\n**Bottom line:** buy nothing before the earnings call.\n"
    assert run(C.of(text)) == "buy nothing before the earnings call."


def test_the_last_conclusion_section_wins(model):
    text = f"## Summary\nEarly view.\n\n## Detail\n{BODY}\n\n## Conclusion\nFinal view.\n"
    assert run(C.of(text)) == "Final view."


def test_without_a_section_the_model_writes_it(model):
    out = run(C.of(BODY))
    assert out == "The ECB will most likely hold in October." and len(model) == 1


def test_the_model_sees_the_start_and_end_of_a_long_report_without_sources():
    text = "START " + "x" * 30_000 + " END\n\nSources:\n1. https://example.com"
    seen = C._model_input(text)
    assert seen.startswith("START") and seen.endswith("END") and "[...]" in seen
    assert "example.com" not in seen


def test_a_failed_model_call_falls_back_to_the_last_paragraph(monkeypatch):
    async def boom(result):
        raise RuntimeError("provider down")
    monkeypatch.setattr(C, "_ask_model", boom)
    text = f"{BODY}\n\nSo the most likely path is a hold, then a cut in December.\n\nSources:\n1. https://x.y"
    assert run(C.of(text)) == "So the most likely path is a hold, then a cut in December."


def test_an_empty_model_answer_falls_back_too(monkeypatch):
    async def empty(result):
        return ""
    monkeypatch.setattr(C, "_ask_model", empty)
    assert run(C.of(f"{BODY}\n\nLast thought here, long enough to count as a paragraph.")) \
        == "Last thought here, long enough to count as a paragraph."


def test_a_long_section_is_cut_at_a_sentence(model):
    long = "This sentence is part of the conclusion. " * 60
    out = run(C.of(f"{BODY}\n\n## Conclusion\n{long}"))
    assert len(out) <= C.MAX_CONCLUSION and out.endswith(".")


def test_nothing_in_nothing_out(model):
    assert run(C.of("")) == "" and run(C.of("   ")) == ""


# ------------------------------------------------------------------ the job lifecycle

def _run_task(monkeypatch, origin, result):
    from backend.brain import background as B
    finished = {}

    async def runner(task, **kw):
        return result
    monkeypatch.setattr(B, "_run_compat_loop", runner)

    async def no_note(t):
        return None
    monkeypatch.setattr(B, "_write_result_note", no_note)
    monkeypatch.setattr(B._jobs, "finish", lambda job_id, status, payload, error="": finished.update(payload))
    seen = []
    monkeypatch.setattr(C, "of", _record(seen))
    t = B.BgTask(id=901, description="ECB outlook", origin=origin)       # no job id: no claim
    asyncio.run(B._worker(t))
    return t, finished, seen


def _record(seen):
    async def fake(result):
        seen.append(result)
        return "Hold in October."
    return fake


def test_a_phone_job_carries_its_conclusion_to_the_payload_and_the_phone(monkeypatch):
    from backend.brain import background as B
    t, payload, seen = _run_task(monkeypatch, "mobile", REPORT_WITH_SECTION)
    assert t.status == "done" and seen == [REPORT_WITH_SECTION.strip()]
    assert payload["conclusion"] == "Hold in October." and payload["result"] == REPORT_WITH_SECTION.strip()
    assert B.public_job(t)["conclusion"] == "Hold in October."


def test_a_job_from_elsewhere_spends_nothing_on_a_conclusion(monkeypatch):
    t, payload, seen = _run_task(monkeypatch, "discord", REPORT_WITH_SECTION)
    assert t.status == "done" and seen == [] and payload["conclusion"] == ""


def test_a_phone_jobs_conclusion_survives_a_restart(monkeypatch):
    from backend.brain import background as B
    monkeypatch.setattr(B, "_tasks", {})
    monkeypatch.setattr(B, "_next_id", 1)
    monkeypatch.setattr(B, "_shutting_down", False)
    monkeypatch.setattr(B, "_schedule_queued", lambda: None)
    monkeypatch.setattr(B._pause, "is_paused", lambda *a: False)

    async def runner(text, **kw):
        return B.RunResult(text=REPORT_WITH_SECTION)

    async def note(t):
        return None
    monkeypatch.setattr(B, "_run_compat_loop", runner)
    monkeypatch.setattr(B, "_write_result_note", note)
    t, err = asyncio.run(B._start("ECB outlook", "brief", origin="mobile"))
    assert err == ""
    t.status = "running"
    asyncio.run(B._worker(t))
    B._tasks.clear()
    B.recover_durable_jobs()
    assert B._tasks[t.id].conclusion.startswith("The ECB is likely to hold in October.")


def test_a_sources_line_after_the_conclusion_is_not_part_of_it(model):
    report = ("Long research body about rates, with enough text that it is not short at all. " * 12
              + "\r\n## Conclusion\r\nHold until December.\r\nSources: https://ecb.example/a, https://ecb.example/b")
    assert run(C.of(report)) == "Hold until December."
    assert model == []
