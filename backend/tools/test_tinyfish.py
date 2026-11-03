"""TinyFish as the primary web backend: the client (backend/tools/tinyfish.py) and how
browser.py chains it with the keyless fallbacks.

No test reaches the real API: conftest unsets TINYFISH_API_KEY, and the tests that need
the TinyFish path set a fake key and fake the HTTP client.
"""
import asyncio
import time

import pytest

from backend.tools import browser, outcomes, tinyfish


class _Resp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient; records every request."""
    calls: list = []
    response = _Resp()
    raises: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        _Client.calls.append(("GET", url, params, headers))
        if _Client.raises:
            raise _Client.raises
        return _Client.response

    async def post(self, url, json=None, headers=None, timeout=None):
        _Client.calls.append(("POST", url, json, headers))
        if _Client.raises:
            raise _Client.raises
        return _Client.response


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "test-key")
    _Client.calls, _Client.response, _Client.raises = [], _Resp(), None
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", _Client)
    return _Client


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_maps_results_and_sends_the_key(fake_api):
    fake_api.response = _Resp(payload={"results": [
        {"position": 1, "site_name": "a.com", "title": "A", "url": "https://a.com/x",
         "snippet": "about  A", "date": "5 hours ago"},
        {"position": 2, "site_name": "b.com", "title": "", "url": "https://b.com"},   # no title
        {"position": 3, "site_name": "c.com", "title": "C", "url": "ftp://c.com"},    # not http
    ]})
    got = run(tinyfish.search("nvda price"))
    assert got == [{"title": "A", "url": "https://a.com/x", "snippet": "about A",
                    "age": "5 hours ago"}]
    method, url, params, headers = fake_api.calls[0]
    assert (method, url) == ("GET", tinyfish._SEARCH_URL)
    assert params == {"query": "nvda price"}
    assert headers == {"X-API-Key": "test-key"}


def test_search_filters_become_api_parameters(fake_api):
    run(tinyfish.search("fed", kind="news", recent_days=7, sites=["reuters.com", "ft.com"]))
    params = fake_api.calls[0][2]
    assert params["domain_type"] == "news"
    assert params["recency_minutes"] == 7 * 1440
    assert params["include_domains"] == "reuters.com,ft.com"


def test_papers_never_send_a_date_filter(fake_api):
    """The API refuses date filters for research papers."""
    run(tinyfish.search("rag", kind="papers", recent_days=30))
    params = fake_api.calls[0][2]
    assert params["domain_type"] == "research_paper"
    assert "recency_minutes" not in params


def test_news_and_paper_details_ride_on_the_snippet_line(fake_api):
    fake_api.response = _Resp(payload={"results": [
        {"title": "Fed hikes", "url": "https://r.com/1", "snippet": "rates up",
         "publisher": "Reuters", "date": "1 day ago"},
        {"title": "RAG paper", "url": "https://arxiv.org/abs/1", "snippet": "",
         "authors": ["A", "B", "C", "D"], "venue": "NeurIPS", "year": 2026,
         "cited_by_count": 51, "pdf_url": "https://arxiv.org/pdf/1"},
    ]})
    news, paper = run(tinyfish.search("x"))
    assert news["snippet"] == "Reuters: rates up"
    assert paper["snippet"] == "A, B, C et al. — NeurIPS — cited by 51 — PDF: https://arxiv.org/pdf/1"
    assert paper["age"] == "2026"


def test_search_caps_results(fake_api):
    fake_api.response = _Resp(payload={"results": [
        {"title": f"t{i}", "url": f"https://x.com/{i}", "snippet": ""} for i in range(10)]})
    assert len(run(tinyfish.search("x", max_results=6))) == 6


def test_no_key_means_no_request(monkeypatch):
    called = []
    monkeypatch.setattr(tinyfish.httpx, "AsyncClient", lambda *a, **k: called.append(1))
    assert run(tinyfish.search("x")) is None
    assert run(tinyfish.fetch(["https://a.com"])) is None
    assert called == []


@pytest.mark.parametrize("status,headers,low,high", [
    (429, {"retry-after": "17"}, 16, 18),
    (401, {}, tinyfish._AUTH_PAUSE_S - 2, tinyfish._AUTH_PAUSE_S + 1),
    (503, {}, tinyfish._DEFAULT_PAUSE_S - 2, tinyfish._DEFAULT_PAUSE_S + 1),
])
def test_a_failure_pauses_the_provider(fake_api, status, headers, low, high):
    fake_api.response = _Resp(status=status, headers=headers)
    assert run(tinyfish.search("x")) is None
    remaining = tinyfish._pause_until["search"] - time.time()
    assert low <= remaining <= high
    # While paused nothing is sent, and fetch is paused separately.
    n = len(fake_api.calls)
    assert run(tinyfish.search("y")) is None
    assert len(fake_api.calls) == n
    assert tinyfish.available("fetch")


def test_a_network_error_pauses_and_returns_none(fake_api):
    fake_api.raises = OSError("connection reset")
    assert run(tinyfish.search("x")) is None
    assert not tinyfish.available("search")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def test_fetch_maps_pages_and_errors_and_asks_for_fresh_copies(fake_api):
    fake_api.response = _Resp(payload={
        "results": [{"url": "https://a.com", "title": "A", "text": "# A\nbody",
                     "published_date": "2026-09-01", "links": ["https://a.com/b"]}],
        "errors": [{"url": "https://b.com", "error": "page_not_found", "status": 404}],
    })
    got = run(tinyfish.fetch(["https://a.com", "https://b.com"], links=True))
    assert got["https://a.com"]["text"] == "# A\nbody"
    assert got["https://a.com"]["links"] == ["https://a.com/b"]
    assert got["https://b.com"] == {"error": "page_not_found", "status": 404}
    body = fake_api.calls[0][2]
    # Without a ttl TinyFish serves any cached copy -- measured: a stale NVDA price.
    assert body["ttl"] == tinyfish._FETCH_TTL_S
    assert body["links"] is True and body["format"] == "markdown"


def test_fetch_failure_pauses_fetch_only(fake_api):
    fake_api.response = _Resp(status=500)
    assert run(tinyfish.fetch(["https://a.com"])) is None
    assert not tinyfish.available("fetch")
    assert tinyfish.available("search")


# ---------------------------------------------------------------------------
# browser.py: the chain
# ---------------------------------------------------------------------------

@pytest.fixture
def chain(monkeypatch):
    """Record which fallback layers run; each returns what the test sets."""
    seen = {"searxng": [], "ddgs": [], "mojeek": [], "static": []}
    answers = {"tinyfish": None, "searxng": [], "ddgs": [], "mojeek": [],
               "fetch": None, "static": "static page text " * 20}
    browser._served.clear()

    async def tf_search(query, **kw):
        seen.setdefault("tinyfish", []).append((query, kw))
        return answers["tinyfish"]

    async def tf_fetch(urls, links=False, timeout_ms=0):
        seen.setdefault("fetch", []).append((urls, links))
        seen.setdefault("timeouts", []).append(timeout_ms)
        return answers["fetch"]

    async def searxng(query, max_results, fresh, time_range="", news=False):
        seen["searxng"].append((query, time_range, news))
        return answers["searxng"]

    async def ddgs(query, max_results):
        seen["ddgs"].append(query)
        return answers["ddgs"]

    async def mojeek(query, max_results):
        seen["mojeek"].append(query)
        return answers["mojeek"]

    async def static(url, cap=0, wikipedia=True):
        seen["static"].append(url)
        return answers["static"]

    monkeypatch.setattr(tinyfish, "search", tf_search)
    monkeypatch.setattr(tinyfish, "fetch", tf_fetch)
    monkeypatch.setattr(browser, "_search_searxng", searxng)
    monkeypatch.setattr(browser, "_search_ddgs", ddgs)
    monkeypatch.setattr(browser, "_search_mojeek", mojeek)
    monkeypatch.setattr(browser, "_static_fetch", static)
    return seen, answers


R = [{"title": "T", "url": "https://t.com/a", "snippet": "s", "age": ""}]


def test_tinyfish_answers_and_the_fallbacks_never_run(chain):
    seen, answers = chain
    answers["tinyfish"] = R
    assert run(browser._search("q")) == R
    assert seen["searxng"] == seen["ddgs"] == []
    assert browser._served["search:tinyfish"] == 1


def test_fallbacks_run_in_order_and_approximate_the_filters(chain):
    seen, answers = chain
    answers["ddgs"] = R
    got = run(browser._search("fed decision", kind="news", recent_days=3,
                              sites=["reuters.com", "ft.com"]))
    assert got == R
    q = "fed decision site:reuters.com OR site:ft.com"
    assert seen["searxng"] == [(q, "week", True)]
    assert seen["ddgs"] == [q]
    assert seen["mojeek"] == []
    assert browser._served["search:ddgs"] == 1


def test_every_layer_empty_is_counted_as_failed(chain):
    assert run(browser._search("q")) == []
    assert browser._served["search:failed"] == 1


def test_page_read_prefers_tinyfish(chain):
    seen, answers = chain
    answers["fetch"] = {"https://x.com": {"text": "rendered page", "links": []}}
    assert run(browser._read_page("https://x.com", cap=1000)) == "rendered page"
    assert seen["static"] == []
    assert browser._served["page:tinyfish"] == 1


def test_a_dead_page_is_reported_without_a_second_try(chain):
    seen, answers = chain
    answers["fetch"] = {"https://x.com/gone": {"error": "page_not_found", "status": 404}}
    out = run(browser._read_page("https://x.com/gone", cap=1000))
    assert out == "Could not fetch https://x.com/gone: page not found (HTTP 404)"
    assert seen["static"] == []


def test_a_blocked_page_gets_one_direct_try(chain):
    seen, answers = chain
    answers["fetch"] = {"https://x.com": {"error": "bot_blocked", "status": None}}
    assert run(browser._read_page("https://x.com", cap=1000)) == answers["static"]
    assert seen["static"] == ["https://x.com"]
    assert browser._served["page:direct"] == 1


def test_both_failing_names_both(chain):
    seen, answers = chain
    answers["fetch"] = {"https://x.com": {"error": "bot_blocked"}}
    answers["static"] = "Could not fetch https://x.com: 403 Forbidden"
    out = run(browser._read_page("https://x.com", cap=1000))
    assert out == ("Could not fetch https://x.com: the site blocks automated readers; "
                   "a direct fetch failed too")
    assert browser._served["page:failed"] == 1


def test_without_tinyfish_the_direct_fetch_is_used(chain):
    seen, answers = chain
    assert run(browser._read_page("https://x.com", cap=1000)) == answers["static"]
    assert seen["static"] == ["https://x.com"]


def test_wikipedia_still_uses_its_api_first(chain, monkeypatch):
    seen, answers = chain

    async def wiki(lang, title, cap):
        return f"extract of {title}"
    monkeypatch.setattr(browser, "_fetch_wikipedia_extract", wiki)
    out = run(browser._read_page("https://en.wikipedia.org/wiki/Viktor_Frankl", cap=1000))
    assert out == "extract of Viktor_Frankl"
    assert seen.get("fetch") is None


def test_links_are_listed_and_capped(chain, monkeypatch):
    seen, answers = chain
    monkeypatch.setattr(browser, "_MAX_LINKS", 2)
    answers["fetch"] = {"https://x.com": {"text": "page",
                                          "links": ["https://x.com/1", "https://x.com/2",
                                                    "https://x.com/3"]}}
    out = run(browser.fetch_page("https://x.com", links=True))
    assert seen["fetch"] == [(["https://x.com"], True)]
    assert "--- Links on this page (3) ---\nhttps://x.com/1\nhttps://x.com/2\n[...1 more not shown]" in out
    assert out.startswith(browser._UNTRUSTED_BANNER)


def test_web_browse_output_layout_is_unchanged(chain):
    """people_research parses this layout: header, numbered results, then the top page
    under its content header."""
    seen, answers = chain
    answers["tinyfish"] = [{"title": "Fed hikes", "url": "https://r.com/1",
                            "snippet": "Reuters: rates up", "age": "1 day ago"}]
    answers["fetch"] = {"https://r.com/1": {"text": "full article", "links": []}}
    out = run(browser.web_browse("search for fed decision", kind="news"))
    assert out == browser._UNTRUSTED_BANNER + (
        "Search results for: fed decision\n\n"
        "1. Fed hikes  [1 day ago]\n"
        "   https://r.com/1\n"
        "   Reuters: rates up\n\n"
        "--- Content from top result (https://r.com/1) ---\n"
        "full article")
    assert seen["tinyfish"][0][1]["kind"] == "news"


def test_web_browse_cleans_its_options(chain):
    seen, answers = chain
    answers["tinyfish"] = R
    run(browser.web_browse("x", kind="podcasts", recent_days="0",
                           sites="https://www.FT.com/markets, reuters.com; nonsense"))
    kw = seen["tinyfish"][0][1]
    assert kw["kind"] == "web"
    assert kw["recent_days"] is None
    assert kw["sites"] == ["www.ft.com", "reuters.com"]


def test_an_unreadable_page_is_still_a_failed_outcome(chain):
    _, answers = chain
    answers["fetch"] = {"https://x.com": {"error": "login_required"}}
    out = run(browser.fetch_page("https://x.com"))
    assert outcomes.classify("fetch_page", out) == outcomes.FAILED


def test_served_line(chain):
    browser._served.update({"search:tinyfish": 3, "search:ddgs": 1, "page:tinyfish": 2})
    assert browser.served_line() == "Web (this run): searches tinyfish 3, ddgs 1 | pages tinyfish 2"
    browser._served.clear()
    assert browser.served_line() == ""


def test_a_page_asked_for_by_name_waits_longer_than_a_preview(chain):
    seen, answers = chain
    answers["tinyfish"] = R
    answers["fetch"] = {"https://t.com/a": {"text": "page", "links": []}}
    run(browser.web_browse("search for x"))              # top-result preview
    run(browser.fetch_page("https://t.com/a"))
    run(browser.web_browse("read https://t.com/a"))
    assert seen["timeouts"] == [tinyfish.PREVIEW_TIMEOUT_MS, tinyfish.READ_TIMEOUT_MS,
                                tinyfish.READ_TIMEOUT_MS]


def test_the_request_timeout_outlasts_the_per_page_budget(fake_api):
    """A client timeout shorter than TinyFish's own per-URL budget would cut off a slow
    page TinyFish was about to return."""
    seen = {}

    async def post(self, url, json=None, headers=None, timeout=None):
        seen["timeout"], seen["budget"] = timeout, json["per_url_timeout_ms"]
        return _Resp(payload={"results": []})
    fake_api.post = post
    run(tinyfish.fetch(["https://a.com"], timeout_ms=tinyfish.READ_TIMEOUT_MS))
    assert seen["timeout"] > seen["budget"] / 1000
