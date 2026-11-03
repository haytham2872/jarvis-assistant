"""backend/brain/usage.py — token/cost accounting for the running session.

In-memory only, resets on restart -- this tracks the CURRENT session's spend,
not a persistent ledger (that would be a different, bigger feature). Every
real LLM call site in the codebase (the main chat pipeline, the
plan/replan calls, background tasks, the research loop, and consolidation)
calls record() right after it gets a response, so session_summary() reflects
total spend across all of JARVIS's activity, not just live conversation.

Pricing table: DeepSeek (OpenRouter) rates
are OpenRouter's own published list prices, verified live against
/api/v1/models on 2026-09-21. Two of them carry TIME-OF-DAY overrides that a flat
table cannot express at all: v4.1-flash and v4-pro-0813 are half price outside
weekday 01:00-04:00 and 06:00-10:00 UTC -- and since that off-peak window covers
~79% of the week (35 peak hours out of 168), the peak rate is the EXCEPTION, not
the norm. The rows below are therefore all recorded on one consistent basis: the
PEAK rate, the conservative direction, so the fallback never underestimates.
  deepseek-v4.1-flash      $0.30 / $1.20  peak   ($0.15 / $0.60 off-peak)
  deepseek-v4-pro-0813     $1.32 / $3.96  peak   ($0.66 / $1.98 off-peak)
  deepseek-v4-flash-0731   $0.04 / $0.16         (flat -- no overrides)
  deepseek-v4-flash-vision-exp $0.22 / $0.66     (flat -- no overrides)
Week-weighted effective rates, if you need to reason about real spend rather than
a worst case: v4.1-flash ~$0.181/$0.725, v4-pro-0813 ~$0.798/$2.393 per Mtok.
(The v4-pro row previously carried its OFF-peak rate while v4.1-flash carried its
peak rate -- two different bases side by side in one table, corrected here.)

The cache-hit split comes from `usage.prompt_tokens_details.cached_tokens`
(OpenAI's convention). Callers that can't read a cache breakdown pass
cache_read=0, and record() bills 100% of input at the (higher) cache-miss
rate in that case. This never underestimates cost, only possibly overstates
it slightly if caching is happening but unreported.

OpenRouter's usage payload additionally reports an authoritative `usage.cost`
per call (already accounting for whatever provider actually served the
request, which can differ from the list price above) -- record()'s optional
`cost` parameter feeds that straight through, taking over from the pricing
table entirely for that model once any call has reported one.
"""

import logging
from collections import defaultdict

log = logging.getLogger(__name__)

# $ per million tokens: (input_or_cache_miss, output, cache_write, cache_read)
# cache_write is the full-price input rate (DeepSeek does not bill cache writes
# separately).
_PRICING: dict[str, dict[str, float]] = {
    # DeepSeek via OpenRouter -- list prices, used only as a fallback for a model whose
    # calls never reported an authoritative usage.cost (see record()'s `cost` param).
    "deepseek/deepseek-v4.1-flash":    {"input": 0.30, "output": 1.20, "cache_write": 0.30, "cache_read": 0.006},
    "deepseek/deepseek-v4-flash-0731": {"input": 0.04, "output": 0.16, "cache_write": 0.04, "cache_read": 0.016},
    "deepseek/deepseek-v4-pro-0813":   {"input": 1.32, "output": 3.96, "cache_write": 1.32, "cache_read": 0.044},
    "deepseek/deepseek-v4-flash-vision-exp": {"input": 0.22, "output": 0.66, "cache_write": 0.22, "cache_read": 0.007},
}

_MTOK = 1_000_000.0


def _empty_counters() -> dict[str, float]:
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0,
        "cost": 0.0, "cost_authoritative": False,
        # A09: tokens from calls that did NOT report an authoritative cost, kept
        # separately so they can still be priced from the table. The totals above stay
        # whole-model figures for display.
        "est_input": 0, "est_output": 0, "est_cache_read": 0, "est_cache_write": 0,
        "est_calls": 0,
    }


_usage: dict[str, dict[str, float]] = defaultdict(_empty_counters)


