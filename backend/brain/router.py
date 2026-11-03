import os
import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# This module intentionally never imports intent.py or calls classify()
# itself. is_complex_result() below takes an optional precomputed
# IntentResult -- the embed happens once per turn in engine.py's chat() and
# is threaded through, because embedding here would break the stated
# contract (synchronous, zero API calls, sub-5ms) that main.py/engine.py
# depend on for the plain-regex path to still work instantly on a cold
# embedder.

# The model provider: OpenRouter, an OpenAI-compatible endpoint, serving DeepSeek V4.1 Flash
# on both tiers (reasoning off on the standard tier, on at `effort` on the thinking tier).
# Every setting is env-overridable. `{"enabled": false}` turns reasoning off: it is sent on
# the standard tier and on every strict-JSON call, because DeepSeek reasons by default and
# that reasoning shares max_tokens with the answer and can starve it. `{"effort": ...}`
# turns it on for the live thinking tier.
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_STANDARD = os.getenv("OPENROUTER_STANDARD_MODEL", "deepseek/deepseek-v4.1-flash")
OPENROUTER_THINKING = os.getenv("OPENROUTER_THINKING_MODEL", "deepseek/deepseek-v4.1-flash")
OPENROUTER_REASONING_EFFORT = os.getenv("OPENROUTER_REASONING_EFFORT", "high")
OPENROUTER_NO_THINKING_BODY = {"reasoning": {"enabled": False}}
OPENROUTER_THINKING_BODY = {"reasoning": {"effort": OPENROUTER_REASONING_EFFORT}}

# Provider routing preference (2026-09-21). OpenRouter routes price-first across the 22
# hosts serving v4.1-flash, and the cheapest one is catastrophically slow: measured live
# on an identical reasoning.effort=high request, Morph served 7.2 tok/s and an unpinned
# (auto-routed) call landed on Morph at 3.8 tok/s, against Together 92.3, BaseTen 81.6 and
# Novita 82.4 -- a 12-25x throughput spread for the same model. Driven through the real
# _chat_compat, pinning cut the benchmark's median turn from 43.4s to 11.1s (3.9x) with no
# other change. This ALSO fixes prefix-cache hit rate: each host keeps its own cache, so
# price-weighted hopping between hosts means a warm cache is rarely the one you land on.
#
# `order` + allow_fallbacks=True (never `only`): a preference, not a pin. If Together is
# down the request still completes on the next host rather than failing outright -- during
# the same measurement GMICloud returned a 520, so host failure is a real event, not a
# hypothetical. Set OPENROUTER_PROVIDER_ORDER="" to disable and go back to price-first
# routing, or to a different comma-separated list to re-order after re-measuring.
OPENROUTER_PROVIDER_ORDER = [
    p.strip() for p in os.getenv(
        "OPENROUTER_PROVIDER_ORDER", "Together,BaseTen,Novita"
    ).split(",") if p.strip()
]
OPENROUTER_PROVIDER_PREFS: dict | None = (
    {"order": OPENROUTER_PROVIDER_ORDER, "allow_fallbacks": True}
    if OPENROUTER_PROVIDER_ORDER else None
)

@dataclass(frozen=True)
class Provider:
    """The OpenAI-compatible chat-completions provider (OpenRouter): its endpoint, the
    model of each tier and the request-body pieces that turn reasoning off and on."""
    name: str
    base_url: str
    api_key_env: str             # env var name -- read live via the api_key property, so
                                  # tests can monkeypatch os.environ after import and a
                                  # corrected key works without a restart
    standard: str
    thinking: str
    no_thinking_body: dict       # extra_body for the standard tier / strict-JSON calls
    thinking_body: dict          # extra_body for the live thinking tier
    reasoning_echo_key: str      # assistant-message key used to echo reasoning back on a
                                  # tool-call turn (see the OPENROUTER descriptor's comment)
    reasoning_echo_required: bool = False   # echo the key even when reasoning came back
                                  # empty ("" rather than omitting it) on a thinking-tier
                                  # turn -- DeepSeek 400s on a missing field, not just a wrong one
    default_headers: dict | None = None
    # Temperature sent with no_thinking_body on the standard tier (_plan_task/_replan).
    # Live-verified accepted by DeepSeek on OpenRouter.
    no_thinking_temperature: float = 0.6
    # Temperature sent on the live thinking tier. DeepSeek accepts any value (verified
    # live: 0.0, 0.6, 1.0, 2.0 all `finish_reason: "stop"`); 1.0 is what was measured.
    thinking_temperature: float | None = 1.0
    # Routing preference merged into every outgoing extra_body by with_provider_prefs().
    provider_prefs: dict | None = None

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


