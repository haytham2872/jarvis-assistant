"""A03: read-your-writes for vault notes.

The roadmap's requirement: *"Use repository read-your-writes behavior: after saving a
note, the run can read the saved version immediately even if Qdrant indexing is
pending."*

The concrete gap. `vault_write()` writes the file and then fires
`semindex.index_file()` **fire-and-forget** (`_index_note_async`). Embedding plus the
Qdrant upsert is measured at ~2.8s, so for several seconds afterwards the note exists on
disk but is invisible to `memory_search`. Within a single turn that is a real failure:
JARVIS writes a note, searches for it two tool calls later, finds nothing, and can
reasonably conclude the write did not happen — the same class of confusion the
anti-confabulation guard exists to prevent, arriving from the opposite direction.

Direct reads were never affected: `vault_read()` opens the file. Only the *semantic*
path has the lag.

**A per-turn registry, not a cache.** Entries live in a `ContextVar` for the duration of
the turn that wrote them; nothing persists, nothing needs invalidating, and a stale entry
is impossible because the next turn starts empty. Once indexing catches up the real hit
supersedes it, and `memory_search` de-duplicates by `(folder, filename)` exactly as it
already does for its own chunks.
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_PREVIEW_CHARS = 1200        # one chunk's worth, matching semindex._chunk
_MAX_ENTRIES = 12            # a turn that writes more than this has other problems


@dataclass
class PendingWrite:
    folder: str
    filename: str
    content: str
    written_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str]:
        return (self.folder, self.filename)


_pending: ContextVar[list | None] = ContextVar("pending_writes", default=None)


def reset() -> None:
    _pending.set([])


def record(folder: str, filename: str, content: str) -> None:
    """Note that this turn wrote a file whose index entry may not exist yet."""
    try:
        entries = _pending.get()
        if entries is None:
            entries = []
            _pending.set(entries)
        entries[:] = [e for e in entries if e.key != (folder, filename)]
        entries.append(PendingWrite(folder, filename, content[:_PREVIEW_CHARS]))
        del entries[:-_MAX_ENTRIES]
    except Exception as exc:
        log.debug("recent_writes: could not record %s/%s (%s)", folder, filename, exc)


def pending() -> list:
    return list(_pending.get() or [])


def matching_hits(query: str, *, exclude: set | None = None, limit: int = 3) -> list[dict]:
    """Notes written this turn that the semantic index has probably not caught yet.

    Matching is deliberately lexical — token overlap against the filename and content.
    These notes are *unindexed by definition*, so there is no vector to compare, and
    inventing a similarity score for them would put them on a scale they do not share
    with real hits. They are scored just below the floor a real hit must clear, so a
    genuine semantic match always outranks a fallback.
    """
    entries = pending()
    if not entries:
        return []

    tokens = {t for t in _tokens(query) if len(t) > 3}
    if not tokens:
        return []

    exclude = exclude or set()
    scored = []
    for entry in entries:
        if entry.key in exclude:
            continue                    # the index already returned it; prefer the real hit
        haystack = _tokens(entry.filename) | _tokens(entry.content)
        overlap = len(tokens & haystack)
        if overlap:
            scored.append((overlap, entry))

    scored.sort(key=lambda pair: -pair[0])
    return [
        {
            "folder": entry.folder,
            "filename": entry.filename,
            "text": entry.content,
            "score": 0.0,               # explicitly not a similarity: see the docstring
            "_kind": "vault",
            "_pending_index": True,     # so the caller can label it honestly
        }
        for _, entry in scored[:limit]
    ]


def _tokens(text: str) -> set:
    out = set()
    word = []
    for ch in (text or "").lower():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    return out
