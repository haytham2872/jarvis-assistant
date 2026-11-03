"""One owner for Qdrant client lifetime, and the one place the host is resolved.

Sixteen call sites each built `QdrantClient(host=os.getenv("QDRANT_HOST", "localhost"))`
per call -- the per-call-client pattern A01 fixed for LLM clients and A10 then left in
place for Qdrant. Measured on the desk (a 1-collection `get_collection`, median of 15):

    host        new client per call   shared client
    localhost        2,518 ms            2,055 ms
    127.0.0.1          656 ms                4.5 ms

Two independent costs, both removed here:

* **`localhost`.** `mem0/docker-compose.yml` binds Qdrant to `127.0.0.1` only, and on
  Windows `localhost` resolves `::1` first; the connect to an address nothing listens on
  does not fail fast, it stalls ~2s before falling back. This is the exact interaction
  CLAUDE.md records for `docker.ps1` (2026-08-28) -- fixed there, never in Python, and
  `.env` still says `QDRANT_HOST=localhost`. Since the compose file makes an IPv6
  loopback bind impossible, `localhost` is mapped to `127.0.0.1` rather than trusted.
* **Construction.** A client costs ~650ms to build even on the right address (the HTTP
  client's TLS context and CA bundle are built eagerly), paid on every call.

The client is resolved from `qdrant_client.QdrantClient` at call time, not imported
once, because the suite fakes it by patching that attribute. Only the genuine class is
cached: a cached real client handed to a test that patched in a fake would silently
reach the real memory -- the isolation hole this codebase has already fallen into three
times.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

PORT = 6333

try:                                         # the defining module, which tests do not patch
    from qdrant_client.qdrant_client import QdrantClient as _REAL_CLIENT
except Exception:                            # pragma: no cover - qdrant-client missing
    _REAL_CLIENT = None

_lock = threading.Lock()
_clients: dict[tuple[str, float], object] = {}


def host() -> str:
    """QDRANT_HOST, with `localhost` pinned to the IPv4 loopback it is bound on."""
    from backend import loopback
    return loopback.pin(os.getenv("QDRANT_HOST") or "") or loopback.IPV4_LOOPBACK


def get_client(timeout: float = 3.0):
    """A process-wide client for `timeout`. Thread-safe; callers must not close it."""
    import qdrant_client

    cls = qdrant_client.QdrantClient
    if _REAL_CLIENT is None or cls is not _REAL_CLIENT:
        return cls(host=host(), port=PORT, timeout=timeout)   # a test fake: never cached
    key = (host(), float(timeout))
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = cls(host=key[0], port=PORT, timeout=timeout)
            _clients[key] = client
        return client


def close_all() -> None:
    """Release pooled connections at shutdown."""
    with _lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception as e:
                log.debug("qdrant: close failed: %s", e)
        _clients.clear()
