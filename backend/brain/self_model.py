"""JARVIS self-knowledge module.

Self-knowledge is sliced from docs/SELF_MODEL.md, the "Active self-model" contract. No LLM is called: the digest is a
cleaned, budget-capped view of those current sections. It refreshes whenever
CLAUDE.md changes (content-hash keyed cache), not on VERSION bumps.

When a user message triggers a relevant section (e.g. "how does the frontend work" →
'frontend', "what version are you" → 'router'), that section's text is injected into the
system prompt so JARVIS answers from actual current knowledge, not stale assumptions.

The 11 logical sections and the CLAUDE.md headings each is built from are defined in
_SECTION_HEADINGS below:
  router      — routing logic + version number
  memory      — mem0 facts, threading, consolidation, sleep markers, semantic index,
                positions, predictive portrait, rumination
  claude      — chat pipeline, streaming, tool dispatch, Discord bridge
  personality — character engine, mood drift, self-model (this file)
  tts         — voice synthesis (ElevenLabs / edge-tts / kokoro)
  stt         — speech-to-text (faster-whisper)
  wake        — wake-word detection
  tools       — browser, gmail, calendar, spotify, vault, skills, Discord, code graph
  main        — FastAPI server + WebSocket + startup lifecycle
  frontend    — React + Electron UI, Three.js neural sphere
  scripts     — PowerShell setup / run / teardown scripts
"""

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import re
import threading
from typing import Optional

from backend import owner as _owner
from backend.brain import pacing
from backend.brain.version import VERSION

log = logging.getLogger(__name__)

_BACKEND_ROOT = pathlib.Path(__file__).parent.parent   # .../backend/
_PROJECT_ROOT = _BACKEND_ROOT.parent                   # the repository root
_CACHE_FILE   = _PROJECT_ROOT / ".self_model_cache.json"
_CLAUDE_MD    = _PROJECT_ROOT / "docs" / "SELF_MODEL.md"   # the self-model source

# 1500 until P4 (2026-09-30): the tools and server sections were both within 25 characters of it and the
# plan-keeping loop needed a sentence in one; each section is injected only on a matching question.
# A heading may use the whole section allowance. It was 1200 while the section cap was 1500,
# and "Active server and autonomous work" (1270 chars) was silently losing its last sentence
# -- the one saying unattended work never sends email -- from what JARVIS reads about himself.
# 1800 until 2026-10-04: two Discord commands (/skills, /skill) took the tools section, which
# joins the tools heading and the whole command list, to 1,983 characters.
# 2600 from the research-and-reading port: the server section kept gaining sentences
# (research that follows reading, commands on the phone, the portrait).
_HEADING_CHAR_CAP = 2600
_SECTION_CHAR_CAP = 2600


# ---------------------------------------------------------------------------
# Section → CLAUDE.md current-contract heading. The historical implementation
# notes often begin with retired behavior and were previously sliced at their
# START, making the self-model confidently repeat obsolete capabilities.
# Matching is normalised-substring (see _match_heading) so small heading edits
# still resolve.
# ---------------------------------------------------------------------------

_SECTION_HEADINGS: dict[str, list[str]] = {
    "router": ["Active model routing"],
    "memory": ["Active memory and continuity"],
    "claude": ["Active conversation engine"],
    "personality": ["Active personality and self-knowledge"],
    "tts": ["Active speech synthesis"],
    "stt": ["Active transcription"],
    "wake": ["Active wake word"],
    "tools": ["Active tools and boundaries", "Discord Commands — quick reference"],
    "main": ["Active server and autonomous work"],
    "frontend": ["Active desktop interface"],
    "scripts": ["Active launch scripts"],
}


_digest: dict[str, str] = {}
_lock   = threading.Lock()
_ready  = False


# ---------------------------------------------------------------------------
# CLAUDE.md parsing → self-knowledge digest (primary path, zero LLM)
# ---------------------------------------------------------------------------

_FENCE_RE     = re.compile(r"^\s*(```|~~~)")
_HEADING_RE   = re.compile(r"^(#{2,4})\s+(.*\S)\s*$")
_MD_LINK_RE   = re.compile(r"\[([^\]]+)\]\([^)]+\)")   # [text](url) → text


