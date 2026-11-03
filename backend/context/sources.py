"""A03: the context source inventory — one typed record per thing that can enter a prompt.

Foreground chat now selects supplied context through the shared service before
`build_system_prompt` renders it. Background work inherits a bounded brief. Profiles
for research, rumination, the morning brief, and consolidation describe intended scope;
those workloads do not yet select through this service. This inventory
names candidate fragments, their priority and provenance so selection can explain
what was included or omitted.

The renderer also owns current time and runtime identity plus trigger-gated blocks
such as runtime status, self-model, and evolution. They have profile IDs but do not
appear as budgeted `Snapshot.fragments`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# Source classes, in the precedence order section 5 of the roadmap defines. Lower sorts
# first and is dropped last: instructions and live state outrank retrieved memory, which
# outranks optional enrichment.
INSTRUCTION = 10      # invariant identity, the user's request, accepted constraints
LIVE_STATE = 20       # session, active plan, verified tool results, current time
CONTINUITY = 30       # recent exchanges, unresolved commitments, history bridge
MEMORY = 40           # portrait, facts, letter, sleep, queue, mood, topics, skills
EVIDENCE = 50         # retrieved pages and integration responses (untrusted data)

CLASS_NAMES = {
    INSTRUCTION: "instruction",
    LIVE_STATE: "live_state",
    CONTINUITY: "continuity",
    MEMORY: "memory",
    EVIDENCE: "evidence",
}


@dataclass(frozen=True)
class Fragment:
    """One candidate piece of context.

    `source_id` is stable across turns (e.g. "vault:letter", "memory:facts") so a
    snapshot can be compared with the previous one and a cache can be keyed on it.
    `version` is whatever identifies this content's revision — a file hash, an mtime, a
    session id. It is opaque here; only equality matters.
    """
    source_id: str
    cls: int
    text: str
    version: str = ""
    trusted: bool = True          # False for fetched web/integration content
    droppable: bool = True        # False for content a turn is invalid without

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def empty(self) -> bool:
        return not self.text.strip()


@dataclass
class Omission:
    """Why something did not make it in. Recorded rather than silently dropped."""
    source_id: str
    reason: str
    chars: int


@dataclass
class Snapshot:
    """An immutable-by-convention view of what one model call was given.

    The roadmap asks for "source IDs/versions, selected fragments, trust/provenance,
    token estimate, omissions and reasons". Everything here is metadata *about* content
    plus the content itself; nothing derived is stored twice.
    """
    profile: str
    fragments: list[Fragment] = field(default_factory=list)
    omissions: list[Omission] = field(default_factory=list)
    token_estimate: int = 0
    built_at: float = field(default_factory=time.time)

    def text_for(self, source_id: str) -> str:
        for fragment in self.fragments:
            if fragment.source_id == source_id:
                return fragment.text
        return ""

    def source_ids(self) -> list[str]:
        return [f.source_id for f in self.fragments]

    def manifest(self) -> dict:
        """Provenance without the content — safe to log.

        The roadmap is explicit that ordinary observability needs metadata, not private
        text dumps: "Store raw prompt content only under an explicit, limited diagnostic
        policy". This is the metadata half.
        """
        return {
            "profile": self.profile,
            "built_at": self.built_at,
            "token_estimate": self.token_estimate,
            "sources": [
                {
                    "id": f.source_id,
                    "class": CLASS_NAMES.get(f.cls, str(f.cls)),
                    "version": f.version,
                    "chars": f.chars,
                    "trusted": f.trusted,
                }
                for f in self.fragments
            ],
            "omissions": [
                {"id": o.source_id, "reason": o.reason, "chars": o.chars}
                for o in self.omissions
            ],
        }


def fragment(source_id: str, cls: int, text: str, **kwargs) -> Fragment | None:
    """Build a Fragment, or None when there is nothing to include.

    Empty sources are a normal outcome (no letter yet, no relevant skill) and should not
    occupy a slot or appear in the manifest as if they had been considered.
    """
    if not text or not text.strip():
        return None
    return Fragment(source_id=source_id, cls=cls, text=text, **kwargs)
