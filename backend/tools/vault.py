"""JARVIS personal vault — an Obsidian-compatible note store at jarvis-vault/.

Folders:
  people/                    — portraits of people in JARVIS's world
  topics/                    — notes on recurring subjects
  self/                      — JARVIS's own evolving self-knowledge
  conversations/             — conversation-derived notes
  conversations/significant/ — significant moments flagged explicitly
  creative/                  — poems, stories, expressive fragments
  letters/                   — letters JARVIS writes to his future self
  drafts/                    — work in progress across any category
  shared/                    — files explicitly flagged for the owner

Files accumulate; there is no deletion tool by design.
"""
from backend import owner as _owner

import re
import asyncio
import shutil
import logging
from datetime import datetime, timedelta
from pathlib import Path
from backend.tasks import spawn

log = logging.getLogger(__name__)

# Project root is three levels up: backend/tools/vault.py → backend/tools/ → backend/ → root
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_VAULT = _PROJECT_ROOT / "jarvis-vault"

FOLDERS = (
    "people",
    "topics",
    "self",
    "conversations",
    "conversations/significant",
    "creative",
    "letters",
    "drafts",
    "shared",
    "attachments",     # images and documents the owner sent, each with a text note (attachments.py)
)

_MAX_READ_CHARS = 32_000  # ~8000 tokens

# A long, section-structured note (research topics/ notes especially — see
# CLAUDE.md's Autonomous Research section, whose synthesis prompt builds
# "## Perspectives & Counterpoints" / "## Connections" / "## Open questions"
# style sections) doesn't need to be read in full just to answer one
# question about it. Below these thresholds a full read is cheap enough
# that a table-of-contents round-trip isn't worth it -- these gate ONLY
# when a full read is skipped by default, never when one is impossible.
_TOC_MIN_CHARS    = 3000   # ~750 tokens -- below this, always return in full
_TOC_MIN_HEADINGS = 2      # need at least this many "## " sections for a TOC to help


def _ensure_vault() -> None:
    for folder in FOLDERS:
        (_VAULT / folder).mkdir(parents=True, exist_ok=True)


def _safe_filename(filename: str) -> str:
    """Strip path components — no traversal."""
    name = Path(filename).name
    if not name:
        raise ValueError("Filename cannot be empty")
    return name


# The three files at the vault root, readable (not writable) as folder "root". Live
# (2026-09-28): JARVIS could write queue.md with vault_write_queue but never read it back
# -- vault_read only reached the named folders -- so he could not verify his own queue
# or see the research trail and schedule he is asked about.
ROOT_FOLDER = "root"
ROOT_FILES = ("queue.md", "research-queue.md", "schedule.md")


def _root_file(filename: str) -> Path:
    name = _safe_filename(filename)
    if name not in ROOT_FILES:
        raise ValueError(f"Only {', '.join(ROOT_FILES)} can be read from the vault root.")
    return _queue_file() if name == "queue.md" else _VAULT / name


def _resolve(folder: str, filename: str, *, folders=FOLDERS) -> Path:
    """Resolve a flat name. Read-only clients may pass their own explicit folder allowlist.

    Writers keep FOLDERS; callers needing rejection rather than basename stripping must
    validate first and check real-path containment (mobile/notes.py does both).
    """
    folder = folder.strip()
    if folder not in folders:
        raise ValueError(f"Unknown folder '{folder}'. Valid folders: {', '.join(FOLDERS)}")
    return _VAULT / folder / _safe_filename(filename)


def _index_note_async(folder: str, filename: str, content: str) -> None:
    """Fire semindex.index_file without blocking the write. Silent on any failure.

    A03: the write is also recorded in the per-turn read-your-writes registry. Indexing
    takes ~2.8s (embed + upsert), so until it lands the note exists on disk but is
    invisible to memory_search -- and a turn that writes a note then searches for it two
    tool calls later would conclude the write never happened.
    """
    try:
        from backend.context import recent_writes
        recent_writes.record(folder, filename, content)
    except Exception as exc:
        log.debug("vault: read-your-writes record skipped: %s", exc)
    try:
        from backend.brain import semindex
        spawn(semindex.index_file(folder, filename, content))
    except Exception as e:
        log.debug("vault: semantic index dispatch skipped: %s", e)


def note_revision(folder: str, filename: str) -> str:
    """A revision marker for a vault note: sha256 of its bytes, or "" if absent.

    A05: content hash rather than mtime. mtime granularity on Windows is coarse enough
    that two writes inside the same tick can share a timestamp, and a hash also makes
    "changed then changed back" correctly compare equal — which is what the caller
    actually cares about (did my read go stale), not how many times it was touched.
    """
    import hashlib
    try:
        path = _resolve(folder, filename)
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


