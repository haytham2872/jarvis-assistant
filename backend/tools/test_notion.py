"""Tests for backend/tools/notion.py.

Notion's two property-shape gotcha (Ideas.Status is a plain `select`;
Tasks.Status is Notion's richer `status` type -- different JSON payload
shapes) was verified live against the real API on 2026-09-15 before writing
any of this, per the same discipline as the rest of the codebase -- these
tests pin that distinction with a fake HTTP layer so a future edit can't
silently collapse it back into one shape.
Run with:
    .venv\\Scripts\\pytest backend/tools/test_notion.py -v
"""

import asyncio

import pytest

from backend.tools import notion


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responder):
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, json=None):
        return self._responder(method, url, json)


def _install(monkeypatch, responder):
    monkeypatch.setattr(notion.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(responder))


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(notion, "_API_KEY", "test-key")
    monkeypatch.setattr(notion, "_IDEAS_DB", "ideas-db-id")
    monkeypatch.setattr(notion, "_TASKS_DB", "tasks-db-id")
    yield


# ---------------------------------------------------------------------------
# Configuration gate
# ---------------------------------------------------------------------------

def test_unconfigured_api_key_returns_clear_error(monkeypatch):
    monkeypatch.setattr(notion, "_API_KEY", "")
    result = asyncio.run(notion.notion_action(action="add_idea", title="x"))
    assert result.startswith("Error:")
    assert "NOTION_API_KEY" in result


def test_unconfigured_db_id_returns_clear_error(monkeypatch):
    monkeypatch.setattr(notion, "_TASKS_DB", "")
    result = asyncio.run(notion.notion_action(action="add_task", title="x"))
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# add_idea
# ---------------------------------------------------------------------------

def test_add_idea_sends_select_status_and_confirms(monkeypatch):
    captured = {}

    def responder(method, url, payload):
        captured["method"], captured["url"], captured["payload"] = method, url, payload
        return _FakeResponse(200, {"id": "page-1", "url": "https://notion.so/page-1"})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(
        action="add_idea", title="A JARVIS skill idea", content="longer body", tags=["jarvis"],
    ))
    assert result.startswith("Confirmed added to Notion Ideas:")
    assert "https://notion.so/page-1" in result
    assert captured["url"].endswith("/pages")
    props = captured["payload"]["properties"]
    assert props["Status"] == {"select": {"name": "Raw"}}  # Ideas.Status is `select`, not `status`
    assert props["Tags"] == {"multi_select": [{"name": "jarvis"}]}
    assert captured["payload"]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "longer body"


def test_add_idea_requires_title(monkeypatch):
    result = asyncio.run(notion.notion_action(action="add_idea", title=""))
    assert result.startswith("Error:")


def test_add_idea_missing_id_in_response_is_not_reported_as_saved(monkeypatch):
    _install(monkeypatch, lambda m, u, p: _FakeResponse(200, {}))
    result = asyncio.run(notion.notion_action(action="add_idea", title="x"))
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# add_task
# ---------------------------------------------------------------------------

def test_add_task_sends_status_type_not_select(monkeypatch):
    captured = {}

    def responder(method, url, payload):
        captured["payload"] = payload
        return _FakeResponse(200, {"id": "page-2", "url": "https://notion.so/page-2"})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(
        action="add_task", title="Renew car insurance", due="2026-10-01", priority="High",
    ))
    assert result.startswith("Confirmed added to Notion Tasks:")
    props = captured["payload"]["properties"]
    assert props["Status"] == {"status": {"name": "Not started"}}  # Tasks.Status is `status`, not `select`
    assert props["Due"] == {"date": {"start": "2026-10-01"}}
    assert props["Priority"] == {"select": {"name": "High"}}


def test_add_task_rejects_invalid_priority(monkeypatch):
    result = asyncio.run(notion.notion_action(action="add_task", title="x", priority="Urgent"))
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

def test_list_tasks_default_excludes_done(monkeypatch):
    captured = {}

    def responder(method, url, payload):
        captured["payload"] = payload
        return _FakeResponse(200, {"results": [
            {"properties": {
                "Title": {"title": [{"plain_text": "Renew insurance"}]},
                "Status": {"status": {"name": "Not started"}},
                "Due": {"date": {"start": "2026-10-01"}},
                "Priority": {"select": {"name": "High"}},
            }},
        ]})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(action="list_tasks"))
    assert captured["payload"]["filter"] == {"property": "Status", "status": {"does_not_equal": "Done"}}
    assert "Renew insurance" in result
    assert "due 2026-10-01" in result
    assert "priority High" in result


