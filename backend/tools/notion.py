"""
backend/tools/notion.py -- Organize ideas and tasks in Notion via plain REST calls.

Two databases, created once by the owner in the Notion app and shared with a
Notion "internal integration" (a static secret token -- no OAuth, no browser
consent flow, unlike gmail.py/calendar.py/spotify.py, which matters because
JARVIS now runs headless on the cloud VM with no browser to complete a
consent flow in):
  - Ideas (NOTION_IDEAS_DB_ID) -- Title, Status (select: Raw/Organized), Tags
    (multi_select), Created (created_time, read-only/auto).
  - Tasks (NOTION_TASKS_DB_ID) -- Title, Status (Notion's `status` type, NOT
    `select` -- a different JSON shape: {"status": {"name": ...}} vs
    {"select": {"name": ...}}), Due (date), Priority (select).
Both schemas were read live via GET /v1/databases/{id} against the owner's real
workspace (2026-09-15) before any of this was written, rather than assumed --
property names/types below must match what's actually in his workspace; if he
renames a property in Notion, these constants need updating to match.

Deliberately NOT the reminder clock. Notion's API has no supported way to make
Notion itself push a client notification at a given time (the "remind" bell on
a date is a Notion-app-only feature, not settable via the public API) --
chronos.py's schedule_reminder/schedule_night_task already do this correctly
(timezone-aware, DST-correct, fires a Discord DM). This module only mirrors
tasks into Notion for visibility/editing in the Notion app itself; it never
fires anything on a timer. See CLAUDE.md's "Notion -- Ideas & Tasks" section.

Same silent-degradation contract as every other tool here: an unconfigured
integration, a network failure, or a Notion API error all return a clear
string, never raise into the tool loop.
"""

import os
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"
_TIMEOUT = 10.0

_API_KEY  = os.getenv("NOTION_API_KEY", "")
_IDEAS_DB = os.getenv("NOTION_IDEAS_DB_ID", "")
_TASKS_DB = os.getenv("NOTION_TASKS_DB_ID", "")

_IDEAS_STATUS_DEFAULT = "Raw"
_TASK_STATUS_DEFAULT  = "Not started"
_TASK_STATUS_DONE     = "Done"
_TASK_PRIORITIES      = ("Low", "Medium", "High")

_MAX_LIST_ROWS = 25
_RICH_TEXT_CAP = 2000  # Notion's own per-rich-text-item character limit


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _configured() -> str | None:
    """None when ready to call the API; otherwise the exact error to return."""
    if not _API_KEY:
        return "Error: NOTION_API_KEY not set in .env -- the Notion integration isn't configured yet."
    if not _IDEAS_DB or not _TASKS_DB:
        return "Error: NOTION_IDEAS_DB_ID / NOTION_TASKS_DB_ID not set in .env."
    return None


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json().get("message", resp.text)
    except Exception:
        return resp.text


async def _request(method: str, path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(headers=_headers(), timeout=_TIMEOUT) as client:
        resp = await client.request(method, f"{_BASE_URL}{path}", json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Notion API {resp.status_code}: {_error_message(resp)}")
        return resp.json()


def _title_text(prop: dict) -> str:
    parts = (prop or {}).get("title", [])
    text = "".join(p.get("plain_text", "") for p in parts)
    return text or "(untitled)"


def _paragraph_blocks(text: str) -> list[dict]:
    chunks = [text[i:i + _RICH_TEXT_CAP] for i in range(0, len(text), _RICH_TEXT_CAP)]
    return [
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"type": "text", "text": {"content": c}}]}}
        for c in chunks if c
    ]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def _add_idea(title: str, content: str, tags: list[str]) -> str:
    if not title:
        return "Error: add_idea requires a title."
    properties = {
        "Title": {"title": [{"text": {"content": title[:_RICH_TEXT_CAP]}}]},
        "Status": {"select": {"name": _IDEAS_STATUS_DEFAULT}},
    }
    if tags:
        properties["Tags"] = {"multi_select": [{"name": t} for t in tags]}
    payload = {"parent": {"database_id": _IDEAS_DB}, "properties": properties}
    if content:
        payload["children"] = _paragraph_blocks(content)
    page = await _request("POST", "/pages", payload)
    if not page.get("id"):
        return "Error: Notion did not confirm the idea was created -- do not report this as saved."
    return f'Confirmed added to Notion Ideas: "{title}" -- {page.get("url", "")}'