# A05: returned instead of writing when a note changed under a caller that read it
# earlier. Prefixed `Error:` so it classifies as a failure through tools/outcomes.py.
_STALE_WRITE_MSG = (
    "Error: {folder}/{filename} changed since you read it — not overwriting. "
    "Re-read it and reapply your change if it still makes sense."
)


async def vault_write(filename: str, content: str, folder: str,
                      expected_revision: str | None = None) -> str:
    """Create or overwrite a file in the specified vault folder. Reads the
    file back after writing and confirms it matches before reporting success
    -- a write that silently didn't land (disk error, permissions) must never
    be reported as done (2026-09-08 incident: a persistence tool's success
    string wasn't actually backed by a verified write)."""
    _ensure_vault()
    try:
        path = _resolve(folder, filename)
        # A05: optional revision precondition. A caller that read this note, spent
        # minutes in an LLM call, and is now writing a full rewrite derived from that
        # read would otherwise destroy anything written in between -- reproduced with
        # research.py's synthesis window, where a note written mid-tick vanished.
        # Callers that pass nothing are completely unaffected.
        if expected_revision is not None:
            current = note_revision(folder, path.name)
            if current != expected_revision:
                log.warning("Vault: refusing stale overwrite of %s/%s", folder, path.name)
                return _STALE_WRITE_MSG.format(folder=folder, filename=path.name)
        path.write_text(content, encoding="utf-8")
        if path.read_text(encoding="utf-8") != content:
            return (f"Error: write to {folder}/{path.name} did not verify on read-back — "
                     f"do not report this as saved.")
        log.info("Vault: wrote %s/%s (%d chars, verified)", folder, filename, len(content))
        # Make the note semantically searchable as it's written (fire-and-forget)
        _index_note_async(folder, path.name, content)
        return f"Confirmed written to jarvis-vault/{folder}/{path.name} ({len(content)} chars, verified by read-back)."
    except Exception as e:
        return f"Error writing to vault: {e}"


