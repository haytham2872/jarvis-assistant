"""web_research: TinyFish's Research API as a background job (backend/tools/tinyfish.py
research(), backend/brain/background.py web_research()).

No test reaches the real API: the streaming client is faked. A live run is recorded in
CLAUDE.md ("Web — TinyFish").
"""
import asyncio
import json
import types

import pytest

from backend.brain import background
from backend.tools import tinyfish


def run(coro):
    return asyncio.run(coro)


def _sse(*events):
    return [f"data: {json.dumps(e)}" for e in events] + [""]


class _Stream:
    def __init__(self, status, lines, body=None, drop_after=None):
        self.status_code, self._lines, self._body, self._drop = status, lines, body, drop_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return b""

    def json(self):
        return self._body or {}

    async def aiter_lines(self):
        for i, line in enumerate(self._lines):
            if self._drop is not None and i == self._drop:
                raise OSError("connection reset")
            yield line


class _Client:
    def __init__(self, stream, polls=None, posts=None):
        self._stream, self._polls, self.posts = stream, list(polls or []), posts

    def __call__(self, *a, **kw):          # used as the AsyncClient class
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.request = json
        return self._stream

    async def get(self, url, headers=None):
        return self._polls.pop(0)

    async def post(self, url, headers=None, json=None):
        if self.posts is not None:
            self.posts.append(url)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "test-key")


REPORT = {"event": "final_result", "result": "# Fed\n\nRaised by 25bp [1].",
          "citations": ["https://federalreserve.gov/a", "https://cnbc.com/b"]}


# --- the client ----------------------------------------------------------------------------

def test_the_report_and_its_citations_come_from_the_final_event(key, monkeypatch):
    client = _Client(_Stream(200, _sse({"event": "created", "research_run_id": "r1"},
                                       {"event": "heartbeat"}, REPORT, {"event": "done"})))
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", client)
    report, cites = run(tinyfish.research("what did the Fed do", "deep"))
    assert report == "# Fed\n\nRaised by 25bp [1]."
    assert cites == ["https://federalreserve.gov/a", "https://cnbc.com/b"]
    assert client.request == {"query": "what did the Fed do", "mode": "deep"}


def test_an_unknown_mode_becomes_standard(key, monkeypatch):
    client = _Client(_Stream(200, _sse(REPORT)))
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", client)
    run(tinyfish.research("q", "max"))           # max costs wallet money; never sent
    assert client.request["mode"] == "standard"


def test_an_error_event_fails_the_run(key, monkeypatch):
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient",
                        _Client(_Stream(200, _sse({"event": "error", "message": "boom"}))))
    with pytest.raises(tinyfish.ResearchError, match="boom"):
        run(tinyfish.research("q"))


def test_the_daily_allowance_is_named(key, monkeypatch):
    body = {"error": {"code": "DAILY_LIMIT_EXCEEDED", "message": "limit",
                      "details": {"resets_at": "2026-09-30T07:00:00.000Z"}}}
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", _Client(_Stream(429, [], body)))
    with pytest.raises(tinyfish.ResearchError,
                       match="5 runs a day.*resets at 2026-09-30T07:00"):
        run(tinyfish.research("q"))


def test_no_key_is_refused_without_a_request(monkeypatch):
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", lambda *a, **k: pytest.fail("called"))
    with pytest.raises(tinyfish.ResearchError, match="TINYFISH_API_KEY"):
        run(tinyfish.research("q"))


def test_a_dropped_stream_polls_the_saved_run(key, monkeypatch):
    """The run carries on server-side when the connection drops, so its report is
    fetched rather than lost."""
    polls = [types.SimpleNamespace(status_code=200, json=lambda: {"status": "RUNNING"}),
             types.SimpleNamespace(status_code=200, json=lambda: {
                 "status": "COMPLETED",
                 "deep_result": {"result": "saved report", "citations": ["https://x.com"]}})]
    stream = _Stream(200, _sse({"event": "created", "research_run_id": "r9"},
                               {"event": "heartbeat"}), drop_after=1)
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", _Client(stream, polls))
    monkeypatch.setattr(tinyfish, "_POLL_S", 0)
    assert run(tinyfish.research("q")) == ("saved report", ["https://x.com"])


def test_a_failed_saved_run_raises(key, monkeypatch):
    polls = [types.SimpleNamespace(status_code=200, json=lambda: {"status": "TIMED_OUT"})]
    stream = _Stream(200, _sse({"event": "created", "research_run_id": "r9"}), drop_after=1)
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", _Client(stream, polls))
    with pytest.raises(tinyfish.ResearchError, match="timed out"):
        run(tinyfish.research("q"))


