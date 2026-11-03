"""
backend/tools/tinyfish.py — TinyFish Search and Fetch, the primary web backend.

Why TinyFish: every keyless backend before it scraped a search engine from the
server's datacenter address and was rate-limited or CAPTCHA'd (SearXNG's upstream
engines 304 of 304 empty on the server, Brave suspended, DuckDuckGo and Startpage
CAPTCHA). TinyFish is an API built for agents, called with a key, so there is no
scraping to block. Search and Fetch never draw from the wallet ("free at any
balance, including $0"), verified live 2026-09-29: the balance did not move after
real searches, fetches and a research run.

  Search  GET  https://api.search.tinyfish.ai   30 requests/min per key
  Fetch   POST https://api.fetch.tinyfish.ai    150 URLs/min per key; renders
          JavaScript in a real browser when a page needs it, up to 10 URLs a call

Fetch caches pages on TinyFish's side and, with no `ttl`, serves any cached copy:
measured, that returned a stale after-hours NVDA price. Every fetch sends `ttl=60`, so
a page is at most a minute old: a scheduled price check at 15:31 must not read a copy
from before the open. Measured on four pages, ttl 600, 60 and 0 took the same time
within noise (1.5-8.4s, no pattern), so freshness costs nothing noticeable.

A provider that fails is paused rather than retried on every call
(`_pause_until`): a key problem pauses 30 minutes, a rate limit until the
`Retry-After` it names, anything else one minute. The caller falls back to the
keyless chain meanwhile. The key is read at call time, so the test suite (which
unsets it) never reaches the real API.

`search` and `fetch` return None when TinyFish could not answer at all, so the
caller knows to fall back; an empty list or a per-URL error is an answer.

  Research POST https://agent.tinyfish.ai/v1/automation/run-research  (beta)
          Searches, reads and cross-checks sources, then writes a cited report. Free
          like the above (wallet unchanged after a live run), but 5 runs per Pacific
          day and 5 at once. Measured: "standard" took 2m23s, searched 35 sources,
          read 12, and returned an 11,891-char report with 20 citations, all correct
          on the September 2026 FOMC decision. `research` raises ResearchError with a
          sentence fit to tell the owner; there is no fallback for it.
"""

import asyncio
import json
import logging
import os
import time

import httpx
from backend.tasks import spawn

log = logging.getLogger(__name__)

_SEARCH_URL = "https://api.search.tinyfish.ai"
_FETCH_URL = "https://api.fetch.tinyfish.ai"
_SEARCH_TIMEOUT = 12.0        # measured p50 1.6s, max 4.7s over 40 real queries
# Per URL. A search's top-result preview waits 20s; a page JARVIS asked for by name
# waits 45s. Measured: etfdb.com took 31.7s on its first (live) render, 2.2s cached.
PREVIEW_TIMEOUT_MS = 20_000
READ_TIMEOUT_MS = 45_000
_FETCH_TTL_S = 60             # accept a cached page up to a minute old

_AUTH_PAUSE_S = 1800.0
_DEFAULT_PAUSE_S = 60.0

_pause_until: dict[str, float] = {"search": 0.0, "fetch": 0.0}

# Fetch's per-URL errors that another fetcher cannot fix: the page is gone, walled
# or not a real address. Anything else (bot_blocked, timeout, empty_content...) is
# worth one direct try.
FINAL_FETCH_ERRORS = frozenset({
    "page_not_found", "login_required", "invalid_url", "invalid_redirect_url",
    "content_too_large",
})

KINDS = {"web": "web", "news": "news", "papers": "research_paper"}


def _key() -> str:
    return os.getenv("TINYFISH_API_KEY", "").strip()


def configured() -> bool:
    return bool(_key())


def available(api: str) -> bool:
    return configured() and time.time() >= _pause_until.get(api, 0.0)


def _pause(api: str, resp: httpx.Response | None, error: object) -> None:
    status = resp.status_code if resp is not None else None
    if status in (401, 402, 403):
        seconds = _AUTH_PAUSE_S
    elif status == 429:
        try:
            seconds = max(1.0, float(resp.headers.get("retry-after", "")))
        except ValueError:
            seconds = _DEFAULT_PAUSE_S
    else:
        seconds = _DEFAULT_PAUSE_S
    _pause_until[api] = time.time() + seconds
    log.warning("TinyFish %s unavailable (%s) — using the fallback for %.0fs",
                api, status or error, seconds)