async def _add_task(title: str, due: str, priority: str) -> str:
    if not title:
        return "Error: add_task requires a title."
    if priority and priority not in _TASK_PRIORITIES:
        return f"Error: priority must be one of {_TASK_PRIORITIES} (got {priority!r})."
    properties = {
        "Title": {"title": [{"text": {"content": title[:_RICH_TEXT_CAP]}}]},
        "Status": {"status": {"name": _TASK_STATUS_DEFAULT}},
    }
    if due:
        properties["Due"] = {"date": {"start": due}}
    if priority:
        properties["Priority"] = {"select": {"name": priority}}
    payload = {"parent": {"database_id": _TASKS_DB}, "properties": properties}
    page = await _request("POST", "/pages", payload)
    if not page.get("id"):
        return "Error: Notion did not confirm the task was created -- do not report this as saved."
    due_note = f" (due {due})" if due else ""
    return f'Confirmed added to Notion Tasks: "{title}"{due_note} -- {page.get("url", "")}'


async def _list_tasks(status_filter: str) -> str:
    if status_filter:
        filt = {"property": "Status", "status": {"equals": status_filter}}
    else:
        filt = {"property": "Status", "status": {"does_not_equal": _TASK_STATUS_DONE}}
    data = await _request("POST", f"/databases/{_TASKS_DB}/query", {
        "filter": filt,
        "sorts": [{"property": "Due", "direction": "ascending"}],
    })
    rows = data.get("results", [])
    if not rows:
        return ("No Notion tasks match that status." if status_filter
                else "Notion Tasks is empty -- nothing open.")
    lines = []
    for row in rows[:_MAX_LIST_ROWS]:
        props = row.get("properties", {})
        bits = [_title_text(props.get("Title")), f"[{(props.get('Status', {}).get('status') or {}).get('name', '?')}]"]
        due_v = (props.get("Due", {}).get("date") or {}).get("start")
        if due_v:
            bits.append(f"due {due_v}")
        pr = (props.get("Priority", {}).get("select") or {}).get("name")
        if pr:
            bits.append(f"priority {pr}")
        lines.append(" ".join(bits))
    return "Notion Tasks:\n" + "\n".join(f"- {l}" for l in lines)


async def _complete_task(title: str) -> str:
    if not title:
        return "Error: complete_task requires a title (or part of one) to match."
    data = await _request("POST", f"/databases/{_TASKS_DB}/query", {
        "filter": {"property": "Title", "title": {"contains": title}},
    })
    rows = [
        r for r in data.get("results", [])
        if (r.get("properties", {}).get("Status", {}).get("status") or {}).get("name") != _TASK_STATUS_DONE
    ]
    if not rows:
        return f"Error: no open Notion task matches {title!r} -- nothing marked done."
    if len(rows) > 1:
        names = ", ".join(_title_text(r["properties"].get("Title")) for r in rows[:5])
        return f"Error: {len(rows)} open tasks match {title!r} ({names}) -- be more specific."
    page_id = rows[0]["id"]
    matched = _title_text(rows[0]["properties"].get("Title"))
    updated = await _request("PATCH", f"/pages/{page_id}", {
        "properties": {"Status": {"status": {"name": _TASK_STATUS_DONE}}},
    })
    new_status = (updated.get("properties", {}).get("Status", {}).get("status") or {}).get("name")
    if new_status != _TASK_STATUS_DONE:
        return f'Error: update sent but Notion did not confirm "{matched}" as Done -- do not report this as done.'
    return f'Confirmed marked "{matched}" as Done in Notion Tasks.'


async def _search(query: str) -> str:
    if not query:
        return "Error: search requires a query."
    idea_data, task_data = await asyncio.gather(
        _request("POST", f"/databases/{_IDEAS_DB}/query",
                 {"filter": {"property": "Title", "title": {"contains": query}}}),
        _request("POST", f"/databases/{_TASKS_DB}/query",
                 {"filter": {"property": "Title", "title": {"contains": query}}}),
    )
    lines = [f'[Idea] {_title_text(r["properties"].get("Title"))}'
             for r in idea_data.get("results", [])[:10]]
    for r in task_data.get("results", [])[:10]:
        st = (r.get("properties", {}).get("Status", {}).get("status") or {}).get("name", "?")
        lines.append(f'[Task, {st}] {_title_text(r["properties"].get("Title"))}')
    if not lines:
        return f"Nothing in Notion matches {query!r}."
    return "Notion matches:\n" + "\n".join(f"- {l}" for l in lines)


async def notion_action(
    action: str,
    title: str = "",
    content: str = "",
    tags: list[str] | None = None,
    due: str = "",
    priority: str = "",
    query: str = "",
    status: str = "",
) -> str:
    err = _configured()
    if err:
        return err
    try:
        if action == "add_idea":
            return await _add_idea(title, content, tags or [])
        if action == "add_task":
            return await _add_task(title, due, priority)
        if action == "list_tasks":
            return await _list_tasks(status)
        if action == "complete_task":
            return await _complete_task(title)
        if action == "search":
            return await _search(query)
        return f"Unknown notion_action action: {action!r}"
    except Exception as e:
        log.error("Notion action %r failed: %s", action, e)
        return f"Error: Notion request failed -- {e}"
