"""A01: one owner for OpenAI-compatible client lifetime.

Before this, four call sites built their own clients. `engine._get_compat_client()`
cached one in a module global; `consolidation._call_compat()` and `research._llm()`
constructed a **brand new `AsyncOpenAI` on every single call** -- a fresh TLS handshake
per research tick, per consolidation, per morning brief, and never closed. That is the
roadmap's "several clients are constructed per call" finding, and it is also why nothing
could close them at shutdown: no one owned them.

This module owns creation, pooling, and shutdown. It does **not** own model selection.

**Configuration stays in `router.py`, deliberately.** The tempting move is to give this
module its own `OpenRouterSettings.from_env()` reading `OPENROUTER_STANDARD_MODEL` and
friends. That would create a *second* source of truth for which model is which tier,
sitting next to `router.OPENROUTER` -- and the roadmap is explicit in the opposite
direction: "Tier and reasoning policy must be explicit fields, not inferred from whether
two model strings happen to match." `router.route_tier()` (2026-09-21) exists precisely
because that fact must have exactly one derivation. So this module takes a
`router.Provider` and reads connection details off it; it never re-reads the environment.
"""
from __future__ import annotations

import inspect
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# Keyed on connection identity, not provider name: two descriptors pointing at the same
# endpoint with the same credentials should share one pool entry, and a CHANGED key must
# produce a different entry so a corrected credential takes effect on the very next call
# without a restart (the F3 recovery property, preserved).
_async_clients: dict[tuple, Any] = {}
_sync_clients: dict[tuple, Any] = {}
_lock = threading.Lock()


def _signature(provider) -> tuple:
    headers = tuple(sorted((provider.default_headers or {}).items()))
    # The api key participates in identity but is never logged; see _describe().
    return (provider.base_url, provider.api_key, headers)


def _describe(provider) -> str:
    """Safe identifier for logs -- endpoint and env var name, never the credential."""
    return f"{provider.name}@{provider.base_url}"


class ProviderNotConfigured(RuntimeError):
    """Raised instead of building a client from an empty API key.

    A deliberate exception to this codebase's silent-degradation contract, inherited from
    `engine._get_compat_client()`'s F3 hardening: a client built from `api_key=""` used to
    be cached and then fail every later turn with a raw 401, staying broken even after the
    key was fixed. Failing loudly and caching nothing keeps the recovery property.
    """


def require_configuration(provider) -> None:
    """Fail fast, before the runtime starts accepting work, if the provider has no key.

    Called from startup so a misconfigured deployment cannot come up looking healthy and
    then fail on the first real message.
    """
    if not provider.configured:
        raise ProviderNotConfigured(
            f"{provider.api_key_env} is not set -- every model call needs it. "
            f"Set it in .env and restart (docs/SETUP.md)."
        )


def get_async_client(provider):
    """Pooled `AsyncOpenAI` for this provider. Never caches an unconfigured client."""
    require_configuration(provider)
    key = _signature(provider)
    cached = _async_clients.get(key)
    if cached is not None:
        return cached
    with _lock:
        cached = _async_clients.get(key)
        if cached is not None:
            return cached
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            default_headers=dict(provider.default_headers or {}),
        )
        _async_clients[key] = client
        log.debug("gateway: opened async client for %s", _describe(provider))
        return client


def get_sync_client(provider):
    """Pooled synchronous `OpenAI` client.

    Kept separate from the async pool because Mem0's extraction path is synchronous and
    is driven from a worker thread. Sharing an async client across an unrelated event
    loop is the failure the roadmap warns about; a real sync client avoids it entirely.
    """
    require_configuration(provider)
    key = _signature(provider)
    cached = _sync_clients.get(key)
    if cached is not None:
        return cached
    with _lock:
        cached = _sync_clients.get(key)
        if cached is not None:
            return cached
        from openai import OpenAI

        client = OpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            default_headers=dict(provider.default_headers or {}),
        )
        _sync_clients[key] = client
        log.debug("gateway: opened sync client for %s", _describe(provider))
        return client


# A plain HTTP client for endpoints that are not OpenAI-shaped -- today only Jev's
# decisions API. One pooled connection matters here more than anywhere: the call sits on
# every turn, and a fresh TLS handshake measured +50ms, an idle-dropped one +180ms.
_http_client = None


def get_http_client():
    """Pooled `httpx.AsyncClient`. Timeouts are set per request by the caller."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        return _http_client
    with _lock:
        if _http_client is None or _http_client.is_closed:
            import httpx
            _http_client = httpx.AsyncClient(
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300))
            log.debug("gateway: opened http client")
        return _http_client


async def close_all() -> None:
    """Close every pooled client. Called from the shutdown sequence.

    Never raises: shutdown must continue even if one client's close misbehaves, and a
    failure here must not prevent the memory flush that runs alongside it.
    """
    global _http_client
    with _lock:
        clients = list(_async_clients.values()) + list(_sync_clients.values())
        if _http_client is not None:
            clients.append(_http_client)
        _http_client = None
        _async_clients.clear()
        _sync_clients.clear()
    for client in clients:
        try:
            # httpx's async client closes with aclose(); the SDK clients with close().
            result = (getattr(client, "aclose", None) or client.close)()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            log.warning("gateway: client close failed: %s", exc)
    if clients:
        log.info("gateway: closed %d pooled client(s)", len(clients))


def pool_size() -> tuple[int, int]:
    """(async, sync) pooled client counts -- for tests and status reporting."""
    return len(_async_clients), len(_sync_clients)


def _reset_for_tests() -> None:
    """Drop pooled clients without awaiting their close.

    Tests install fake clients whose event loops are already gone; awaiting close() on
    those raises noise unrelated to what is being tested.
    """
    global _http_client
    with _lock:
        _async_clients.clear()
        _sync_clients.clear()
        _http_client = None
