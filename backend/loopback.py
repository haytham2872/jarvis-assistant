"""`localhost` pinned to the IPv4 loopback the local services are actually bound on.

`mem0/docker-compose.yml` binds Qdrant and SearXNG to `127.0.0.1` only (deliberately:
an unbound port on a public VM would expose the memory database). On Windows,
`localhost` resolves to `::1` first, and a connect to an address nothing listens on does
not fail fast -- it stalls about two seconds before falling back to IPv4.

CLAUDE.md records this for `docker.ps1` (2026-08-28), where it was fixed in PowerShell.
The Python side kept `localhost` -- in `.env`, and as the default for SearXNG -- so every
Qdrant call and every web search on the desk paid the stall. Measured per request:

    Qdrant   (shared client)   localhost 2,055 ms   127.0.0.1   4.5 ms
    SearXNG  (/healthz)        localhost 2,556 ms   127.0.0.1 504 ms*

    * the remainder is per-request HTTP client construction, not the network

Because the compose file makes an IPv6 loopback bind impossible, `localhost` can only
ever mean the IPv4 loopback here, so it is rewritten rather than trusted.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

IPV4_LOOPBACK = "127.0.0.1"


def pin(host: str) -> str:
    h = (host or "").strip()
    return IPV4_LOOPBACK if h.lower() == "localhost" else h


def pin_url(url: str) -> str:
    """The same rewrite for a URL, preserving scheme, port, path and query."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "localhost":
        return url
    netloc = IPV4_LOOPBACK + (f":{parts.port}" if parts.port else "")
    if parts.username:
        cred = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{cred}@{netloc}"
    return urlunsplit(parts._replace(netloc=netloc))