async def search(query: str, *, kind: str = "web", recent_days: int | None = None,
                 sites: list[str] | None = None, max_results: int = 6) -> list[dict] | None:
    """[{title, url, snippet, age}] from TinyFish Search, or None if it failed."""
    if not available("search"):
        return None
    params: dict = {"query": query[:2000]}
    if kind in KINDS and kind != "web":
        params["domain_type"] = KINDS[kind]
    if recent_days and kind != "papers":   # date filters are refused for papers
        params["recency_minutes"] = min(int(recent_days) * 1440, 5_256_000)
    if sites:
        params["include_domains"] = ",".join(sites)
    resp = None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_SEARCH_URL, params=params, headers={"X-API-Key": _key()},
                                    timeout=_SEARCH_TIMEOUT)
        if resp.status_code != 200:
            _pause("search", resp, resp.text[:200])
            return None
        items = resp.json().get("results") or []
    except Exception as e:
        _pause("search", resp, e)
        return None

    results = []
    for item in items:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not url.startswith("http") or not title:
            continue
        results.append({
            "title": title,
            "url": url,
            "snippet": _snippet(item),
            "age": str(item.get("date") or item.get("year") or "").strip(),
        })
        if len(results) >= max_results:
            break
    return results


def _snippet(item: dict) -> str:
    """The result's snippet, led by who published it and, for papers, where and
    how cited — on the snippet line so the result layout other code parses stays
    the same."""
    lead = []
    if item.get("publisher"):
        lead.append(str(item["publisher"]))
    if item.get("authors"):
        names = [str(a) for a in item["authors"][:3]]
        lead.append(", ".join(names) + (" et al." if len(item["authors"]) > 3 else ""))
    if item.get("venue"):
        lead.append(str(item["venue"]))
    if item.get("cited_by_count") is not None:
        lead.append(f"cited by {item['cited_by_count']}")
    if item.get("pdf_url"):
        lead.append(f"PDF: {item['pdf_url']}")
    snippet = " ".join(str(item.get("snippet") or "").split())
    if not lead:
        return snippet
    return " — ".join(lead) + (f": {snippet}" if snippet else "")


async def fetch(urls: list[str], *, links: bool = False,
                timeout_ms: int = PREVIEW_TIMEOUT_MS) -> dict | None:
    """{url: {"text", "title", "published_date", "links"} | {"error", "status"}}
    for each URL, or None if TinyFish could not be asked at all."""
    if not available("fetch") or not urls:
        return None
    body = {"urls": urls[:10], "format": "markdown", "ttl": _FETCH_TTL_S,
            "per_url_timeout_ms": timeout_ms}
    if links:
        body["links"] = True
    resp = None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(_FETCH_URL, json=body, headers={"X-API-Key": _key()},
                                     timeout=timeout_ms / 1000 + 5)
        if resp.status_code != 200:
            _pause("fetch", resp, resp.text[:200])
            return None
        data = resp.json()
    except Exception as e:
        _pause("fetch", resp, e)
        return None

    out: dict[str, dict] = {}
    for r in data.get("results") or []:
        out[r.get("url", "")] = {
            "text": r.get("text") if isinstance(r.get("text"), str) else
                    (json.dumps(r["text"]) if r.get("text") else ""),
            "title": r.get("title") or "",
            "published_date": r.get("published_date") or "",
            "links": r.get("links") or [],
        }
    for e in data.get("errors") or []:
        out[e.get("url", "")] = {"error": e.get("error") or "unknown", "status": e.get("status")}
    return out


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

_RESEARCH_URL = "https://agent.tinyfish.ai/v1/automation/run-research"
_RUN_URL = "https://agent.tinyfish.ai/v1/research-run"
RESEARCH_MODES = ("standard", "deep")
RESEARCH_TIMEOUT_S = {"standard": 900.0, "deep": 1800.0}   # documented 2-10 / 5-20 min
_POLL_S = 20.0
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