OPENROUTER = Provider(
    name="openrouter",
    base_url=OPENROUTER_BASE_URL,
    api_key_env="OPENROUTER_API_KEY",
    standard=OPENROUTER_STANDARD,
    thinking=OPENROUTER_THINKING,
    no_thinking_body=OPENROUTER_NO_THINKING_BODY,
    thinking_body=OPENROUTER_THINKING_BODY,
    # DeepSeek V4's OWN API contract, not a generic OpenRouter convention -- OpenRouter's
    # docs describe a provider-agnostic "reasoning" field, but DeepSeek V4 rejects a
    # thinking-mode tool-call turn with HTTP 400 ("The reasoning_content in the thinking mode
    # must be passed back to the API.") unless the echo uses reasoning_content specifically,
    # confirmed via DeepSeek's own error text reproduced identically across multiple
    # independent integrations (LiteLLM #26395, opencode #24190/#24114, n8n #29119,
    # spring-ai #5027) hitting both the direct DeepSeek API and OpenRouter.
    reasoning_echo_key="reasoning_content",
    # One report (n8n #29119) traces the 400 to the field being flat-out absent/undefined on
    # a tool-call turn, not merely present-but-wrong -- so an empty string must still be sent
    # rather than omitting the key when a thinking-tier turn happens to stream no reasoning text.
    reasoning_echo_required=True,
    default_headers={"X-Title": "JARVIS"},
    provider_prefs=OPENROUTER_PROVIDER_PREFS,
)

# Jev (TypeSafe's classifier) decides the per-turn signals -- see brain/turn_signals.py.
# Pinned to a version, not the rolling ~typesafe/jev-latest alias: the thresholds were
# measured against jev-1.13, and a silently upgraded model would move every calibrated
# probability underneath them. It is served through OpenRouter's separate decisions
# endpoint with the same OpenRouter key.
JEV_MODEL = os.getenv("JARVIS_JEV_MODEL", "typesafe/jev-1.13")
JEV_URL = os.getenv("JARVIS_JEV_URL",
                    OPENROUTER_BASE_URL.rsplit("/v1", 1)[0] + "/alpha/decisions")


def jev_enabled() -> bool:
    """On unless JARVIS_JEV=0, and only when an OpenRouter key exists to pay for it."""
    return os.getenv("JARVIS_JEV", "1") != "0" and OPENROUTER.configured


def provider() -> Provider:
    """The model provider for every LLM call: live chat, the planner, background work,
    research, consolidation and memory extraction."""
    return OPENROUTER


_COMPLEX_KEYWORDS = frozenset([
    # Code / engineering tasks
    "debug", "write", "fix", "implement", "function", "script", "error",
    "stack trace", "traceback", "exception", "plan", "architect", "compare",
    "analyze", "analyse", "design", "refactor", "optimize", "build", "create",
    "explain", "how does", "why does", "difference between",
    # Introspection / memory queries -- the thinking tier follows instructions more reliably
    "do you remember", "what do you remember", "your memory", "your letter",
    "your last letter", "self letter", "your session", "your history",
    "how long did you sleep", "how long were you", "when did you wake",
    "what do you know about me", "your last session", "previous session",
    "what was in your", "tell me what you got", "tell me what was",
])

