"""Real writer formats in a temporary vault and real, isolated SQLite tables."""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.brain import background, chronos, research, rumination, skill_nightly
from backend.mobile import notes, overnight as O, routes
from backend.storage import delivery, forecasts, goals, jobs, watches
from backend.tools import vault


def at(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=O.LOCAL_TZ).timestamp()


NOW = at("2026-10-25T08:20:00")
SINCE = at("2026-10-24T22:00:00")


def note(folder, name, text, when=NOW - 600):
    path = vault._resolve(folder, name, folders=notes.FOLDERS)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when, when))
    return path


def night():
    # research.py _log_research_pass: timestamp | subject [angle] (origin) | outcome
    research._QUEUE_FILE.write_text(
        "# Research Queue\n\n"
        "[done] 2026-10-24 21:59 | Prior pass [counterpoint] (auto) | no new understanding\n"
        "[done] 2026-10-24 23:17 | Why bond prices move [counterpoint] (queued) | wrote topics/bond-prices.md — separated duration from credit risk (DM held: min gap)\n"
        "[done] 2026-10-25 04:11 | Interest-rate expectations [connections] (auto) | no new understanding\n", encoding="utf-8")
    note("topics", "bond-prices.md", "---\ncreated: 2026-10-24\n---\n# Bond prices\nA sourced distinction.")
    # rumination.py's exact dated heading (no time field).
    note("self", "ruminations.md", "# Ruminations\n\n## 2026-10-20 (session old12345)\nAn old thought.\n\n## 2026-10-25 (session d47ea921)\nI treated his question as a request for certainty; it may have been about trust.\n")
    chronos._SCHEDULE_FILE.write_text(
        "# Schedule\n[done] 2026-10-25 02:31 | night task done: Review the ECB's duration explanation\n"
        "[done] 2026-10-25 03:35 | timed check started: Check the overnight launch status\n", encoding="utf-8")
    chronos._STATE_FILE.write_text(json.dumps({"night_results": [{"date": "2026-10-25", "task": "Review the ECB's duration explanation", "status": "done",
        "summary": "Compared the short and long duration examples, with two source quotations.", "file": "night-2026-10-25-review-the-ecb-s-duration-explanation.md"}]}), encoding="utf-8")
    note("drafts", "night-2026-10-25-review-the-ecb-s-duration-explanation.md", "# Night task — 2026-10-25\n\nTask: Review the ECB's duration explanation\n\nCompared examples.")
    wid = watches.add(subject="A launch weather delay", condition="The operator announces a weather scrub", method="web",
                      query="operator launch weather scrub October 25 2026", every_s=3600, expires_at=NOW + 86400, now=SINCE)
    assert watches.update(wid, state=watches.FIRED, last_checked_at=at("2026-10-25T04:42:00"), alert_text='"The launch was scrubbed due to upper-level winds."')
    fid = forecasts.add(claim="The next launch attempt will be postponed for weather", criteria="The operator announces a weather scrub", domain="science",
                        raw_p=.3947, stated_p=.4, method="web", check_query="operator launch status October 25", resolve_by=at("2026-10-25T03:00:00"), now=SINCE - 3600)
    assert forecasts.resolve(fid, outcome=1, resolver="web", quote="The operator announced a weather scrub.", notice="", now=at("2026-10-25T04:43:00"))
    skill_nightly._STATE_FILE.write_text(json.dumps({"report": {"date": "2026-10-25", "lines": [
        "learned check-launch-status (you asked for it 3 times: Fri 23, Sat 24)", "merged launch-weather into check-launch-status",
        "changed check-launch-status: check the operator before a news report", "retired unused-summary (not opened in 90 days)"]}}), encoding="utf-8")
    # consolidation.py deliberately names a letter by its old session date, not write date.
    note("letters", "2026-10-22_8f3b162a.md", "Dear future self,\n\nThe question of trust is still open.\nCarry it carefully.", at("2026-10-25T06:12:00"))
    p = {"display_id": 17, "kind": "web_research", "description": "Compare official launch statements", "started": SINCE - 2 * 86400,
         "finished": at("2026-10-25T05:20:00"), "summary": "The operator's statement resolves the conflicting reports.", "note_file": "drafts/research-17-compare-official-launch-statements.md"}
    note("drafts", "research-17-compare-official-launch-statements.md", "# Web research #17\n\n## Result\nThe operator's statement.")
    jid = jobs.enqueue(p); assert jobs.claim(jid); assert jobs.finish(jid, "done", p)
    return p