def _strip_frontmatter(content: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    try:
        close = next(i for i, l in enumerate(lines[1:], start=1) if l.strip() == "---")
        return "\n".join(lines[close + 1:])
    except StopIteration:
        return content


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split a note body into (heading, content) pairs on "## " headings
    (not "### " or deeper — those are sub-structure within a section, not a
    section boundary of their own). Content before the first heading (the
    lead/intro) is returned under heading "". Drops a wholly-empty leading
    entry when the note starts right at its first heading."""
    lines = body.splitlines()
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if heading or buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf).strip()))
    return [(h, c) for h, c in sections if h or c]


async def vault_read(filename: str, folder: str, section: str = "") -> str:
    """Read a file from the vault. Content capped at ~8000 tokens.

    For a long, section-structured note (2+ "## " headings and 3000+ chars),
    omitting `section` returns a table of contents instead of the full text
    — call again with `section` set to one of the listed headings to read
    just that part, far cheaper than pulling a whole multi-thousand-token
    note into context for one question about it. Pass section="*" to force
    the full text of a long note anyway. Short notes and notes without real
    "## " sections are unaffected and always return in full either way.
    """
    _ensure_vault()
    try:
        if folder.strip() == ROOT_FOLDER:
            path = _root_file(filename)
        else:
            path = _resolve(folder, filename)
        if not path.exists():
            return f"Not found: {folder}/{filename}"
        text = path.read_text(encoding="utf-8")
        section = (section or "").strip()

        if section and section != "*":
            sections = _split_sections(_strip_frontmatter(text))
            headings = [h for h, _ in sections if h]
            matched = next((hc for hc in sections if hc[0].lower() == section.lower()), None)
            if matched is None:
                matched = next((hc for hc in sections if section.lower() in hc[0].lower()), None)
            if matched is None:
                available = ", ".join(headings) or "(this note has no \"## \" sections)"
                return (f"No section matching '{section}' in {folder}/{filename}. "
                        f"Available sections: {available}")
            heading, content = matched
            if len(content) > _MAX_READ_CHARS:
                content = content[:_MAX_READ_CHARS] + "\n[... truncated at ~8000 tokens]"
            return f"## {heading}\n\n{content}" if heading else content

        if section != "*":
            sections = _split_sections(_strip_frontmatter(text))
            headings = [h for h, _ in sections if h]
            if len(text) >= _TOC_MIN_CHARS and len(headings) >= _TOC_MIN_HEADINGS:
                lines = [
                    f"{filename} is {len(text)} chars (~{len(text) // 4} tokens) with "
                    f"{len(headings)} sections — reading it whole would cost real context "
                    f"budget for what may be one question. Sections:"
                ]
                for h, c in sections:
                    if h:
                        lines.append(f"  - \"{h}\" (~{len(c)} chars)")
                lines.append('Call again with section="<heading>" to read just one part, '
                             'or section="*" to read the full note anyway.')
                return "\n".join(lines)

        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + "\n[... truncated at ~8000 tokens]"
        return text
    except Exception as e:
        return f"Error reading from vault: {e}"


async def vault_list(folder: str | None = None) -> str:
    """List vault files grouped by top-level folder. Returns empty string if vault is empty."""
    _ensure_vault()
    try:
        top_level = [f for f in FOLDERS if "/" not in f]
        folders_to_scan: list[str] = [folder.strip()] if folder else top_level
        if folder and folder.strip() not in FOLDERS:
            return f"Unknown folder '{folder}'. Valid folders: {', '.join(FOLDERS)}"

        lines: list[str] = []
        any_files = False

        for fname in folders_to_scan:
            folder_path = _VAULT / fname
            try:
                files = [p for p in folder_path.iterdir() if p.is_file()]
            except Exception:
                files = []
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            lines.append(f"{fname}/")
            if not files:
                lines.append("  (empty)")
            else:
                any_files = True
                for fpath in files:
                    mtime = datetime.fromtimestamp(fpath.stat().st_mtime).strftime("%Y-%m-%d")
                    try:
                        first_line = fpath.read_text(encoding="utf-8").split("\n")[0].strip()
                        first_line = first_line[:80] + ("…" if len(first_line) > 80 else "")
                    except Exception:
                        first_line = ""
                    desc = f" — {first_line}" if first_line else ""
                    lines.append(f"  {fpath.name} ({mtime}){desc}")

        if not any_files:
            return ""

        return "\n".join(lines)

    except Exception as e:
        log.warning("vault_list failed: %s", e)
        return ""


_SUMMARY_RECENT = 3


def vault_summary() -> str:
    """A compact, cheap snapshot for the system prompt's stable block (Step 5,
    2026-09-09) -- each top-level folder, its file count, and its newest few
    filenames. NOT vault_list's output: no per-file dates, no first-line
    descriptions -- the point is knowing what's owned without a tool call,
    not reading the vault. Sync (cheap stat-only disk scan); degrades to ''
    on any error, same contract as every other vault read here.
    """
    _ensure_vault()
    try:
        top_level = [f for f in FOLDERS if "/" not in f]
        lines: list[str] = []
        for fname in top_level:
            folder_path = _VAULT / fname
            try:
                files = [p for p in folder_path.iterdir() if p.is_file()]
            except Exception:
                files = []
            if not files:
                lines.append(f"{fname}/ (empty)")
                continue
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            recent = ", ".join(p.name for p in files[:_SUMMARY_RECENT])
            more = len(files) - _SUMMARY_RECENT
            suffix = f", +{more} more" if more > 0 else ""
            lines.append(f"{fname}/ ({len(files)} files — newest: {recent}{suffix})")
        return "\n".join(lines)
    except Exception as e:
        log.warning("vault_summary failed: %s", e)
        return ""


async def vault_share(filename: str, source_folder: str) -> str:
    """Copy a file into shared/ so the owner can see it."""
    _ensure_vault()
    try:
        src = _resolve(source_folder, filename)
        if not src.exists():
            return f"Not found: {source_folder}/{filename}"
        dst = _VAULT / "shared" / src.name
        shutil.copy2(src, dst)
        log.info("Vault: shared %s/%s → shared/%s", source_folder, filename, src.name)
        return (
            _owner.say(f"Shared: {src.name} is now in shared/ for «owner» to see. "
            f"Tell «him» it's there if «he» hasn't noticed.")
        )
    except Exception as e:
        return f"Error sharing file: {e}"


# ---------------------------------------------------------------------------
# Intent queue — jarvis-vault/queue.md (root level, not a subfolder)
# ---------------------------------------------------------------------------

# Resolved at call time from `_VAULT`, not bound at import. An import-time path meant
# anything redirecting `_VAULT` (every isolated test, every scratch probe) still wrote
# the REAL queue.md -- found the hard way, when a probe of consolidation overwrote the
# live file. An explicit override (tests set `_QUEUE_FILE` directly) still wins.
_QUEUE_FILE: Path | None = None


def _queue_file() -> Path:
    return _QUEUE_FILE if _QUEUE_FILE is not None else _VAULT / "queue.md"


async def read_queue() -> str:
    """Read the intent queue from jarvis-vault/queue.md. Returns '' if missing."""
    try:
        if not _queue_file().exists():
            return ""
        content = _queue_file().read_text(encoding="utf-8")
        return content[:_MAX_READ_CHARS] if content else ""
    except Exception as e:
        log.warning("read_queue failed: %s", e)
        return ""


def queue_revision() -> str:
    """Revision marker for queue.md, same contract as `note_revision` ("" if absent).
    queue.md lives at the vault root, outside `FOLDERS`, so it needs its own."""
    import hashlib
    try:
        path = _queue_file()
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
    except Exception:
        return ""


async def vault_write_queue(content: str, expected_revision: str | None = None) -> str:
    """Rewrite the intent queue at jarvis-vault/queue.md. Verifies the write
    by reading it back before confirming, same discipline as vault_write.
    Note: this is the INTENT queue (open conversational threads) -- it is a
    different file from jarvis-vault/schedule.md's night_tasks, which is the
    only file chronos.py's scheduler actually fires from. Use
    schedule_night_task/schedule_reminder for anything that needs to fire at
    a time; this file is never read by the scheduler."""
    _ensure_vault()
    try:
        # Same optional precondition as vault_write: a caller whose content derives from
        # an earlier read (consolidation, across a long LLM call) must not overwrite a
        # queue edited in between. The tool path passes nothing and is unaffected.
        if expected_revision is not None and queue_revision() != expected_revision:
            log.warning("Vault: refusing stale overwrite of queue.md")
            return ("Error: queue.md changed since you read it — not overwriting. "
                    "Re-read it and reapply your change if it still makes sense.")
        _queue_file().write_text(content, encoding="utf-8")
        if _queue_file().read_text(encoding="utf-8") != content:
            return "Error: queue.md write did not verify on read-back — do not report this as saved."
        log.info("Vault: wrote queue.md (%d chars, verified)", len(content))
        return f"Confirmed written to jarvis-vault/queue.md ({len(content)} chars, verified by read-back)."
    except Exception as e:
        return f"Error writing queue: {e}"


# ---------------------------------------------------------------------------
# Session startup reads — loaded before each turn, injected into system prompt
# ---------------------------------------------------------------------------

async def read_owner() -> str:
    """Read people/owner.md — the living portrait. Returns '' if missing."""
    try:
        path = _VAULT / "people" / "owner.md"
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[:_MAX_READ_CHARS] if text else ""
    except Exception as e:
        log.warning("read_owner failed: %s", e)
        return ""


async def read_positions() -> str:
    """Read self/positions.md — JARVIS's evolving intellectual stances. Returns '' if missing."""
    try:
        path = _VAULT / "self" / "positions.md"
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[:_MAX_READ_CHARS] if text else ""
    except Exception as e:
        log.warning("read_positions failed: %s", e)
        return ""


