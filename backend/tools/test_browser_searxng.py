"""Tests for backend/tools/browser.py's SearXNG empty-result diagnostic (2026-09-03).

Live incident: a full Discord research session degraded silently -- SearXNG
returned HTTP 200 with an empty results array because every one of its
upstream engines was suspended/rate-limited/CAPTCHA'd at once (confirmed live:
brave "too many requests", duckduckgo timeout, startpage CAPTCHA), forcing
every single web_browse call onto the noisier ddgs fallback chain with zero
log signal that the primary backend had gone dark. Combined with a broken
plan-continuation mechanism (see test_engine_progress_note.py), this drove the
model into a retry spiral that burned the tool-loop cap twice in a row.
Run with:
    .venv\\Scripts\\pytest backend/tools/test_browser_searxng.py -v
"""

import asyncio
import logging

import pytest

from backend.tools import browser


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, timeout=None):
        return _FakeResponse(self._payload)


def _install_fake_searxng(monkeypatch, payload):
    monkeypatch.setattr(browser.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(payload))


def test_searxng_empty_results_logs_warning_with_unresponsive_engines(monkeypatch, caplog):
    _install_fake_searxng(monkeypatch, {
        "results": [],
        "unresponsive_engines": [["brave", "Suspended: too many requests"], ["duckduckgo", "timeout"]],
    })
    with caplog.at_level(logging.WARNING, logger="backend.tools.browser"):
        results = asyncio.run(browser._search_searxng("kimi k3 pricing", max_results=6, fresh=False))
    assert results == []
    assert any("unresponsive" in r.message.lower() or "0 usable results" in r.message.lower()
               for r in caplog.records)
    assert any("brave" in r.message for r in caplog.records)


def test_searxng_with_real_results_produces_no_warning(monkeypatch, caplog):
    _install_fake_searxng(monkeypatch, {
        "results": [{"title": "A real result", "url": "https://example.com/x", "content": "snippet"}],
    })
    with caplog.at_level(logging.WARNING, logger="backend.tools.browser"):
        results = asyncio.run(browser._search_searxng("kimi k3 pricing", max_results=6, fresh=False))
    assert len(results) == 1
    assert not any("unresponsive" in r.message.lower() for r in caplog.records)


def test_searxng_empty_results_with_missing_unresponsive_key_degrades_cleanly(monkeypatch, caplog):
    """A 200 response with results:[] but no unresponsive_engines key at all
    (a different SearXNG version, or a genuinely-empty-for-this-query case)
    must still warn, just without naming engines -- never raise."""
    _install_fake_searxng(monkeypatch, {"results": []})
    with caplog.at_level(logging.WARNING, logger="backend.tools.browser"):
        results = asyncio.run(browser._search_searxng("obscure query", max_results=6, fresh=False))
    assert results == []
    assert any("0 usable results" in r.message.lower() for r in caplog.records)
