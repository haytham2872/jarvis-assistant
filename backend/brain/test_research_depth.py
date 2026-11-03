"""A research pass may improve understanding or leave the note alone."""
import asyncio
import json

from backend.brain import research


def run(coro):
    return asyncio.run(coro)


def test_picker_can_skip_without_searching_or_writing(monkeypatch):
    async def candidates(_now):
        return [{"file": "camus.md", "title": "Camus", "score": 0.8,
                 "freshness": 0.8, "staleness": 0.0, "last_angle": "deepen",
                 "digest": "Revolt and limits", "open_question": ""}]

    async def recent(_now):
        return "We talked about Camus."

    async def pick(_prompt, **_kwargs):
        return '{"skip": true}'

    def unexpected(*_args, **_kwargs):
        raise AssertionError("A skipped pass must not search or write")

    monkeypatch.setattr(research, "_pop_queued", lambda: None)
    monkeypatch.setattr(research, "_gather_candidates", candidates)
    monkeypatch.setattr(research, "_recent_exchange_lines", recent)
    monkeypatch.setattr(research, "_llm", pick)
    monkeypatch.setattr(research, "_closed_notes", lambda: [])
    monkeypatch.setattr(research, "fallback_pick", unexpected)
    run(research._tick())


def test_no_progress_keeps_note_body_and_does_not_dm(tmp_path, monkeypatch):
    topics = tmp_path / "topics"
    topics.mkdir()
    old_body = "# Camus\n\nRevolt is a response to absurdity.\n"
    (topics / "camus.md").write_text(old_body, encoding="utf-8")
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    monkeypatch.setattr(research, "_pop_queued", lambda: {
        "subject": "Camus", "angle": "deepen", "reason": "Explain revolt"})

    async def synth(_prompt, **_kwargs):
        return json.dumps({"progress": False, "changed_understanding": "",
                           "note": None, "notify": False, "message": None})

    async def search(_query):
        return "Camus primary text https://example.org/camus"

    writes = []

    async def write(filename, body, folder, **_kwargs):
        writes.append((filename, body, folder))
        return "wrote"

    async def notify(_message):
        raise AssertionError("No-progress research must not send a DM")

    from backend.tools import browser, vault
    monkeypatch.setattr(research, "_llm", synth)
    monkeypatch.setattr(research, "_log_research_pass", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(research, "_notifier", notify)
    monkeypatch.setattr(browser, "web_browse", search)
    monkeypatch.setattr(vault, "vault_write", write)
    monkeypatch.setattr(vault, "note_revision", lambda *_args: None)

    run(research._tick())
    assert len(writes) == 1
    assert writes[0][0] == "camus.md"
    assert old_body.strip() in writes[0][1]
    assert "last_researched:" in writes[0][1]
    assert "last_angle: deepen" in writes[0][1]