def test_quiet_night_and_default_paris_window():
    v = O.view(now=NOW)
    assert v == {"as_of": NOW, "since": SINCE, "items": [], "notes": [], "complete": True}
    # Midnight offset changes across this window: 22:00 was UTC+2, morning is UTC+1.
    assert datetime.fromtimestamp(v["since"], O.LOCAL_TZ).isoformat() == "2026-10-24T22:00:00+02:00"
    assert NOW - v["since"] == 11 * 3600 + 20 * 60
    # At 23:00 last evening still means yesterday, not the current evening.
    assert O.default_since(at("2026-10-25T23:00:00")) == at("2026-10-24T22:00:00")


@pytest.mark.parametrize("since", [None, "unreadable", "nan", "inf", NOW + 1, NOW - 7 * 86400 - 1])
def test_invalid_or_unbounded_since_falls_back(since):
    assert O.view(since, now=NOW)["since"] == SINCE


def test_real_writer_shapes_are_ordered_filtered_and_every_link_opens(monkeypatch):
    p = night()
    monkeypatch.setattr(background, "snapshot", lambda: [{**p, "id": 17, "status": "done"}])
    v = O.view(now=NOW)
    assert v["complete"] and not v["notes"]
    kinds = {i["kind"] for i in v["items"]}
    assert kinds == {"research", "rumination", "night_task", "timed_check", "watch", "forecast", "skills", "job", "letter"}
    assert [i["at"] for i in v["items"]] == sorted(i["at"] for i in v["items"])
    assert sum(i["kind"] == "job" for i in v["items"]) == 1  # no live/durable duplicate
    assert not any("Prior pass" in i["title"] for i in v["items"])
    assert any(i["ok"] is False for i in v["items"] if i["kind"] == "forecast")
    assert any("upper-level winds" in i["detail"] for i in v["items"] if i["kind"] == "watch")
    assert next(i for i in v["items"] if i["kind"] == "night_task")["link"]
    assert all(notes.read(**i["link"])["text"] for i in v["items"] if i["link"])
    assert next(i for i in v["items"] if i["kind"] == "letter")["at"] == at("2026-10-25T06:12:00")
    assert all(i["date_only"] for i in v["items"] if i["kind"] in ("skills", "rumination"))
    # An explicit exact window excludes earlier events; day-only uncertainty stays explicit.
    later = O.view(at("2026-10-25T05:00:00"), now=NOW)
    assert not any(i["kind"] in ("research", "night_task", "watch", "forecast") for i in later["items"])


def test_goal_milestones_answered_questions_and_only_confirmed_deliveries(monkeypatch):
    from backend.mobile.test_goals_view import goal, question
    # Use the same clock that all real writers use, not synthetic payload names.
    monkeypatch.setattr(goals.time, "time", lambda: NOW - 120)
    g = goal(title="Compare bond funds")
    q = question(g, "Is capital stability the priority?")
    assert goals.resolve_wait(q["wait_id"], {"answer": "Yes"})
    assert goals.record_event(g["goal_id"], "milestone_done", {"milestone": "M1", "accepted": {"evidence_refs": ["ev91"]}}, "m1")
    for key in ("sent-result", "pending-result"):
        assert delivery.prepare(key, "discord", "Compared duration and fees.", ["Compared duration and fees."])
        goals.link_delivery(g["goal_id"], key, "end:done", key)
    assert delivery.claim_part("sent-result", 0) and delivery.mark_sent("sent-result", 0, "discord-331")
    items = [i for i in O.view(now=NOW)["items"] if i["kind"] == "goal"]
    assert {i["event"] for i in items} == {"milestone", "question", "delivered"}
    assert len(items) == 3 and all(i["goal_id"] == g["goal_id"] for i in items)
    assert any("capital stability" in i["detail"] for i in items)


def test_job_started_before_window_survives_without_a_live_snapshot():
    night()
    v = O.view(now=NOW)
    jobs_seen = [i for i in v["items"] if i["kind"] == "job"]
    assert len(jobs_seen) == 1 and jobs_seen[0]["link"]
    assert jobs_seen[0]["title"] == "Compare official launch statements"


@pytest.mark.parametrize("module,attribute,label", [(research, "snapshot", "Research passes"), (rumination, "snapshot", "Second thoughts"),
    (chronos, "agenda", "Night tasks and timed checks"), (skill_nightly, "report", "Procedure changes"),
    (watches, "fired_since", "Alerts"), (jobs, "finished_since", "Saved background jobs"), (goals, "overnight_since", "Goal steps"),
    (background, "snapshot", "Background jobs"), (forecasts, "query", "Settled forecasts")])
