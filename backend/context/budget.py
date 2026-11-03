"""A03: one whole-request budget, instead of several independent character caps.

Today the backend has a scattering of unrelated truncation points — `_MEMORY_BUDGET_CHARS`
(~2800), `_SECTION_CHAR_CAP` (5500), `_EXISTING_NOTE_CAP` (20000), `_MAX_CHARS_BROWSE`
(24000) — each sized in isolation. None of them knows what the others are spending, and
none accounts for the tool definitions, the message history or the answer the model still
has to produce. That is the roadmap's "several independent character caps stand in for one
request policy".

This module reserves space for the *complete* request and then fills what remains in
priority order, recording what it dropped and why.

**Token estimates are labelled as estimates.** The roadmap asks for exact limits where
available and honest labelling where not. There is no local tokenizer for DeepSeek here,
so this uses a measured chars-per-token ratio rather than a guessed one:
`scripts/measure_note_token_ratio.py` ran Anthropic's real token-counting API over every
vault note and found a worst case of **3.82** chars/token (average 4.45) — markdown
headers, bold, wiki-links and em-dashes all tokenize denser than plain prose. The worst
case is used deliberately: overestimating tokens spends the budget conservatively, which
fails toward "dropped an optional source" rather than "provider rejected the request".
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.context.sources import Fragment, Omission, Snapshot

# Measured, not guessed -- see the module docstring.
CHARS_PER_TOKEN = 3.82

# Reservations, subtracted from the model's context window before any source competes.
# These are the parts of a request that are not optional and not selectable.
@dataclass(frozen=True)
class Reservation:
    # Serialized tool schemas. Measured live 2026-09-28 (H1): the 27 registered tools add
    # 9,022 prompt tokens to an identical request, 4.06 chars/token of schema JSON. This
    # was 1,500 (22 tools, "~1.4k"), so every foreground budget overstated the room for
    # context by ~7,500 tokens. test_budget_reserves_the_real_tool_schemas guards drift.
    tools: int = 14800         # +1600 portfolio (32 tools), +400 the skill tool, +700 goal (33,
                               # measured 14,212 on 2026-10-05), +300 G3 steer and the goal
                               # pointers (measured 14,509 on 2026-10-06); the lab's three
                               # tools (+1000) left with the lab on 2026-10-24; verify
                               # actual payload
    # Conversation history the caller will attach. 8000 is only the fallback for a
    # caller that does not measure: measured over the real exchange history, the live
    # 40-turn window runs p50 8,878 / p95 12,985 / max 16,146 tokens, so a fixed 8000
    # sat below the MEDIAN. Foreground turns pass the real figure (`for_history`).
    messages: int = 8000
    answer: int = 4096         # room for the reply itself, matching _MAX_TOKENS
    # Reasoning is output, and output is bounded per call by engine._OUTPUT_LIMITS (up to
    # 128K) against the provider's 1,048,576-token window, not by this 32K working
    # ceiling on what we assemble. Reserving 64K of reasoning here would leave no room
    # for any source. test_output_limits_fit_the_provider_window checks the real bound.
    reasoning: int = 0
    margin: int = 1000         # estimation error + provider-side framing

    @property
    def total(self) -> int:
        return self.tools + self.messages + self.answer + self.reasoning + self.margin


DEFAULT_RESERVATION = Reservation()

# DeepSeek V4.1 Flash advertises 1,048,576. Using it in full would be a mistake: cost and
# latency scale with what is actually sent, and the roadmap warns that a model output-token
# cap "is not a full system budget". This is a deliberate working ceiling for assembled
# context, not the provider's hard limit.
#
# 32,000 until H1 (2026-09-28), chosen while the tool schemas were believed to cost 1,500
# tokens. They cost 9,022. Correcting only the reservation would have cut real context
# from normal long sessions (measured: a p95-length history left 4,440 tokens, less than
# today's ~6,400-token prompt), so the ceiling moved by the same 7,600: the room left for
# context is unchanged, and the numbers are now true. Each later tool addition moved
# both by the same amount (vault_send +300, TinyFish's web_research and filters +300,
# schedule_task +300, the calendar listing note +300, the activity tool's wording +300, week_plan and calendar edit/delete +800, the report action +400, the skill tool +400,
# G3's goal steer and the goal pointers +300; the lab's tools, +1000, and -1000 when it left).
DEFAULT_CONTEXT_WINDOW = 45000

# The model's real context window (DeepSeek V4.1 Flash), input and output together.
PROVIDER_CONTEXT_WINDOW = 1_048_576


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for a string. Always labelled as an estimate."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


# Per-message framing a provider adds around each chat message (role, separators).
_MESSAGE_OVERHEAD_TOKENS = 4


def history_tokens(messages: list[dict] | None) -> int:
    """Estimated tokens of a chat history as it will actually be sent."""
    total = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        text = content if isinstance(content, str) else str(content or "")
        total += estimate_tokens(text) + _MESSAGE_OVERHEAD_TOKENS
    return total


def for_history(messages: list[dict] | None, user_text: str = "",
                base: "Reservation | None" = None) -> "Reservation":
    """A reservation whose `messages` figure is the real history about to be sent,
    plus the new user turn, instead of a guess."""
    from dataclasses import replace
    base = base or DEFAULT_RESERVATION
    return replace(base, messages=history_tokens(messages) + estimate_tokens(user_text)
                   + _MESSAGE_OVERHEAD_TOKENS)


@dataclass
class BudgetResult:
    selected: list[Fragment]
    omissions: list[Omission]
    tokens_used: int
    tokens_available: int

    @property
    def overflowed(self) -> bool:
        """True when mandatory content alone exceeded the budget."""
        return any(o.reason == "mandatory-overflow" for o in self.omissions)


def select(
    candidates: list[Fragment],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reservation: Reservation = DEFAULT_RESERVATION,
) -> BudgetResult:
    """Fill the available budget in precedence order.

    Ordering is by source class (see `sources`), stable within a class so a caller's own
    ordering is preserved. Non-droppable fragments are taken first regardless of budget:
    if they alone overflow, that is reported explicitly rather than silently trimming a
    constraint the turn depends on — the roadmap requires "If mandatory content alone
    exceeds the budget, split the task or request a narrower input rather than silently
    dropping constraints."

    A fragment that does not fit is skipped, not truncated, and the next one still gets a
    chance: dropping one large optional note should not also cost three small ones.
    """
    available = max(0, context_window - reservation.total)
    selected: list[Fragment] = []
    omissions: list[Omission] = []
    used = 0

    mandatory = [f for f in candidates if not f.droppable]
    optional = [f for f in candidates if f.droppable]

    for fragment in mandatory:
        cost = estimate_tokens(fragment.text)
        used += cost
        selected.append(fragment)
    if used > available:
        omissions.append(Omission(
            source_id="|".join(f.source_id for f in mandatory) or "(none)",
            reason="mandatory-overflow",
            chars=sum(f.chars for f in mandatory),
        ))

    for fragment in sorted(optional, key=lambda f: f.cls):
        cost = estimate_tokens(fragment.text)
        if used + cost > available:
            omissions.append(Omission(fragment.source_id, "budget-exhausted", fragment.chars))
            continue
        selected.append(fragment)
        used += cost

    # Restore the caller's original ordering for the selected set: precedence decides
    # what survives, not what order it is rendered in. Rendering order belongs to the
    # prompt author, and changing it would alter the cached stable prefix.
    order = {f.source_id: i for i, f in enumerate(candidates)}
    selected.sort(key=lambda f: order.get(f.source_id, 0))

    return BudgetResult(selected=selected, omissions=omissions,
                        tokens_used=used, tokens_available=available)


def build_snapshot(
    profile: str,
    candidates: list[Fragment | None],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reservation: Reservation = DEFAULT_RESERVATION,
) -> Snapshot:
    """Budget a candidate list into a Snapshot. `None` entries are ignored."""
    real = [c for c in candidates if c is not None]
    result = select(real, context_window=context_window, reservation=reservation)
    return Snapshot(
        profile=profile,
        fragments=result.selected,
        omissions=result.omissions,
        token_estimate=result.tokens_used,
    )
