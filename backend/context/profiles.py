"""A03: workload profiles — which sources each kind of call is entitled to.

"Unified context" does **not** mean giving every workload the whole conversation and the
entire personality prompt. Foreground `chat()` assembles rich context; background jobs
inherit a bounded brief with the originating request and relevant evidence.

A profile is a declaration, not a mechanism: it names the source ids a workload may
include and whether each is required. Selection and budgeting stay in `budget.py`, the
content stays with whoever owns it. Profiles exist so that "what does consolidation
actually see?" has one answer that can be read, tested, and diffed.

These mirror the table in section 5 of the roadmap. `foreground` names caller-supplied
sources and renderer-owned trigger blocks. The latter (runtime status, self-model,
evolution, positions, calibration) are rendered by `build_system_prompt` and do not
appear in budgeted snapshots; keep that distinction explicit when reading a manifest.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str
    required: tuple[str, ...] = ()      # absent -> the call is malformed
    optional: tuple[str, ...] = ()      # included when present and affordable
    excluded: tuple[str, ...] = ()      # never included, even if offered
    notes: str = ""

    def allows(self, source_id: str) -> bool:
        if source_id in self.excluded:
            return False
        return source_id in self.required or source_id in self.optional

    def filter(self, fragments):
        """Drop anything this profile does not allow. Order is preserved."""
        return [f for f in fragments if f is not None and self.allows(f.source_id)]

    def missing(self, fragments) -> list[str]:
        present = {f.source_id for f in fragments if f is not None}
        return [r for r in self.required if r not in present]


# Source ids, named once so a typo is a NameError rather than a silently ignored string.
CORE = "instruction:core"
USER_REQUEST = "instruction:user_request"
NOW = "live:now"
PLAN = "live:plan"
RUNTIME_IDENTITY = "live:runtime_identity"  # every turn, renderer-owned
RUNTIME_STATUS = "live:runtime_status"
HISTORY_BRIDGE = "continuity:history_bridge"
EXCHANGE = "continuity:exchange"
OWNER = "memory:owner"
QUEUE = "memory:queue"
VAULT_SUMMARY = "memory:vault_summary"   # the [Your vault] listing, built inside
                                          # build_system_prompt -- not caller-supplied
VAULT_PASSAGE = "memory:vault_passage"   # best semantic vault excerpt from recall()
FACTS = "memory:facts"
LETTER = "memory:letter"
SLEEP = "memory:sleep"
MOOD = "memory:mood"
TOPIC = "memory:topic"
SKILL_CATALOG = "memory:skill_catalog"   # the skills list, built inside
                                          # build_system_prompt like VAULT_SUMMARY
POSITIONS = "memory:positions"
PREDICTIONS = "memory:predictions"
SELF_MODEL = "memory:self_model"
EVOLUTION = "memory:evolution"       # trigger-gated update log, rendered inside
                                      # build_system_prompt like SELF_MODEL
TASK_BRIEF = "instruction:task_brief"
SEARCH_EVIDENCE = "evidence:search"
PORTFOLIO = "evidence:portfolio"       # exact code-owned ledger, trigger-gated by caller

FOREGROUND = Profile(
    name="foreground",
    required=(CORE, USER_REQUEST),
    optional=(NOW, PLAN, RUNTIME_IDENTITY, RUNTIME_STATUS, HISTORY_BRIDGE, EXCHANGE, OWNER, QUEUE,
              VAULT_SUMMARY, VAULT_PASSAGE, FACTS, LETTER, SLEEP, MOOD, TOPIC, SKILL_CATALOG,
              POSITIONS, PREDICTIONS, SELF_MODEL, EVOLUTION, PORTFOLIO),
    notes="Names foreground sources; renderer-owned blocks are not in budget snapshots.",
)

PLANNING = Profile(
    name="planning",
    required=(USER_REQUEST,),
    optional=(CORE, NOW, PLAN, OWNER, QUEUE, EXCHANGE, TOPIC, SKILL_CATALOG),
    excluded=(SELF_MODEL, PREDICTIONS, POSITIONS, MOOD, SLEEP),
    notes=("Same goal and constraints as the executor, plus the latest plan. The "
           "roadmap's rule: never plan from bare task text when context already exists. "
           "Identity/mood layers are irrelevant to producing a step list."),
)

BACKGROUND = Profile(
    name="background",
    required=(TASK_BRIEF,),
    optional=(NOW, OWNER, QUEUE, TOPIC, SKILL_CATALOG, SEARCH_EVIDENCE),
    excluded=(EXCHANGE, HISTORY_BRIDGE, PREDICTIONS, MOOD, SELF_MODEL),
    notes=("Inherits an explicit brief rather than a bare task string, but no blanket "
           "copy of private conversation -- the roadmap forbids that specifically."),
)

RESEARCH = Profile(
    name="research",
    required=(USER_REQUEST,),
    optional=(NOW, TOPIC, SEARCH_EVIDENCE, QUEUE),
    excluded=(EXCHANGE, HISTORY_BRIDGE, FACTS, OWNER, PREDICTIONS, MOOD, SELF_MODEL),
    notes="Unrelated conversation history is explicitly excluded.",
)

CONSOLIDATION = Profile(
    name="consolidation",
    required=(USER_REQUEST,),
    optional=(OWNER, QUEUE, SLEEP, MOOD, POSITIONS, PREDICTIONS, FACTS),
    excluded=(SELF_MODEL, RUNTIME_STATUS, SEARCH_EVIDENCE),
    notes="Explicit session source range; every input/output linked to source ids.",
)

RUMINATION = Profile(
    name="rumination",
    required=(USER_REQUEST,),
    optional=(NOW, LETTER, OWNER, MOOD),
    excluded=(SEARCH_EVIDENCE, RUNTIME_STATUS, SELF_MODEL, PREDICTIONS),
    notes="Interpretation, kept separate from fact extraction.",
)

BRIEF = Profile(
    name="brief",
    required=(USER_REQUEST,),
    optional=(NOW, CORE, QUEUE),
    excluded=(EXCHANGE, FACTS, PREDICTIONS, SELF_MODEL, SEARCH_EVIDENCE),
    notes="Small identity/voice context; deterministic digest when the model is down.",
)

EXTRACTION = Profile(
    name="extraction",
    required=(USER_REQUEST,),
    optional=(),
    excluded=(CORE, EXCHANGE, HISTORY_BRIDGE, OWNER, MOOD, SELF_MODEL,
              SEARCH_EVIDENCE, PREDICTIONS, POSITIONS),
    notes=("Committed exchange sources only. No raw reasoning and no unrelated "
           "retrieved material -- a model's deliberation must never become a user fact."),
)

ALL: dict[str, Profile] = {
    p.name: p for p in (FOREGROUND, PLANNING, BACKGROUND, RESEARCH, CONSOLIDATION,
                        RUMINATION, BRIEF, EXTRACTION)
}


def get(name: str) -> Profile:
    try:
        return ALL[name]
    except KeyError:
        raise KeyError(f"unknown context profile {name!r}; known: {sorted(ALL)}") from None