def test_each_broken_source_is_a_logged_note_and_keeps_the_rest(monkeypatch, caplog, module, attribute, label):
    night()
    def broken(*args, **kwargs): raise OSError("fixture unreadable")
    monkeypatch.setattr(module, attribute, broken)
    with caplog.at_level(logging.WARNING): v = O.view(now=NOW)
    assert not v["complete"] and f"{label} could not be read just now." in v["notes"]
    assert len(v["items"]) >= 7 and "fixture unreadable" in caplog.text


def test_unreadable_letter_keeps_the_other_sources(monkeypatch, caplog):
    night()
    original = notes.read
    def unreadable(folder, name):
        if folder == "letters": raise OSError("fixture letter read failed")
        return original(folder, name)
    monkeypatch.setattr(notes, "read", unreadable)
    with caplog.at_level(logging.WARNING): v = O.view(now=NOW)
    assert not v["complete"] and "Letters could not be read just now." in v["notes"]
    assert len(v["items"]) >= 10 and "fixture letter read failed" in caplog.text


@pytest.mark.parametrize("module,path", [(chronos, "_STATE_FILE"), (research, "_PACE_FILE"), (skill_nightly, "_STATE_FILE")])
def test_corrupt_owned_state_is_visible_instead_of_quiet(module, path, caplog):
    getattr(module, path).write_text('{"broken":', encoding="utf-8")
    with caplog.at_level(logging.WARNING): v = O.view(now=NOW)
    assert not v["complete"] and v["notes"] and "state could not be read" in caplog.text


@pytest.mark.parametrize("module,reader", [(watches, lambda: watches.fired_since(SINCE, strict=True)),
    (jobs, lambda: jobs.finished_since(SINCE)), (goals, lambda: goals.overnight_since(SINCE))])
def test_strict_storage_failures_raise_and_log(module, reader, monkeypatch, caplog):
    monkeypatch.setattr(module, "_conn", lambda: None)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError): reader()
    assert caplog.records


def test_an_unreadable_source_cannot_mean_a_quiet_night(monkeypatch, caplog):
    research._QUEUE_FILE.mkdir()  # a real filesystem error, not only a mocked reader
    with caplog.at_level(logging.WARNING): v = O.view(now=NOW)
    assert v["items"] == [] and not v["complete"]
    assert v["notes"] == ["Research passes could not be read just now."]
    assert "queue could not be read" in caplog.text
    assert research.snapshot()["passes"] == []  # legacy silent-degrading caller unchanged


def test_missing_notes_do_not_become_links():
    night()
    notes.resolve("topics", "bond-prices.md").unlink()
    notes.resolve("drafts", "night-2026-10-25-review-the-ecb-s-duration-explanation.md").unlink()
    v = O.view(now=NOW)
    assert v["complete"]
    assert not next(i for i in v["items"] if i["kind"] == "night_task")["link"]
    assert not next(i for i in v["items"] if "Why bond prices" in i["title"])["link"]


def test_night_result_without_trail_and_trail_after_brief_are_both_honest():
    night()
    chronos._SCHEDULE_FILE.write_text("# Schedule\n", encoding="utf-8")
    result = next(i for i in O.view(now=NOW)["items"] if i["kind"] == "night_task")
    assert result["date_only"] and result["link"] and "two source quotations" in result["detail"]
    chronos._STATE_FILE.write_text('{"night_results": []}', encoding="utf-8")
    chronos._SCHEDULE_FILE.write_text("[done] 2026-10-25 02:31 | night task done: Review the ECB's duration explanation\n", encoding="utf-8")
    result = next(i for i in O.view(now=NOW)["items"] if i["kind"] == "night_task")
    assert result["link"] is None and result["title"].startswith("night task done:")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", "overnight-token")
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer overnight-token"
        yield c


@pytest.mark.parametrize("folder,name", [("topics", "../escape.md"), ("topics", "..\\escape.md"), ("topics", "/absolute.md"),
    ("topics", "C:\\absolute.md"), ("topics", "%2e%2e%2fescape.md"), ("unknown", "note.md"), ("topics", "plain.txt"),
    ("topics", "note.md:secret.md"), ("topics", "CON.md"), ("topics", "bad..md")])
def test_note_validation(client, folder, name):
    assert client.get("/mobile/v1/vault/note", params={"folder": folder, "name": name}).status_code == 400