async def read_latest_letter() -> str:
    """Read the most recently modified file from letters/. Returns '' if none exist."""
    try:
        letters_dir = _VAULT / "letters"
        if not letters_dir.exists():
            return ""
        files = [p for p in letters_dir.iterdir() if p.is_file()]
        if not files:
            return ""
        latest = max(files, key=lambda p: p.stat().st_mtime)
        text = latest.read_text(encoding="utf-8")
        return text[:_MAX_READ_CHARS] if text else ""
    except Exception as e:
        log.warning("read_latest_letter failed: %s", e)
        return ""


_TOPIC_PASSAGE_CAP = 1600  # chars (~400 tokens) — budget-friendly passage, not whole file


async def read_relevant_topic(query: str, query_vec: list[float] | None = None) -> str:
    """Return the most relevant topics/ passage(s), or '' if none match.

    Primary path: semantic passage search over the jarvis_semantic index
    (kind='vault', folder='topics') — surfaces passages even when the query
    uses synonyms not present in the filename. Falls back to the legacy
    filename-keyword match only when there is no index to search (Qdrant down,
    or no topic indexed). An empty search over a real index means nothing was
    relevant, and stays empty: until 2026-10-11 it fell back too, and on the
    server 21 of 40 deep turns got a 4,000-char note picked by filename words
    after the cross-encoder had rejected every passage.

    `query_vec`, when given, is reused instead of re-embedding `query` — see
    semindex.search()'s docstring.
    """
    # Semantic path first
    try:
        from backend.brain import semindex
        from backend.brain.memory import USER_ID
        from backend.storage import fact_corrections
        corrections = fact_corrections.list_for_user(USER_ID)
        hits = await semindex.search(query, kind="vault", k=2, folder="topics", query_vec=query_vec,
                                     rerank=True)
        if hits:
            passages: list[str] = []
            total = 0
            for h in hits:
                text = (h.get("text") or "").strip()
                if not text or fact_corrections.suppresses(text, corrections):
                    continue
                if total + len(text) > _TOPIC_PASSAGE_CAP:
                    text = text[: max(0, _TOPIC_PASSAGE_CAP - total)]
                if text:
                    passages.append(text)
                    total += len(text)
                if total >= _TOPIC_PASSAGE_CAP:
                    break
            if passages:
                return "\n\n".join(passages)
    except Exception as e:
        log.debug("read_relevant_topic: semantic path skipped: %s", e)

    # Keyword fallback (legacy): only when there is nothing to search.
    try:
        from backend.brain import semindex
        if semindex.can_search("vault", folder="topics"):
            return ""
        topics_dir = _VAULT / "topics"
        if not topics_dir.exists():
            return ""
        files = [p for p in topics_dir.iterdir() if p.is_file()]
        if not files:
            return ""

        query_lower  = query.lower()
        query_words  = {w for w in query_lower.split() if len(w) > 3}
        best_file    = None
        best_score   = 0

        for f in files:
            stem = f.stem.replace("_", " ").replace("-", " ").lower()
            score = 0
            # Exact phrase match in query
            if stem in query_lower:
                score += 4
            # Individual stem-word matches against query words
            for w in stem.split():
                if w in query_words:
                    score += 1
            if score > best_score:
                best_score = score
                best_file  = f

        if best_file and best_score > 0:
            text = best_file.read_text(encoding="utf-8")
            from backend.brain.memory import USER_ID
            from backend.storage import fact_corrections
            if fact_corrections.suppresses(
                    text, fact_corrections.list_for_user(USER_ID)):
                return ""
            return text[:_TOPIC_PASSAGE_CAP]
        return ""
    except Exception as e:
        log.warning("read_relevant_topic failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Frontmatter helper (research.py edits topic-note frontmatter with it).
# Skills moved to backend/brain/skills.py on 2026-10-04.
# ---------------------------------------------------------------------------

def _update_frontmatter(content: str, updates: dict[str, str]) -> str:
    """Replace or insert frontmatter keys. Returns content unchanged if there
    is no frontmatter block (never destroys a malformed file)."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    try:
        close = next(i for i, l in enumerate(lines[1:], start=1) if l.strip() == "---")
    except StopIteration:
        return content
    remaining = dict(updates)
    for i in range(1, close):
        k, sep, _ = lines[i].partition(":")
        key = k.strip()
        if sep and key in remaining:
            lines[i] = f"{key}: {remaining.pop(key)}"
    # Insert any keys not already present just before the closing fence
    for key, val in remaining.items():
        lines.insert(close, f"{key}: {val}")
        close += 1
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


# ---------------------------------------------------------------------------
# memory_search — on-demand recall across ALL memory stores (registered tool)
#
# Four stores, one tool: vault notes, past exchanges, skills (jarvis_semantic)
# and mem0 facts (hybrid BM25+dense, consolidated archive included). Default
# kind="all" fans out across every store so one call answers "do I know
# anything about X". Dense kinds get a lexical re-rank (exact-name weakness),
# vault hits are deduped per file, exchanges are recency/significance-ranked.
# ---------------------------------------------------------------------------

_MEMORY_SEARCH_KINDS = ("all", "vault", "exchange", "skill", "fact")
_MS_RESULT_CAP = 700    # per-result text cap
_MS_TOTAL_CAP  = 3000   # total output cap

# Tool-search relevance floors, per kind. Separate from semindex's floors,
# which gate the PASSIVE turn-start recall paths — calibrated here without
# touching prompt injection. Measured 2026-06-11 with
# scripts/measure_semantic_floors.py: gte-small's cosine baseline is high
# (unrelated queries score 0.72-0.81), so the old 0.30 floor filtered nothing.
#   vault:    unrelated P95 0.802 vs related min 0.824 — clean separation at 0.80
#   exchange: unrelated P95 0.763 vs related min 0.760 — overlap; 0.75 keeps all
#             related hits and cuts ~half the junk (re-rank + scores do the rest)
#   skill:    distributions overlap; 0.68 keeps this search permissive. Skills are no
#             longer injected by similarity at all (backend/brain/skills.py), so the
#             floor only shapes what this on-demand search shows.
_SEARCH_SCORE_MIN = {"vault": 0.80, "exchange": 0.75}

_MS_DAYS_BACK_MAX = 365


def _lexical_bonus(query: str, text: str) -> float:
    """Small ranking bonus for exact token overlap — compensates dense search's
    weakness on names, filenames, and rare tokens. Ranking only; never shown."""
    qtok = set(re.findall(r"[a-z0-9]{4,}", query.lower()))
    if not qtok:
        return 0.0
    ttok = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    return 0.04 * min(len(qtok & ttok), 4)


def _dedupe_by_file(hits: list[dict]) -> list[dict]:
    """Keep only the best-ranked chunk per (folder, filename). Input must
    already be sorted best-first."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for h in hits:
        key = (h.get("folder"), h.get("filename"))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


async def _search_one_kind(
    kind: str, query: str, top_k: int, cutoff_ts: float | None
) -> list[dict]:
    """Search a single store; returns hit payloads tagged with '_kind', sorted
    best-first, at most top_k. Degrades to [] on any failure."""
    try:
        if kind == "fact":
            from backend.brain.memory import search_facts
            hits = await search_facts(query, top_k)
            for h in hits:
                h["_kind"] = "fact"
            return hits[:top_k]

        from backend.brain import semindex

        if kind == "vault":
            hits = await semindex.search(
                query, kind="vault", k=top_k * 3, score_min=_SEARCH_SCORE_MIN["vault"]
            )
            if cutoff_ts is not None:
                hits = [h for h in hits if float(h.get("mtime", 0.0)) >= cutoff_ts]
            for h in hits:
                h["_rank"] = float(h.get("score", 0.0)) + _lexical_bonus(query, h.get("text", ""))
            hits.sort(key=lambda h: h["_rank"], reverse=True)
            hits = _dedupe_by_file(hits)[:top_k]

        elif kind == "exchange":
            from backend.brain.threads import rank_exchange, _dedupe_exchange_hits
            hits = await semindex.search(
                query, kind="exchange", k=top_k * 3, score_min=_SEARCH_SCORE_MIN["exchange"]
            )
            if cutoff_ts is not None:
                hits = [h for h in hits if float(h.get("timestamp", 0.0)) >= cutoff_ts]
            now = datetime.now().timestamp()
            for h in hits:
                h["_rank"] = rank_exchange(h, now) + _lexical_bonus(query, h.get("text", ""))
            # Step 2 (2026-09-08): an exchange over ~1200 chars is now chunked
            # (see semindex._index_exchange_sync) -- without this, a long
            # exchange could occupy multiple slots in the result list under
            # its own separate chunks. Same dedup discipline _dedupe_by_file
            # already applies to vault/skill hits, keyed on exchange_id
            # instead of (folder, filename).
            hits = _dedupe_exchange_hits(hits)
            hits.sort(key=lambda h: h["_rank"], reverse=True)
            hits = hits[:top_k]

        else:  # skill
            hits = await semindex.search(
                query, kind="skill", k=top_k * 2,
                score_min=semindex._SKILL_SCORE_MIN, folder="skills",
            )
            hits = [h for h in hits if str(h.get("status", "active")).lower() == "active"]
            if cutoff_ts is not None:
                hits = [h for h in hits if float(h.get("mtime", 0.0)) >= cutoff_ts]
            for h in hits:
                h["_rank"] = float(h.get("score", 0.0)) + _lexical_bonus(query, h.get("text", ""))
            hits.sort(key=lambda h: h["_rank"], reverse=True)
            hits = _dedupe_by_file(hits)[:top_k]

        for h in hits:
            h["_kind"] = kind
        return hits
    except Exception as e:
        log.debug("memory_search: %s search failed: %s", kind, e)
        return []


def _format_hit_source(h: dict) -> str:
    kind = h.get("_kind", "?")
    if kind == "fact":
        label = "fact · consolidated (pre-vault archive)" if h.get("consolidated") else "fact"
        # A05: a fact JARVIS asserted is not a fact the owner established -- see
        # memory._get_assistant_attributed_ids for why the distinction is load-bearing.
        if h.get("from_assistant"):
            label += _owner.say(" · you said this, not «owner»")
        return label
    if kind == "exchange":
        sid = (h.get("session_id") or "")[:8] or "unknown"
        ts = h.get("timestamp")
        try:
            when = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d") if ts else "?"
        except (TypeError, ValueError, OSError):
            when = "?"
        return f"exchange · session {sid} · {when}"
    # vault or skill — show the file and its note date
    folder = h.get("folder") or "?"
    filename = h.get("filename") or "?"
    try:
        when = datetime.fromtimestamp(float(h["mtime"])).strftime("%Y-%m-%d") if h.get("mtime") else ""
    except (TypeError, ValueError, OSError):
        when = ""
    source = f"{folder}/{filename}" + (f" · {when}" if when else "")
    if h.get("_pending_index"):
        # A03: written earlier in THIS turn and not indexed yet. Labelled so the model
        # knows it is reading its own very recent write rather than an established note
        # -- and so a "(score 0.00)" next to it does not read as a weak match.
        source += " · just written this turn, not yet indexed"
    if kind == "skill" and h.get("skill_name"):
        source = f"skill '{h['skill_name']}' ({source}) · open it with skill(action=\"view\")"
    return source


def _merge_pending_writes(hits: list, query: str, kind: str, top_k: int) -> list:
    """Add notes written earlier in THIS turn that indexing has not caught up with.

    A03 read-your-writes. Only for kinds that include vault notes; appended after the
    real hits and never displacing one, because a genuine semantic match is better
    evidence than a lexical fallback. De-duplicated against what the index already
    returned, so a note that IS indexed appears once, with its real score.
    """
    if kind not in ("all", "vault"):
        return hits
    try:
        from backend.context import recent_writes
        already = {
            (h.get("folder"), h.get("filename"))
            for h in hits if h.get("_kind") == "vault"
        }
        extra = recent_writes.matching_hits(query, exclude=already,
                                            limit=max(1, top_k // 2 or 1))
        if not extra:
            return hits
        log.debug("memory_search: %d just-written note(s) not yet indexed", len(extra))
        return (hits + extra)[:max(top_k, len(hits))]
    except Exception as exc:
        log.debug("memory_search: pending-write merge skipped (%s)", exc)
        return hits


async def memory_search(
    query: str, kind: str = "all", top_k: int = 3, days_back: int | None = None
) -> str:
    """Search JARVIS's long-term memory and return readable, source-labeled results.

      - kind="all" (default) — fan out across every store in parallel
      - kind="vault"    — chunks of vault notes (topics, letters, people, …)
      - kind="exchange" — past user/assistant exchanges (recency/significance ranked)
      - kind="skill"    — procedures in skills/ (open one with the skill tool)
      - kind="fact"     — mem0 extracted facts, consolidated archive included

    days_back restricts exchange/vault/skill hits to the last N days (facts
    carry no usable timestamp and are unfiltered). Returns an explicit
    "nothing matched" string (never empty); degrades to a clean message on
    any failure.
    """
    if not query or not query.strip():
        return "Error: memory_search needs a non-empty query."

    kind = (kind or "all").strip().lower()
    if kind not in _MEMORY_SEARCH_KINDS:
        return f"Error: kind must be one of {', '.join(_MEMORY_SEARCH_KINDS)} (got '{kind}')."

    try:
        top_k = max(1, min(int(top_k), 8))
    except (TypeError, ValueError):
        top_k = 3

    cutoff_ts: float | None = None
    if days_back is not None:
        try:
            back = max(1, min(int(days_back), _MS_DAYS_BACK_MAX))
            cutoff_ts = (datetime.now() - timedelta(days=back)).timestamp()
        except (TypeError, ValueError):
            pass

    try:
        from backend.brain.memory import USER_ID
        from backend.storage import fact_corrections
        corrections = fact_corrections.list_for_user(USER_ID)
        if kind == "all":
            per_kind = await asyncio.gather(*[
                _search_one_kind(k, query, top_k, cutoff_ts)
                for k in ("fact", "vault", "exchange", "skill")
            ])
            per_kind = [[h for h in lst if not fact_corrections.suppresses(
                h.get("text", ""), corrections)] for lst in per_kind]
            from backend.brain import semindex
            hits = semindex.interleave_by_rank([lst for lst in per_kind if lst], top_k)
        else:
            hits = await _search_one_kind(kind, query, top_k, cutoff_ts)
            hits = [h for h in hits if not fact_corrections.suppresses(
                h.get("text", ""), corrections)]
        hits = _merge_pending_writes(hits, query, kind, top_k)
        hits = [h for h in hits if not fact_corrections.suppresses(
            h.get("text", ""), corrections)]
    except Exception as e:
        return f"Error searching memory: {e}"

    if not hits:
        scope = "memory store" if kind == "all" else f"{kind} memory"
        window = f" in the last {int(days_back)} days" if cutoff_ts is not None else ""
        return (
            f"No {scope} matched '{query}'{window} above the relevance threshold "
            f"— you have nothing on this yet."
        )

    lines: list[str] = []
    total = 0
    for i, h in enumerate(hits, 1):
        score = h.get("score")
        score_str = f"{float(score):.2f}" if isinstance(score, (int, float)) else "?"
        text = (h.get("text") or "").strip()
        if len(text) > _MS_RESULT_CAP:
            text = text[:_MS_RESULT_CAP].rstrip() + " […]"
        block = f"{i}. [{_format_hit_source(h)}] (score {score_str})\n{text}"
        if total + len(block) > _MS_TOTAL_CAP and lines:
            break
        lines.append(block)
        total += len(block)

    scope = "result(s) across all memory stores" if kind == "all" else f"{kind} result(s)"
    return f"Found {len(lines)} {scope} for '{query}':\n\n" + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# memory_timeline — temporal recall over conversation threads (registered tool)
#
# Semantic recall answers "what do I know about X"; this answers "what
# happened WHEN" — last Tuesday, two weeks ago, since the start of the month.
# Scrolls jarvis_threads by timestamp and groups results by session.
# ---------------------------------------------------------------------------

_TL_LINE_CAP    = 220    # per-exchange line cap
_TL_TOTAL_CAP   = 3500   # total output cap — oldest sessions dropped first
_TL_MAX_DAYS    = 90     # widest allowed window


async def memory_timeline(
    start_date: str | None = None,
    end_date: str | None = None,
    days_back: int | None = None,
) -> str:
    """Chronological view of past conversations in a date window.

    Pass ISO dates (YYYY-MM-DD, end inclusive) or days_back as a relative
    window. Defaults to the last 7 days when nothing is given. Returns
    exchanges grouped by session, oldest first. Degrades to a clean message
    when Qdrant is down or the window is empty.
    """
    now = datetime.now()

    def _parse(s: str) -> datetime:
        return datetime.strptime(s.strip(), "%Y-%m-%d")

    try:
        if start_date:
            start = _parse(start_date)
            end = _parse(end_date) + timedelta(days=1) if end_date else now
        else:
            back = days_back if days_back is not None else 7
            back = max(1, min(int(back), _TL_MAX_DAYS))
            start = now - timedelta(days=back)
            end = now
    except (ValueError, TypeError) as e:
        return f"Error: could not parse dates ({e}). Use YYYY-MM-DD or days_back."

    if (end - start).days > _TL_MAX_DAYS:
        start = end - timedelta(days=_TL_MAX_DAYS)

    try:
        from backend.brain.threads import threads_in_range
        payloads = await threads_in_range(start.timestamp(), end.timestamp())
    except Exception as e:
        return f"Error reading conversation history: {e}"

    # end is exclusive (midnight after the last requested day) — display inclusive
    window = (
        f"{start.strftime('%Y-%m-%d')} → "
        f"{min(end - timedelta(seconds=1), now).strftime('%Y-%m-%d')}"
    )
    if not payloads:
        return (
            f"No conversations recorded between {window} "
            f"(today is {now.strftime('%Y-%m-%d')}). Either nothing happened "
            f"then, or it predates conversation threading."
        )

    # Group consecutive threads by session_id, preserving chronological order
    sessions: list[tuple[str, list[dict]]] = []
    for p in payloads:
        sid = p.get("session_id") or "unknown"
        if sessions and sessions[-1][0] == sid:
            sessions[-1][1].append(p)
        else:
            sessions.append((sid, [p]))

    def _line(p: dict) -> str:
        ts = datetime.fromtimestamp(float(p.get("timestamp", 0.0)))
        if p.get("high"):
            u = (p.get("user") or "").strip().replace("\n", " ")
            a = (p.get("assistant") or "").strip().replace("\n", " ")
            body = f"[significant] User: {u[:90]} | JARVIS: {a[:110]}"
        else:
            body = (p.get("summary") or "").strip().replace("\n", " ")
        if len(body) > _TL_LINE_CAP:
            body = body[:_TL_LINE_CAP - 1].rstrip() + "…"
        return f"  {ts.strftime('%H:%M')}  {body}"

    blocks: list[str] = []
    for sid, items in sessions:
        first = datetime.fromtimestamp(float(items[0].get("timestamp", 0.0)))
        last  = datetime.fromtimestamp(float(items[-1].get("timestamp", 0.0)))
        span  = first.strftime("%a %Y-%m-%d %H:%M")
        if last.strftime("%H:%M") != first.strftime("%H:%M"):
            span += f" → {last.strftime('%H:%M')}"
        header = f"— Session {sid[:8]} · {span} · {len(items)} exchange(s)"
        blocks.append(header + "\n" + "\n".join(_line(p) for p in items))

    # Keep the most recent sessions if the window is too verbose
    out_blocks: list[str] = []
    total = 0
    truncated = False
    for block in reversed(blocks):
        if total + len(block) > _TL_TOTAL_CAP and out_blocks:
            truncated = True
            break
        out_blocks.append(block)
        total += len(block)
    out_blocks.reverse()

    header = (
        f"Conversation timeline {window} (today is {now.strftime('%Y-%m-%d')}): "
        f"{len(payloads)} exchange(s) across {len(sessions)} session(s)."
    )
    if truncated:
        header += " Older sessions omitted for length — narrow the range to see them."
    return header + "\n\n" + "\n\n".join(out_blocks)