def _normalise(s: str) -> str:
    """Lowercase, strip backticks, collapse non-alphanumerics to single spaces."""
    s = s.replace("`", "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _parse_claude_md() -> dict[str, str]:
    """Parse CLAUDE.md into {heading_text: body_text}, fence-aware.

    Lines inside fenced code blocks are never treated as headings — the Skills
    section embeds a SKILL.md example whose '## When to use' etc. must not be
    parsed as real headings.
    """
    try:
        # The owner markers (backend/owner.py) are filled in as the file is read.
        lines = _owner.say(_CLAUDE_MD.read_text(encoding="utf-8", errors="replace")).splitlines()
    except Exception as e:
        log.warning("self_model: could not read %s: %s", _CLAUDE_MD.name, e)
        return {}

    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current is not None:
                sections[current].append(line)
            continue
        m = _HEADING_RE.match(line) if not in_fence else None
        if m:
            current = m.group(2).strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {h: "\n".join(body).strip() for h, body in sections.items()}


def _find_heading(parsed: dict[str, str], wanted: str) -> Optional[str]:
    """Return the actual CLAUDE.md heading key matching `wanted`, or None.

    Exact (normalised) match first, then substring either direction so small
    heading edits still resolve.
    """
    want = _normalise(wanted)
    for h in parsed:
        if _normalise(h) == want:
            return h
    # A heading that EXTENDS the wanted name (a date or subtitle appended) is the edit
    # this tolerance exists for, so it wins. Only then fall back to a heading the wanted
    # name contains (one renamed shorter) -- and the most specific such heading, never the
    # first. First-wins let `## Architecture`, near the top of the file, capture
    # "Architecture Roadmap — review pass" and inject the wrong section.
    for h in parsed:
        if want in _normalise(h):
            return h
    shorter = [h for h in parsed if _normalise(h) and _normalise(h) in want]
    return max(shorter, key=lambda h: len(_normalise(h))) if shorter else None


def _match_heading(parsed: dict[str, str], wanted: str) -> str:
    """Return the body of the CLAUDE.md heading matching `wanted`, or ''."""
    key = _find_heading(parsed, wanted)
    return parsed[key] if key is not None else ""


def _clean_for_speech(text: str) -> str:
    """Strip Markdown noise so the slice reads as plain prose (no LLM).

    Drops fenced code blocks entirely, removes heading hashes, table pipes and
    backticks, and unwraps [text](url) links. Collapses blank-line runs.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)       # heading hashes
        line = _MD_LINK_RE.sub(r"\1", line)          # links → text
        line = line.replace("`", "").replace("|", " ")
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)     # collapse blank runs
    return cleaned.strip()


def _truncate(text: str, cap: int) -> str:
    """Truncate to `cap` chars, preferring a paragraph/sentence boundary."""
    if len(text) <= cap:
        return text
    window = text[:cap]
    brk = window.rfind("\n\n")
    if brk < cap // 2:
        brk = window.rfind(". ")
    if brk > cap // 2:
        window = window[: brk + 1]
    return window.rstrip() + " …"


def _build_digest_from_claudemd() -> dict[str, str]:
    """Build the {section: text} digest from CLAUDE.md. Pure, instant, no API.

    Each mapped heading is capped individually (_HEADING_CHAR_CAP) so every heading
    is represented even when a section maps to many headings (e.g. `tools`), then the
    joined section is capped at _SECTION_CHAR_CAP as a ceiling.
    """
    parsed = _parse_claude_md()
    if not parsed:
        return {}
    digest: dict[str, str] = {}
    for section, headings in _SECTION_HEADINGS.items():
        parts: list[str] = []
        for h in headings:
            body = _match_heading(parsed, h)
            if body:
                cleaned = _clean_for_speech(body)
                if cleaned:
                    if len(cleaned) > _HEADING_CHAR_CAP:
                        log.warning(
                            "self_model: heading '%s' is %d chars, over _HEADING_CHAR_CAP=%d -- "
                            "its last sentences are being silently cut from the '%s' section. "
                            "Shorten the heading or raise the cap.",
                            h, len(cleaned), _HEADING_CHAR_CAP, section)
                    parts.append(_truncate(cleaned, _HEADING_CHAR_CAP))
        joined = "\n\n".join(parts).strip()
        if len(joined) > _SECTION_CHAR_CAP:
            log.warning(
                "self_model: section '%s' is %d chars, over _SECTION_CHAR_CAP=%d -- "
                "content from its later headings (%s) is being silently truncated away. "
                "Raise _SECTION_CHAR_CAP or trim/split this section's heading list.",
                section, len(joined), _SECTION_CHAR_CAP, ", ".join(headings),
            )
        text = _truncate(joined, _SECTION_CHAR_CAP)
        if text:
            digest[section] = text
    return digest


def _claude_md_hash() -> str:
    try:
        return hashlib.sha256(_CLAUDE_MD.read_bytes()).hexdigest()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache(expected_hash: str) -> Optional[dict[str, str]]:
    """Return cached sections only when CLAUDE.md is unchanged since the build."""
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if data.get("claude_md_hash") != expected_hash or not expected_hash:
            return None
        sections = data.get("sections")
        # Empty sections dict means a previous build failed — treat as a miss.
        if not sections:
            return None
        return sections
    except Exception:
        return None


def _save_cache(sections: dict[str, str], claude_md_hash: str) -> None:
    try:
        _CACHE_FILE.write_text(
            json.dumps(
                {"claude_md_hash": claude_md_hash, "version": VERSION,
                 "sections": sections},
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        log.debug("self_model: cache write skipped: %s", e)


# ---------------------------------------------------------------------------
# Startup warmup
# ---------------------------------------------------------------------------

def _warmup_worker() -> None:
    """Populate the digest from CLAUDE.md. Cached by CLAUDE.md content hash.

    Zero LLM calls — the digest is a cleaned, capped view of CLAUDE.md's own
    sections. Rebuilds when CLAUDE.md changes (hash mismatch); evolution.py
    separately derives update entries from Git commits.
    """
    global _digest, _ready
    try:
        cur_hash = _claude_md_hash()
        sections = _load_cache(cur_hash)
        if sections is not None:
            log.info("self_model: loaded from cache (CLAUDE.md unchanged, %d sections)",
                     len(sections))
        else:
            log.info("self_model: building digest from CLAUDE.md…")
            sections = _build_digest_from_claudemd()

            if sections:
                _save_cache(sections, cur_hash)
                log.info("self_model: digest built from CLAUDE.md (%d/%d sections, no API)",
                         len(sections), len(_SECTION_HEADINGS))
                # No evolution entry is written here. `backend/brain/evolution.py`
                # owns that file now, deriving entries from git rather than from this
                # hash comparison. The old writer that lived here was deleted rather
                # than left dormant: two writers to one file is the competing
                # implementation A10 exists to prevent, and a dormant one is a
                # resurrection hazard. It was also the weaker source -- it only fired
                # when a backend happened to be running at the moment CLAUDE.md
                # changed (measured: 44 commits over 13 days produced zero entries),
                # it could only ever see CLAUDE.md, and it emitted section names
                # rather than substance.
            else:
                log.warning(
                    "self_model: CLAUDE.md produced 0 sections — "
                    "skipping cache write so next startup retries"
                )
    except Exception as e:
        log.warning("self_model: build failed: %s", e)
        sections = {}
    with _lock:
        # Replace (not merge) on a real rebuild so a section whose headings were
        # entirely removed from CLAUDE.md actually drops out of the live digest,
        # instead of surviving forever via update()'s never-delete semantics.
        # On failure/empty sections is {} -- skip so the last good digest is kept.
        if sections:
            _digest.clear()
            _digest.update(sections)
        _ready = True


def warm_self_model() -> None:
    """Trigger digest build in a background thread at startup."""
    threading.Thread(target=_warmup_worker, daemon=True, name="self-model-warmup").start()


# ---------------------------------------------------------------------------
# Live reload — periodic re-check, not just at startup
# ---------------------------------------------------------------------------
#
# Live incident (2026-08-28): the digest was ONLY ever built once, at process
# startup. A dev session that edits CLAUDE.md repeatedly while the backend
# keeps running (the normal Claude Code workflow) left JARVIS serving a
# self-model frozen at whatever CLAUDE.md looked like at launch -- new
# sections, new tools, whole new subsystems added mid-session were invisible
# to him until a manual restart, even though this module's own docstring
# claims the digest "refreshes whenever CLAUDE.md changes." That claim was
# only true across restarts, not true live, and JARVIS confidently
# confabulated a false timeline ("I've always had this") rather than being
# able to say "let me check" -- because there was nothing live to check.
#
# _warmup_worker() is already idempotent and safe to call repeatedly: a
# hash match is a cheap no-op (reads the cache, re-assigns the same
# in-memory digest), a hash mismatch does a real rebuild. So the periodic loop
# below is just _warmup_worker() on a timer -- no new rebuild logic needed.

_RECHECK_INTERVAL_S = 300.0   # 5 min -- cheap (one hash comparison) when unchanged


async def _recheck_cycle() -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _warmup_worker)


async def run_loop() -> None:
    """Started as a background task from main.py's lifespan (bootstrap.py's
    loop registry). Purely local file I/O + a hash comparison -- no LLM, no
    Qdrant, safe to run every cycle regardless of whether a conversation
    turn is in flight, so unlike research/chronos this needs no busy_check."""
    await pacing.run_periodic(
        name="self_model", check_interval_s=_RECHECK_INTERVAL_S, on_cycle=_recheck_cycle,
    )


# ---------------------------------------------------------------------------
# Public retrieval API (unchanged)
# ---------------------------------------------------------------------------

def get_section(name: str) -> str:
    """Return the CLAUDE.md-derived description for one section.

    Returns '' if the digest isn't ready yet or the section doesn't exist.
    """
    with _lock:
        return _digest.get(name, "")


def get_sections(*names: str) -> str:
    """Return descriptions for multiple sections, joined with blank lines."""
    with _lock:
        return "\n\n".join(_digest[n] for n in names if n in _digest)


def get_all_sections() -> dict[str, str]:
    """Return a copy of every section's description, keyed by section name."""
    with _lock:
        return dict(_digest)


def full_digest() -> str:
    """Return all section descriptions concatenated."""
    with _lock:
        return "\n\n".join(_digest.values())


def is_ready() -> bool:
    """True once the background digest build has completed."""
    return _ready