def test_note_token_missing_cap_unicode_and_conditional_get(client):
    text = "# Pensées\n\n" + "Le prix n'est pas la valeur. €\n" * 2400
    note("topics", "value.md", text)
    for path in ("/mobile/v1/vault/note?folder=topics&name=value.md", "/mobile/v1/overnight", "/mobile/v1/hub"):
        assert client.get(path, headers={"Authorization": ""}).status_code == 401
    assert client.get("/mobile/v1/vault/note?folder=topics&name=missing.md").status_code == 404
    # Once-encoded traversal, as sent by a real URL, is refused after the HTTP decode too.
    assert client.get("/mobile/v1/vault/note?folder=topics&name=%2e%2e%2fescape.md").status_code == 400
    response = client.get("/mobile/v1/vault/note?folder=topics&name=value.md")
    assert response.status_code == 200 and response.json()["text"] == text[:60000] and response.json()["truncated"]
    assert client.get("/mobile/v1/vault/note?folder=topics&name=value.md", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    assert "private" in response.headers["cache-control"]
    assert client.get("/mobile/v1/overnight?since=not-a-number").status_code == 200


def test_unreadable_note_is_logged_as_unavailable_not_invalid_request(client, caplog):
    path = note("topics", "unreadable.md", "placeholder")
    path.write_bytes(b'\xff\xfeinvalid UTF-8')
    with caplog.at_level(logging.WARNING): response = client.get("/mobile/v1/vault/note?folder=topics&name=unreadable.md")
    assert response.status_code == 503 and "could not be read" in response.json()["detail"]
    assert "note could not be read" in caplog.text


@pytest.mark.parametrize("owner,path,label", [(research, "_QUEUE_FILE", "Research passes"), (chronos, "_SCHEDULE_FILE", "Night tasks and timed checks")])
def test_malformed_trail_has_a_note_while_legacy_callers_keep_their_fallback(owner, path, label):
    getattr(owner, path).write_text("[done] not-a-date | unfinished trail\n", encoding="utf-8")
    v = O.view(now=NOW)
    assert not v["complete"] and f"{label} could not be read just now." in v["notes"]
    legacy = research.snapshot() if owner is research else chronos.agenda()
    assert legacy["passes" if owner is research else "done"] == []


def test_traversal_cannot_read_a_real_file_outside_the_allowed_folder(client):
    private = vault._VAULT / "private.md"
    private.write_text("This fixture must never leave the vault root.", encoding="utf-8")
    response = client.get("/mobile/v1/vault/note", params={"folder": "topics", "name": "../private.md"})
    assert response.status_code == 400 and "fixture must never leave" not in response.text


def test_note_symlink_escape_is_refused(tmp_path, client):
    outside = tmp_path / "outside.md"; outside.write_text("private fixture", encoding="utf-8")
    note("topics", "safe.md", "A safe note.")
    target = vault._resolve("topics", "escape.md")
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"This machine does not permit test symlinks: {exc}")
    assert client.get("/mobile/v1/vault/note?folder=topics&name=escape.md").status_code == 400


def test_real_path_containment_even_without_symlink_privilege(tmp_path, client, monkeypatch):
    original = vault._resolve
    def escaped(folder, name, **kwargs):
        return tmp_path / "outside.md" if name == "escape.md" else original(folder, name, **kwargs)
    monkeypatch.setattr(vault, "_resolve", escaped)
    assert client.get("/mobile/v1/vault/note?folder=topics&name=escape.md").status_code == 400


def test_hub_optional_module_failure_and_custom_since(client, monkeypatch, caplog):
    import importlib
    real = importlib.import_module
    monkeypatch.setattr(O.time, "time", lambda: NOW)
    def load(name, *args, **kwargs):
        if name == "backend.mobile.record": raise ImportError("fixture absent record")
        return real(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", load)
    with caplog.at_level(logging.WARNING): v = client.get(f"/mobile/v1/hub?since={NOW-60}").json()
    assert v["overnight"] == {"since": NOW-60, "count": 0, "complete": True}
    assert "goals" in v and "record" not in v and "fixture absent record" in caplog.text
    def absent(name, *args, **kwargs):
        if name == "backend.mobile.overnight": raise ImportError("fixture absent overnight")
        return real(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", absent)
    v = client.get("/mobile/v1/hub").json()
    assert "record" in v and "goals" in v and "overnight" not in v
    assert any("Overnight activity" in n for n in v["notes"])
