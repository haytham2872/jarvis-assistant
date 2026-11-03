"""A01: a project-owned Mem0 LLM adapter for the provider (OpenRouter).

**Why not just set `provider: "openai"`.** The installed `mem0.llms.openai.OpenAILLM`
already auto-detects `OPENROUTER_API_KEY` and silently redirects to OpenRouter, which
looks like a free migration and is a trap here:

1. **It cannot disable reasoning.** Its OpenRouter branch builds `models`, `route` and
   `extra_headers`, and has no path to `extra_body`. DeepSeek V4.1 Flash reasons by
   default and that reasoning shares `max_tokens` with the answer -- the exact starvation
   failure `router.OPENROUTER_NO_THINKING_BODY` exists to
   prevent, already hit live twice (see CLAUDE.md's 2026-09-01 hardening notes). Fact
   extraction returns strict JSON, so it is precisely the call shape that starves.
2. **It builds its own client**, outside the gateway pool -- a fresh connection per
   `Memory` instance, invisible to shutdown.
3. **It ignores the configured api_key** whenever `OPENROUTER_API_KEY` is set, so the
   credential in use stops being the one the config names.
4. **No usage capture**, so extraction spend stays missing from `/cost`.

The roadmap anticipates exactly this: "If the installed adapter cannot pass the necessary
fields, add a project-owned adapter with a narrow contract; do not patch installed package
files." This is that adapter. It follows the precedent already in `memory.py`, where
`_install_shared_embedder()` supplies a project-owned embedding class the same way.

Synchronous on purpose: Mem0's `add()`/`search()` are sync and already run in a thread
executor. It uses `gateway.get_sync_client()`, a real sync client -- driving an async
client from an unrelated event loop is the failure mode the roadmap warns about.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# The provider name used in mem0's config.
#
# A distinct name like "jarvis_compat" was the first choice and DOES NOT WORK: mem0
# validates `llm.provider` twice against two different lists. `LlmFactory.
# provider_to_class` is an ordinary dict we can extend, but `mem0.llms.configs.LlmConfig`
# also has a pydantic field_validator carrying a hardcoded tuple of accepted names, so
# `Memory.from_config()` rejects anything outside it with "Unsupported LLM provider"
# before the factory is ever consulted. Caught by an end-to-end run against real Qdrant;
# the unit tests missed it because they call LlmFactory.create() directly and bypass
# config validation.
#
# So the config name stays one pydantic accepts, and `register()` repoints that name's
# FACTORY ENTRY at this class. "openai" is the honest choice -- this adapter really is an
# OpenAI-compatible client -- and it is also a strict improvement on what that name
# otherwise does here: mem0's own OpenAILLM auto-redirects to OpenRouter when
# OPENROUTER_API_KEY is set, but without the reasoning toggle, without the shared pool,
# ignoring the configured key, and recording no usage (see the module docstring).
#
# Nothing else in this process builds an "openai" mem0 LLM -- `_MEM0_CONFIG` is the only
# mem0 config, and its embedder is separately supplied by `_install_shared_embedder()`.
PROVIDER_NAME = "openai"


def _build_class():
    """Defined lazily so importing this module never requires mem0 to be installed."""
    from mem0.llms.base import LLMBase

    class JarvisCompatLLM(LLMBase):
        """Mem0 LLM backed by the provider (`router.provider()`)."""

        def __init__(self, config=None):
            super().__init__(config)
            from backend.brain.router import provider as _provider

            self._provider = _provider()
            if not getattr(self.config, "model", None):
                self.config.model = self._provider.standard

        def _client(self):
            from backend.providers import gateway

            return gateway.get_sync_client(self._provider)

        def _extra_body(self) -> dict | None:
            """Reasoning OFF, plus the provider routing preference.

            This is the whole reason the adapter exists -- see the module docstring.
            """
            from backend.brain.router import with_provider_prefs

            return with_provider_prefs(self._provider.no_thinking_body, self._provider)

        def _record_usage(self, response) -> None:
            """Extraction spend belongs in /cost. The roadmap's A09 acceptance names this
            explicitly ("extraction appears in cost"); it was invisible before."""
            try:
                from backend.brain import usage

                u = getattr(response, "usage", None)
                if u is None:
                    return
                details = getattr(u, "prompt_tokens_details", None)
                cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
                total_in = getattr(u, "prompt_tokens", 0) or 0
                usage.record(
                    self.config.model,
                    max(total_in - cached, 0),
                    getattr(u, "completion_tokens", 0) or 0,
                    cache_read=cached,
                    cost=getattr(u, "cost", None),   # OpenRouter's authoritative value
                )
            except Exception as exc:                  # never break extraction over metering
                log.debug("mem0 usage record failed: %s", exc)

        def generate_response(
            self,
            messages: list[dict],
            response_format: Any = None,
            tools: list[dict] | None = None,
            tool_choice: str = "auto",
            **kwargs,
        ):
            params: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "extra_body": self._extra_body(),
            }
            temperature = getattr(self._provider, "no_thinking_temperature", None)
            if temperature is not None:
                # Same descriptor field the planner call sites read, rather than a
                # second literal.
                params["temperature"] = temperature
            max_tokens = getattr(self.config, "max_tokens", None)
            if max_tokens:
                params["max_tokens"] = max_tokens
            if response_format:
                params["response_format"] = response_format
            if tools:
                params["tools"] = tools
                params["tool_choice"] = tool_choice

            started = time.time()
            response = self._client().chat.completions.create(**params)
            self._record_usage(response)
            from backend.storage import llm_calls as _llm_calls
            _llm_calls.record_response(
                response, model=self.config.model, started_at=started,
                purpose="mem0.extraction", extra_body=params["extra_body"],
                max_tokens=params.get("max_tokens"),
            )

            message = response.choices[0].message
            if tools:
                calls = []
                for call in (getattr(message, "tool_calls", None) or []):
                    import json

                    from mem0.memory.utils import extract_json

                    calls.append({
                        "name": call.function.name,
                        "arguments": json.loads(extract_json(call.function.arguments)),
                    })
                return {"content": message.content, "tool_calls": calls}

            content = message.content or ""
            if not content:
                # Deliberately NOT salvaged from reasoning_content. On the live chat path
                # that salvage existed for a Moonshot quirk (removed with Kimi), and it shipped one
                # bug (2026-09-05) by treating leaked reasoning as an answer. Extraction
                # feeds long-term memory: a model's private deliberation must never be
                # stored as a user fact. Empty means empty; mem0 handles no-facts fine.
                log.warning(
                    "mem0 extraction returned empty content (model=%s, finish=%s)",
                    self.config.model, getattr(response.choices[0], "finish_reason", "?"),
                )
            return content

    return JarvisCompatLLM


def is_registered() -> bool:
    """True when mem0's factory currently resolves PROVIDER_NAME to this adapter."""
    try:
        from mem0.utils.factory import LlmFactory

        entry = LlmFactory.provider_to_class.get(PROVIDER_NAME)
        return bool(entry) and entry[0].startswith(__name__)
    except Exception:
        return False


def register() -> bool:
    """Point mem0's factory at this adapter. Idempotent; never raises.

    Returns True when a mem0 config using PROVIDER_NAME will build this class.
    """
    try:
        from mem0.configs.llms.base import BaseLlmConfig
        from mem0.utils.factory import LlmFactory

        globals()["JarvisCompatLLM"] = _build_class()
        LlmFactory.provider_to_class[PROVIDER_NAME] = (
            f"{__name__}.JarvisCompatLLM", BaseLlmConfig,
        )
        return is_registered()
    except Exception as exc:
        log.warning("mem0 compat adapter registration failed: %s", exc)
        return False
