"""Tests for F3 (2026-09-08): _get_compat_client() must fail loudly when the
provider isn't configured, rather than silently building an AsyncOpenAI client
with an empty api_key that then fails every turn with a raw 401 -- and stays
failing forever, since the old code cached it. This is a DELIBERATE exception
to the silent-degradation contract the rest of the codebase follows.

The unconfigured signal is provider.configured being False, never the provider
being None. No real network call or real OpenAI SDK client is exercised --
engine._provider is monkeypatched to a fake Provider, and the client pool is
reset before/after each test.
"""

import pytest

from backend.brain import engine
from backend.brain.router import Provider


@pytest.fixture(autouse=True)
def _reset_compat_client():
    """The client pool must start (and end) each test empty, or an earlier test's
    pooled client would silently short-circuit this file's whole premise.

    A01 (2026-09-21): the cache moved from the engine's `_compat_client` module global into
    providers.gateway's pool. The F3 guarantees under test are unchanged -- what is
    asserted is the behavior (raises, caches nothing, recovers on the next call), not
    where the object happens to be stored."""
    from backend.providers import gateway
    gateway._reset_for_tests()
    yield
    gateway._reset_for_tests()


def _unconfigured_provider(name: str, api_key_env: str) -> Provider:
    """A Provider whose api_key_env genuinely isn't set -- .configured
    reads live via os.getenv, so as long as the test doesn't set this env
    var, .configured is False without any extra patching."""
    return Provider(
        name=name, base_url="https://example.invalid",
        api_key_env=api_key_env, standard="std", thinking="think",
        no_thinking_body={}, thinking_body={}, reasoning_echo_key="reasoning_content",
    )


def test_raises_when_provider_unconfigured(monkeypatch):
    provider = _unconfigured_provider("openrouter", "SOME_KEY_DOES_NOT_EXIST")
    monkeypatch.setattr(engine, "_provider", lambda: provider)
    with pytest.raises(RuntimeError, match="SOME_KEY_DOES_NOT_EXIST"):
        engine._get_compat_client()


def test_error_names_the_unconfigured_providers_own_env_var(monkeypatch):
    """The message must name the key the provider actually needs."""
    provider = _unconfigured_provider("openrouter", "OPENROUTER_API_KEY_DOES_NOT_EXIST")
    monkeypatch.setattr(engine, "_provider", lambda: provider)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY_DOES_NOT_EXIST"):
        engine._get_compat_client()


def test_nothing_is_cached_on_failure(monkeypatch):
    """The core of the fix: a failed attempt must not poison the module
    global, so a subsequently corrected environment recovers on the very
    next call instead of staying broken until a restart."""
    provider = _unconfigured_provider("openrouter", "OPENROUTER_API_KEY_DOES_NOT_EXIST")
    monkeypatch.setattr(engine, "_provider", lambda: provider)
    with pytest.raises(RuntimeError):
        engine._get_compat_client()
    from backend.providers import gateway
    assert gateway.pool_size() == (0, 0)   # nothing cached from a bad key


def test_recovers_on_next_call_once_configured(monkeypatch):
    """Simulates the key being fixed between two calls in the same process --
    no restart, no lingering broken client from the first failed attempt."""
    unconfigured = _unconfigured_provider("openrouter", "OPENROUTER_API_KEY_DOES_NOT_EXIST")
    monkeypatch.setattr(engine, "_provider", lambda: unconfigured)
    with pytest.raises(RuntimeError):
        engine._get_compat_client()
    from backend.providers import gateway
    assert gateway.pool_size() == (0, 0)   # nothing cached from a bad key

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake-key")
    configured = Provider(
        name="openrouter", base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY", standard="std", thinking="think",
        no_thinking_body={}, thinking_body={}, reasoning_echo_key="reasoning_content",
    )
    monkeypatch.setattr(engine, "_provider", lambda: configured)

    client = engine._get_compat_client()
    assert client is not None
    # A second call must reuse the now-valid pooled client, not rebuild it.
    assert engine._get_compat_client() is client
    from backend.providers import gateway
    assert gateway.pool_size() == (1, 0)


def test_succeeds_normally_when_configured(monkeypatch):
    """Regression guard: the common, already-configured case is unaffected --
    still builds and caches exactly one client, with no exception raised."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    provider = Provider(
        name="openrouter", base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY", standard="std", thinking="think",
        no_thinking_body={}, thinking_body={}, reasoning_echo_key="reasoning_content",
    )
    monkeypatch.setattr(engine, "_provider", lambda: provider)

    client1 = engine._get_compat_client()
    client2 = engine._get_compat_client()
    assert client1 is client2