# Word-boundary matching: bare `in` substring checks misfire constantly
# ("plan" in "planet", "error" in "terror", "fix" in "prefix"). One compiled
# alternation keeps the check sub-ms.
_COMPLEX_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in sorted(_COMPLEX_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def is_complex(prompt: str) -> bool:
    """Regex-only complexity check: length + keyword floor, sub-5ms, no embed.

    This is the DEGRADATION FLOOR, not the primary signal anymore — see
    is_complex_result below, which is what engine.py actually calls. Kept
    as its own function (rather than folded into is_complex_result with a
    default) because it's also the standalone fallback used the rare turn
    the intent matrix hasn't warmed yet (embedder cold-starting) or the
    embedder is unavailable entirely (torch/sentence-transformers missing).
    """
    if len(prompt) > 300:
        return True
    return _COMPLEX_RE.search(prompt) is not None


def is_complex_result(prompt: str, result=None) -> bool:
    """True when a request warrants the stronger model AND deep context assembly.

    `result` is an optional intent.IntentResult, computed once per turn by
    engine.py's chat() and threaded through — this function does not embed
    anything itself. Semantic and keyword signals are OR'd, deliberately:
    the semantic route (intent.ROUTES["complex"]) catches paraphrases the
    keyword list was never going to enumerate ("what would break if you
    changed how you write to your vault" shares no keyword with any entry
    in _COMPLEX_KEYWORDS), while the keyword list stays a hard floor for
    complexity signals that must never depend on an embedder being warm.

    Also used by engine.py to gate heavy per-turn context (memory recall,
    vault letter, topic match): simple requests — greetings, commands,
    factual Q&A — skip it for a faster response.
    """
    if is_complex(prompt):
        return True
    if result is not None and result.is_route("complex"):
        return True
    return False


def with_provider_prefs(body: dict | None, provider: "Provider") -> dict | None:
    """Merge `provider.provider_prefs` into an outgoing extra_body.

    ALWAYS returns a new dict -- never mutates `body`. That matters because the
    bodies passed in here are shared module-level constants
    (OPENROUTER_THINKING_BODY / OPENROUTER_NO_THINKING_BODY / escalation.MAX_EFFORT_BODY); mutating one in place would leak the routing
    preference into every other call site importing the same object, silently, for
    the rest of the process -- the exact trap CLAUDE.md's escalation guardrail
    already calls out for MAX_EFFORT_BODY.

    Returns `body` unchanged (including None) when the provider has no preference
    (OPENROUTER_PROVIDER_ORDER set to an empty string).
    """
    if not provider.provider_prefs:
        return body
    merged = dict(body or {})
    merged["provider"] = provider.provider_prefs
    return merged


def route_tier(prompt: str, result=None, complex_: bool | None = None) -> tuple[str, bool]:
    """(model, is_thinking_tier) -- the ONE place the tier is decided.

    Both tiers are the same model at different reasoning efforts, so the tier cannot be
    recovered from the model string; it is returned alongside it. Verified live when the
    tiers became one model (2026-09-21): deriving it from `model == provider.thinking`
    made a plain "hey what's up" send `reasoning: {effort: high}`.

    `complex_`, when given, is a decision already made elsewhere -- Jev's turn signal
    (brain/turn_signals.py). The model and tier are still derived here, from it.
    """
    if complex_ is None:
        complex_ = is_complex_result(prompt, result)
    p = provider()
    return (p.thinking if complex_ else p.standard), complex_


def route(prompt: str, result=None) -> str:
    """The model alone. Callers that also need the tier use route_tier()."""
    return route_tier(prompt, result)[0]


def log_startup_mode() -> None:
    """Log the models and endpoint. Called from main.py after logging is configured."""
    log.info("Models: simple→%s | complex→%s | endpoint: %s",
             OPENROUTER_STANDARD, OPENROUTER_THINKING, OPENROUTER_BASE_URL)