def test_list_tasks_empty_result_is_a_clean_message(monkeypatch):
    _install(monkeypatch, lambda m, u, p: _FakeResponse(200, {"results": []}))
    result = asyncio.run(notion.notion_action(action="list_tasks"))
    assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# complete_task
# ---------------------------------------------------------------------------

def test_complete_task_matches_and_marks_done(monkeypatch):
    calls = []

    def responder(method, url, payload):
        calls.append((method, url, payload))
        if method == "POST":
            return _FakeResponse(200, {"results": [
                {"id": "page-3", "properties": {
                    "Title": {"title": [{"plain_text": "Renew car insurance"}]},
                    "Status": {"status": {"name": "Not started"}},
                }},
            ]})
        return _FakeResponse(200, {"properties": {"Status": {"status": {"name": "Done"}}}})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(action="complete_task", title="insurance"))
    assert result == 'Confirmed marked "Renew car insurance" as Done in Notion Tasks.'
    assert calls[-1][0] == "PATCH"
    assert calls[-1][2]["properties"]["Status"] == {"status": {"name": "Done"}}


def test_complete_task_no_match_is_an_error(monkeypatch):
    _install(monkeypatch, lambda m, u, p: _FakeResponse(200, {"results": []}))
    result = asyncio.run(notion.notion_action(action="complete_task", title="nothing like this"))
    assert result.startswith("Error:")


def test_complete_task_ambiguous_match_asks_for_specificity(monkeypatch):
    def responder(method, url, payload):
        return _FakeResponse(200, {"results": [
            {"id": "p1", "properties": {"Title": {"title": [{"plain_text": "Call bank A"}]},
                                         "Status": {"status": {"name": "Not started"}}}},
            {"id": "p2", "properties": {"Title": {"title": [{"plain_text": "Call bank B"}]},
                                         "Status": {"status": {"name": "In progress"}}}},
        ]})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(action="complete_task", title="Call bank"))
    assert result.startswith("Error:")
    assert "2 open tasks" in result


def test_complete_task_already_done_rows_are_excluded_from_matching(monkeypatch):
    def responder(method, url, payload):
        return _FakeResponse(200, {"results": [
            {"id": "p1", "properties": {"Title": {"title": [{"plain_text": "Old done task"}]},
                                         "Status": {"status": {"name": "Done"}}}},
        ]})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(action="complete_task", title="Old done task"))
    assert result.startswith("Error:")
    assert "no open Notion task" in result


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_merges_ideas_and_tasks(monkeypatch):
    def responder(method, url, payload):
        if "ideas-db-id" in url:
            return _FakeResponse(200, {"results": [
                {"properties": {"Title": {"title": [{"plain_text": "Cold email idea"}]}}},
            ]})
        return _FakeResponse(200, {"results": [
            {"properties": {"Title": {"title": [{"plain_text": "Draft cold email"}]},
                             "Status": {"status": {"name": "In progress"}}}},
        ]})

    _install(monkeypatch, responder)
    result = asyncio.run(notion.notion_action(action="search", query="cold email"))
    assert "[Idea] Cold email idea" in result
    assert "[Task, In progress] Draft cold email" in result


def test_search_no_matches(monkeypatch):
    _install(monkeypatch, lambda m, u, p: _FakeResponse(200, {"results": []}))
    result = asyncio.run(notion.notion_action(action="search", query="nothing"))
    assert "Nothing in Notion matches" in result


# ---------------------------------------------------------------------------
# Error / degradation contract
# ---------------------------------------------------------------------------

def test_notion_api_error_response_degrades_to_error_string(monkeypatch):
    _install(monkeypatch, lambda m, u, p: _FakeResponse(404, {"message": "database not found"}))
    result = asyncio.run(notion.notion_action(action="add_idea", title="x"))
    assert result.startswith("Error:")
    assert "database not found" in result


def test_unknown_action_does_not_raise(monkeypatch):
    result = asyncio.run(notion.notion_action(action="delete_everything"))
    assert result.startswith("Unknown notion_action action:")


def test_network_exception_never_raises(monkeypatch):
    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **kw):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(notion.httpx, "AsyncClient", lambda *a, **kw: _RaisingClient())
    result = asyncio.run(notion.notion_action(action="add_idea", title="x"))
    assert result.startswith("Error:")
    assert "connection refused" in result
