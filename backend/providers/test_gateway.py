"""A01: the client pool is the single owner of provider client lifetime.

Covers the properties the four former owners each had to get right independently, plus
the ones none of them had: pooling across call sites, and closing at shutdown.
"""
import asyncio

import pytest

from backend.brain.router import Provider
from backend.providers import gateway


def _provider(name="openrouter", env="OPENROUTER_API_KEY", base="https://example.invalid",
              headers=None):
    return Provider(
        name=name, base_url=base, api_key_env=env, standard="std", thinking="think",
        no_thinking_body={}, thinking_body={}, reasoning_echo_key="reasoning_content",
        default_headers=headers,
    )


class _Fake:
    closed = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gateway._reset_for_tests()
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _Fake)
    monkeypatch.setattr(openai, "OpenAI", _Fake)
    yield
    gateway._reset_for_tests()


def test_same_provider_reuses_one_client(monkeypatch):
    """The whole point: research ticks and consolidation used to build a fresh client
    (and TLS handshake) per call."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    p = _provider()
    assert gateway.get_async_client(p) is gateway.get_async_client(p)
    assert gateway.pool_size() == (1, 0)


def test_distinct_call_sites_share_the_pool(monkeypatch):
    """Two descriptors with identical connection identity are one pooled client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    assert gateway.get_async_client(_provider()) is gateway.get_async_client(_provider())
    assert gateway.pool_size() == (1, 0)


def test_unconfigured_provider_raises_and_caches_nothing(monkeypatch):
    """F3, preserved: never build a client from an empty key, never cache one."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(gateway.ProviderNotConfigured):
        gateway.get_async_client(_provider())
    assert gateway.pool_size() == (0, 0)


def test_corrected_key_takes_effect_on_the_next_call(monkeypatch):
    """The recovery property: a bad key must not poison the pool for the process."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    p = _provider()
    with pytest.raises(gateway.ProviderNotConfigured):
        gateway.get_async_client(p)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-now-valid")
    assert gateway.get_async_client(p) is not None
    assert gateway.pool_size() == (1, 0)


def test_changed_key_produces_a_different_pooled_client(monkeypatch):
    """A rotated credential must not keep serving from the stale client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-old")
    old = gateway.get_async_client(_provider())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-new")
    assert gateway.get_async_client(_provider()) is not old
    assert gateway.pool_size() == (2, 0)


def test_different_endpoints_do_not_share_a_client(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    monkeypatch.setenv("OTHER_API_KEY", "sk-b")
    a = gateway.get_async_client(_provider())
    b = gateway.get_async_client(
        _provider("other", "OTHER_API_KEY", "https://other.example.invalid/v1"))
    assert a is not b
    assert gateway.pool_size() == (2, 0)


def test_headers_participate_in_identity(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    plain = gateway.get_async_client(_provider())
    titled = gateway.get_async_client(_provider(headers={"X-Title": "JARVIS"}))
    assert plain is not titled


def test_sync_and_async_pools_are_separate(monkeypatch):
    """Mem0's extraction path is synchronous and driven from a worker thread. Handing it
    an async client bound to another event loop is the failure this separation avoids."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    p = _provider()
    assert gateway.get_async_client(p) is not gateway.get_sync_client(p)
    assert gateway.pool_size() == (1, 1)


def test_close_all_closes_both_pools_and_empties_them(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    p = _provider()
    a, sy = gateway.get_async_client(p), gateway.get_sync_client(p)
    asyncio.run(gateway.close_all())
    assert a.closed and sy.closed
    assert gateway.pool_size() == (0, 0)


def test_close_all_survives_a_misbehaving_client(monkeypatch):
    """Shutdown must continue to the memory flush even if a close misbehaves."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")

    class _Bad(_Fake):
        def close(self):
            raise RuntimeError("boom")

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _Bad)
    gateway.get_async_client(_provider())
    asyncio.run(gateway.close_all())          # must not raise
    assert gateway.pool_size() == (0, 0)


def test_close_all_awaits_an_async_close(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    seen = {}

    class _AsyncClose(_Fake):
        async def close(self):
            seen["closed"] = True

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _AsyncClose)
    gateway.get_async_client(_provider())
    asyncio.run(gateway.close_all())
    assert seen.get("closed") is True


def test_require_configuration_names_the_env_var(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(gateway.ProviderNotConfigured, match="OPENROUTER_API_KEY"):
        gateway.require_configuration(_provider())


def test_gateway_reads_no_configuration_from_the_environment():
    """Guards the design decision: configuration has ONE owner (router.py).

    A `from_env()` here -- which is what the codex branch's providers/openrouter.py does
    -- would recreate the two-sources-of-truth problem router.route_tier() exists to
    prevent, and would silently disagree with router.py about which model is which tier.
    Checked against the parsed AST rather than the text, so the module can *discuss* env
    vars in its docstring while never reading one.
    """
    import ast

    tree = ast.parse(open(gateway.__file__, encoding="utf-8").read())
    reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ"):
            reads.append(node.attr)
        if isinstance(node, ast.Name) and node.id == "environ":
            reads.append(node.id)
    assert reads == [], (
        f"gateway.py reads the environment ({reads}). It owns client lifetime, not "
        "model/tier selection -- configuration belongs to router.py alone."
    )
    assert "import os" not in open(gateway.__file__, encoding="utf-8").read().split('"""')[-1]