class ResearchError(Exception):
    """A research run that produced no report; the message is fit to pass on."""


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
    except Exception:
        err = {}
    code, msg = err.get("code", ""), err.get("message", "")
    if code == "DAILY_LIMIT_EXCEEDED":
        resets = (err.get("details") or {}).get("resets_at", "")
        return ("today's research allowance (5 runs a day) is used up"
                + (f"; it resets at {resets}" if resets else ""))
    if resp.status_code == 429:
        return "5 research runs are already in progress; wait for one to finish"
    if resp.status_code in (401, 403):
        return f"the research service refused the key ({msg or resp.status_code})"
    return f"the research service returned {resp.status_code}: {msg or 'no detail'}"


def _report(result: object, citations: list) -> tuple[str, list[str]]:
    text = result if isinstance(result, str) else str(result or "")
    urls = [str(c.get("url") if isinstance(c, dict) else c) for c in citations or []]
    return text.strip(), [u for u in urls if u]


async def research(question: str, mode: str = "standard",
                   on_run_id=None) -> tuple[str, list[str]]:
    """(report markdown, citation URLs) for `question`. Raises ResearchError.

    Streams the run's events; if the connection drops after the run was created, the
    run carries on server-side, so this polls the saved run until it finishes. Cancelled
    (a timeout or shutdown), it cancels the run too, which then does not count against
    the day's allowance.
    """
    run: dict = {"on_run_id": on_run_id}
    try:
        return await _research(question, mode, run)
    except asyncio.CancelledError:
        if run.get("id"):
            spawn(cancel_research(run["id"]))
        raise


async def _research(question: str, mode: str, run: dict) -> tuple[str, list[str]]:
    if not configured():
        raise ResearchError("TINYFISH_API_KEY is not set, so web research is unavailable")
    mode = mode if mode in RESEARCH_MODES else "standard"
    headers = {"X-API-Key": _key()}
    run_id = ""
    try:
        timeout = httpx.Timeout(30.0, read=180.0)   # heartbeats arrive well inside this
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", _RESEARCH_URL, headers=headers,
                                     json={"query": question[:2000], "mode": mode}) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise ResearchError(_error_message(resp))
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        continue
                    kind = event.get("event")
                    if kind == "created":
                        run_id = run["id"] = event.get("research_run_id") or ""
                        # G2: a goal's child job keeps the remote id durably the moment it is
                        # known, so a lost acceptance is reconciled, never relaunched.
                        if run_id and run.get("on_run_id"):
                            try:
                                run["on_run_id"](run_id)
                            except Exception as cb_exc:
                                log.debug("TinyFish: run id callback failed (%s)", cb_exc)
                    elif kind == "final_result":
                        report = _report(event.get("result"), event.get("citations"))
                        if report[0]:
                            return report
                    elif kind == "error":
                        raise ResearchError("the research run failed: "
                                            + str(event.get("message") or event.get("error")
                                                  or "no detail"))
    except ResearchError:
        raise
    except Exception as e:
        if not run_id:
            raise ResearchError(f"could not reach the research service ({e})") from e
        log.warning("TinyFish research %s: stream dropped (%s) — polling the saved run", run_id, e)
    if not run_id:
        raise ResearchError("the research run ended without a report")
    return await _poll_run(run_id, headers)


async def _poll_run(run_id: str, headers: dict) -> tuple[str, list[str]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                resp = await client.get(f"{_RUN_URL}/{run_id}", headers=headers)
                run = resp.json() if resp.status_code == 200 else {}
            except Exception:
                run = {}
            status = run.get("status", "")
            if status in _TERMINAL:
                deep = run.get("deep_result") or {}
                quick = run.get("quick_result") or {}
                text, cites = _report(deep.get("result") or quick.get("answer"),
                                      deep.get("citations") or quick.get("citations"))
                if status == "COMPLETED" and text:
                    return text, cites
                raise ResearchError(f"the research run ended {status.lower().replace('_', ' ')}"
                                    " without a report")
            await asyncio.sleep(_POLL_S)


async def cancel_research(run_id: str) -> None:
    """Best-effort: a cancelled run does not count against the daily allowance."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{_RUN_URL}/{run_id}/cancel", headers={"X-API-Key": _key()})
    except Exception as e:
        log.debug("TinyFish research cancel failed for %s: %s", run_id, e)