def record(model: str, input_tokens: int, output_tokens: int,
           cache_read: int = 0, cache_write: int = 0, cost: float | None = None) -> None:
    """Accumulate one API call's token usage under `model`. Never raises --
    a malformed/missing usage field from a provider should never break the
    turn that triggered it; this is bookkeeping, not a correctness path.

    `cost` is an optional authoritative per-call $ figure (OpenRouter's
    usage.cost) -- once any call for a model supplies it, that model's
    reported cost total takes over from the pricing-table estimate entirely
    (see _cost_for_model).
    """
    try:
        counters = _usage[model]
        counters["input"]       += max(0, int(input_tokens))
        counters["output"]      += max(0, int(output_tokens))
        counters["cache_read"]  += max(0, int(cache_read))
        counters["cache_write"] += max(0, int(cache_write))
        counters["calls"]       += 1
        if cost is not None:
            counters["cost"] += max(0.0, float(cost))
            counters["cost_authoritative"] = True
        else:
            # A09: this call has no authoritative figure, so its tokens must still be
            # priced from the table. They used to be dropped the moment ANY call for
            # the same model reported a cost -- see _cost_for_model.
            counters["est_input"]       += max(0, int(input_tokens))
            counters["est_output"]      += max(0, int(output_tokens))
            counters["est_cache_read"]  += max(0, int(cache_read))
            counters["est_cache_write"] += max(0, int(cache_write))
            counters["est_calls"]       += 1
    except Exception as e:
        log.debug("usage: record failed for model %r: %s", model, e)


def _estimated_cost(model: str, counters: dict[str, float]) -> float | None:
    """Table-priced cost of this model's calls that reported no authoritative figure.

    None means *unknown* rather than zero: tokens were spent but the model has no
    pricing row, so the true number is unavailable. Collapsing that to 0.0 is what
    makes a cost total quietly wrong.
    """
    if not counters.get("est_calls"):
        return 0.0
    rates = _PRICING.get(model)
    if rates is None:
        return None
    return (
        counters["est_input"]       * rates["input"]       +
        counters["est_output"]      * rates["output"]      +
        counters["est_cache_read"]  * rates["cache_read"]  +
        counters["est_cache_write"] * rates["cache_write"]
    ) / _MTOK


def _cost_for_model(model: str, counters: dict[str, float]) -> float:
    """Authoritative costs PLUS table-priced estimates for the calls that had none.

    A09 acceptance, stated as a number: *"an estimated $1 call plus a separate
    authoritative $0.25 call totals $1.25."* It used to total $0.25 -- one reported
    cost flipped the whole model to authoritative-only and silently discarded every
    un-reported call's tokens. Reproduced before this fix.
    """
    estimated = _estimated_cost(model, counters)
    return float(counters.get("cost", 0.0)) + (estimated or 0.0)


def estimate_cost() -> float:
    """Total estimated $ across every model used this session. 0.0 for an
    untracked/unpriced model rather than raising -- an unknown model should
    degrade the number, not the feature."""
    return sum(_cost_for_model(model, counters) for model, counters in _usage.items())


def session_summary() -> str:
    """Human-readable per-model token breakdown + total estimated cost.
    Never empty -- reports 'no LLM calls yet' rather than an empty string."""
    if not _usage:
        return "No LLM calls recorded yet this session."
    lines = []
    for model, counters in sorted(_usage.items()):
        if counters["calls"] == 0:
            continue
        cost = _cost_for_model(model, counters)
        # A09: say WHICH kind of number this is. "measured" is the provider's own
        # per-call figure, "estimated" is priced from the table, and a model with
        # spent tokens but no pricing row is "partly unknown" rather than silently 0.
        estimated = _estimated_cost(model, counters)
        if estimated is None:
            basis = " (partly unknown — no pricing for this model)"
        elif counters.get("cost_authoritative") and counters.get("est_calls"):
            basis = " (measured + estimated)"
        elif counters.get("cost_authoritative"):
            basis = " (measured)"
        else:
            basis = " (estimated)"
        extra = ""
        if counters["cache_read"] or counters["cache_write"]:
            extra = f", cache {counters['cache_read']:,} read / {counters['cache_write']:,} write"
        lines.append(
            f"{model}: {counters['input']:,} in / {counters['output']:,} out "
            f"({counters['calls']} call{'s' if counters['calls'] != 1 else ''}{extra}) — ~${cost:.4f}{basis}"
        )
    total = estimate_cost()
    unknown = [m for m, c in _usage.items()
               if c["calls"] and _estimated_cost(m, c) is None]
    lines.append(f"Total estimated: ~${total:.4f}")
    if unknown:
        lines.append(
            f"(excludes unpriced calls for: {', '.join(sorted(unknown))} — the real "
            f"total is higher)")
    return "\n".join(lines)


def cost_report() -> str:
    """/cost: this session's spend by model, then the day's deepest turns (H1), read
    from the per-call record so a restart does not lose them."""
    lines = [session_summary()]
    try:
        from backend.storage import llm_calls
        lines += llm_calls.deepest_turns_lines()
    except Exception as e:
        log.debug("usage: deepest turns unavailable: %s", e)
    return "\n".join(lines)


def reset() -> None:
    """Clear all accumulated usage. Used by tests; not called in production
    (session cost is meant to persist for the process's whole lifetime)."""
    _usage.clear()