def test_a_cancelled_job_cancels_the_run(key, monkeypatch):
    """A cancelled run does not count against the day's five."""
    posts = []

    class _Hang(_Stream):
        async def aiter_lines(self):
            yield f'data: {json.dumps({"event": "created", "research_run_id": "r7"})}'
            await asyncio.sleep(3600)

    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", _Client(_Hang(200, []), posts=posts))

    async def go():
        task = asyncio.create_task(tinyfish.research("q"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)              # let the cancel request go out
    run(go())
    assert posts == [f"{tinyfish._RUN_URL}/r7/cancel"]


# --- the job -------------------------------------------------------------------------------

def test_the_tool_refuses_without_a_question_or_a_key(monkeypatch):
    assert run(background.web_research("  ")).startswith("Error:")
    assert "unavailable" in run(background.web_research("what did the Fed do"))


def test_the_tool_starts_a_research_job(key, monkeypatch):
    started = {}

    async def start(task, brief, **kw):
        started.update(task=task, **kw)
        return types.SimpleNamespace(id=5), ""
    monkeypatch.setattr(background, "_start", start)
    out = run(background.web_research("what did the Fed do", depth="nonsense"))
    assert started["kind"] == "web_research" and started["depth"] == "standard"
    assert started["task"] == "what did the Fed do"
    assert out.startswith("Research job #5 started (standard)") and "At most 5 of these run per day" in out


def test_the_worker_writes_the_report_with_numbered_sources(key, monkeypatch):
    notes, sent = [], []

    async def research(question, mode, on_run_id=None):
        assert (question, mode) == ("what did the Fed do", "deep")
        return "# Fed\n\nRaised [1].", ["https://federalreserve.gov/a", "https://cnbc.com/b"]

    async def note(t):
        notes.append(t.result)

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(tinyfish, "research", research)
    monkeypatch.setattr(background, "_write_result_note", note)
    monkeypatch.setattr(background, "_notifier", notifier)
    t = background.BgTask(id=2, description="what did the Fed do", kind="web_research",
                          depth="deep", notify=True)
    run(background._worker(t))
    assert t.status == "done"
    assert notes == ["# Fed\n\nRaised [1].\n\nSources:\n1. https://federalreserve.gov/a\n"
                     "2. https://cnbc.com/b"]
    assert sent and "Raised [1]." in sent[0]


def test_a_failed_research_job_says_why(key, monkeypatch):
    async def research(question, mode, on_run_id=None):
        raise tinyfish.ResearchError("today's research allowance (5 runs a day) is used up")
    monkeypatch.setattr(tinyfish, "research", research)
    t = background.BgTask(id=3, description="q", kind="web_research")
    run(background._worker(t))
    assert t.status == "failed" and "allowance" in t.summary


def test_a_research_job_times_out_on_its_own_budget(key, monkeypatch):
    async def research(question, mode, on_run_id=None):
        await asyncio.sleep(10)
    monkeypatch.setattr(tinyfish, "research", research)
    monkeypatch.setitem(tinyfish.RESEARCH_TIMEOUT_S, "standard", 0.05)
    t = background.BgTask(id=4, description="q", kind="web_research")
    run(background._worker(t))
    assert t.status == "failed" and t.summary.startswith("Timed out")


def test_kind_and_depth_survive_a_restart():
    t = background.BgTask(id=6, description="q", kind="web_research", depth="deep")
    payload = background._job_payload(t)
    assert (payload["kind"], payload["depth"]) == ("web_research", "deep")


def test_the_result_note_is_named_for_research(monkeypatch):
    written = {}

    async def vault_write(filename, content, folder):
        written.update(filename=filename, content=content)
        return "ok"
    from backend.tools import vault
    monkeypatch.setattr(vault, "vault_write", vault_write)
    t = background.BgTask(id=8, description="What did the Fed do?", kind="web_research",
                          status="done", result="report")
    run(background._write_result_note(t))
    assert written["filename"].startswith("research-8-")
    assert written["content"].startswith("# Web research #8\n\n**Question:** What did the Fed do?")


def test_research_is_not_offered_to_background_workers():
    """A job can't wait on another job: a night task calling web_research would end
    before the report arrived."""
    assert "web_research" not in background._ALLOWED_TOOLS


def test_a_queued_research_job_is_still_research_after_a_restart(monkeypatch):
    """Without its kind, a recovered job would run the question through the agent loop."""
    from backend.storage import jobs
    monkeypatch.setattr(background, "_tasks", {})
    t = background.BgTask(id=11, description="q", kind="web_research", depth="deep")
    assert jobs.enqueue(background._job_payload(t))
    background._tasks.clear()
    assert background.recover_durable_jobs() == 1
    back = background._tasks[11]
    assert (back.kind, back.depth) == ("web_research", "deep")
