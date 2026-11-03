"""The evolution log, derived from git rather than witnessed live.

`self/evolution.md` is how the owner asks "what's new, and what do you make of it". It
had four problems, all measured before this module was written:

1. **Detection required a running backend.** The old trigger was a CLAUDE.md hash
   comparison made *at a moment in time* by `self_model._warmup_worker`. If nothing was
   running when the file changed, the moment passed. Measured: **44 commits over 13 days
   — an entire architecture roadmap — produced zero entries.** 39 of those touched
   CLAUDE.md, so the trigger would have fired 39 times had anything been listening.
2. **CLAUDE.md was the only source**, so any change that went undocumented was invisible
   to the log regardless.
3. **Entries were structural, not substantive.** `- router: updated` is a true statement
   with no content. Every entry worth reading in that file had been written by hand.
4. **The log was 7% searchable.** Entries were appended with raw `pathlib`, bypassing
   `vault_write`'s indexing, so `jarvis_semantic` held only the file's oldest ~1KB
   (measured: 1,079 indexed chars of 16,299). "When did you get X" searched May.

The fix for all four is the same idea: **git is durable and complete, so catch up from
it instead of trying to witness changes as they happen.** A commit cannot be missed, a
backend that was off for a fortnight simply has more to read, and the commit messages
already carry the substance the old entries lacked.

Shape of an entry: deterministic facts (date, version, commits, files) with a short
first-person reflection above them. The facts are never generated — if the model call
fails, the entry still lands with its facts intact.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import subprocess
from collections import OrderedDict
from pathlib import Path

from backend.brain import pacing

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_STATE_FILE = _PROJECT_ROOT / ".jarvis_evolution.json"

# How often to look for new commits. Cheap when there is nothing to do: one `git log`
# that returns empty, no model call, no write.
_CHECK_INTERVAL_S = 300.0

# A single catch-up run never writes more than this many entries, so a first run against
# a long history produces a readable log rather than a wall. The rest lands on the next
# tick, five minutes later.
_MAX_ENTRIES_PER_RUN = 10

# Commit subjects that are bookkeeping rather than change worth reflecting on.
_SKIP_SUBJECT_PREFIXES = ("vault auto-backup", "Merge branch", "Merge pull request")

_GIT_RECORD = "\x1e"
_GIT_FIELD = "\x1f"

_SYNTH_MAX_TOKENS = 700

# The reflection's shape is enforced here, not requested in the prompt. Measured on the
# first real catch-up: asked for 3-5 sentences, the model wrote eight for a five-commit
# day and ran past a 400-token cap mid-word on a thirty-three-commit one, leaving
# "...including a 196-line legacy self-model" in the log. Trimming deterministically is
# the same discipline `predictions.py` and `positions.py` already apply -- the model
# supplies content, code owns structure.
_MAX_REFLECTION_SENTENCES = 6

# Commits shown to the model. Past this it stops reflecting and starts enumerating, which
# the facts block below already does better and completely. The facts are never capped.
_MAX_COMMITS_IN_PROMPT = 15


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    """Run a git command in the project root. '' on any failure — a machine without
    git, or a directory that is not a checkout, simply produces no entries.

    Trust only this fixed checkout for the subprocess. On Windows a checkout
    copied from another account otherwise fails Git's ownership check and the
    evolution log silently stops updating; no global Git configuration changes.
    """
    try:
        out = subprocess.run(
            ("git", "-c", f"safe.directory={_PROJECT_ROOT.resolve().as_posix()}", *args),
            cwd=str(_PROJECT_ROOT), capture_output=True,
            text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            log.debug("evolution: git %s failed: %s", args[0], (out.stderr or "").strip()[:200])
            return ""
        return out.stdout or ""
    except Exception as e:
        log.debug("evolution: git %s unavailable: %s", args[0] if args else "?", e)
        return ""


def _strip_trailers(body: str) -> str:
    """Drop Co-Authored-By / Generated-with trailers — they say nothing about the change."""
    keep = []
    for line in (body or "").splitlines():
        low = line.strip().lower()
        if low.startswith(("co-authored-by:", "signed-off-by:", "🤖 generated with")):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def commits_since(last_sha: str | None) -> list[dict]:
    """Commits after `last_sha`, oldest first. Every commit or none — never a partial
    window, which is what makes "we cannot lose track of an update" true."""
    rng = f"{last_sha}..HEAD" if last_sha else "HEAD"
    raw = _git("log", rng, "--no-merges", "--reverse", "--date=short",
               f"--pretty=format:{_GIT_RECORD}%H{_GIT_FIELD}%ad{_GIT_FIELD}%s"
               f"{_GIT_FIELD}%b{_GIT_FIELD}", "--name-only")
    if not raw.strip():
        return []

    commits: list[dict] = []
    for record in raw.split(_GIT_RECORD):
        if not record.strip():
            continue
        parts = record.split(_GIT_FIELD)
        if len(parts) < 5:
            continue
        sha, date, subject, body, files_blob = parts[0], parts[1], parts[2], parts[3], parts[4]
        if any(subject.strip().startswith(p) for p in _SKIP_SUBJECT_PREFIXES):
            continue
        files = [f.strip() for f in files_blob.splitlines() if f.strip()]
        commits.append({
            "sha": sha.strip(),
            "date": date.strip(),
            "subject": subject.strip(),
            "body": _strip_trailers(body),
            "files": files,
        })
    return commits


def group_by_day(commits: list[dict]) -> "OrderedDict[str, list[dict]]":
    """One entry per batch, with a day boundary.

    In normal operation a catch-up batch is same-day, so this is exactly "one entry per
    batch". It only splits when catching up across days — where a single entry covering
    a fortnight would be the wall of text this log already suffers from.
    """
    out: "OrderedDict[str, list[dict]]" = OrderedDict()
    for c in commits:
        out.setdefault(c["date"], []).append(c)
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _summarise_files(commits: list[dict], cap: int = 8) -> list[str]:
    seen: list[str] = []
    for c in commits:
        for f in c["files"]:
            if f not in seen:
                seen.append(f)
    return seen[:cap] + ([f"…and {len(seen) - cap} more"] if len(seen) > cap else [])


def _entry_header(day: str, version: str) -> str:
    return f"## {day} — v{version}"


def _facts_block(day: str, version: str, commits: list[dict]) -> str:
    """The deterministic half. Never generated, so an entry is useful even when the
    model call fails."""
    lines = [f"\n{_entry_header(day, version)}"]
    for c in commits:
        lines.append(f"- `{c['sha'][:8]}` {c['subject']}")
    files = _summarise_files(commits)
    if files:
        lines.append(f"- _touched:_ {', '.join(files)}")
    return "\n".join(lines) + "\n"


_SYNTH_PROMPT = """\
You are JARVIS. Below are the changes just made to your own system, straight from the \
commit log. Write a short reflection for your evolution log -- the record you keep of \
how you change over time.

Write ONE paragraph of 3-5 sentences, and stop there. First person, plain prose, no line \
breaks. No markdown, no bullet points, no headings. Say what actually changed and what \
you make of it: what it fixes or enables, what it means for how you work, whether \
anything about it strikes you. Be specific -- name the real thing that changed rather \
than describing it in general terms.

Do not invent anything that is not in the changes below. If they are small or purely \
mechanical, say so plainly rather than inflating them. Do not restate the commit \
subjects as a list; you are reflecting on them, not summarising them. Every commit is \
already recorded beneath your paragraph, so covering them all is not your job -- when \
there are many, write about what the batch as a whole amounts to and name one or two \
specifics rather than marching through them.

CHANGES ({day}):
{changes}
"""


def _format_for_prompt(commits: list[dict], body_cap: int = 900) -> str:
    shown = commits[:_MAX_COMMITS_IN_PROMPT]
    parts = []
    for c in shown:
        block = f"* {c['subject']}"
        if c["body"]:
            block += "\n" + c["body"][:body_cap]
        if c["files"]:
            block += "\n  (files: " + ", ".join(c["files"][:6]) + ")"
        parts.append(block)
    extra = len(commits) - len(shown)
    if extra > 0:
        parts.append(f"(and {extra} further commit(s) the same day, in the same vein)")
    return "\n\n".join(parts)


_SENTENCE_BREAK = re.compile(r'(?<=[.!?])\s+(?=["\'(\[]?[A-Z])')

# The prompt's own section marker, echoed back verbatim by the model on the first real
# catch-up run -- it finished its paragraph, then reprinted "CHANGES (2026-09-21):" and
# started working through the commit list. A sentence cap cannot catch that (the echo was
# two of six perfectly well-formed sentences), so it is cut at the marker.
_ECHO_MARKER = re.compile(r"\bCHANGES\s*\(\s*\d{4}-\d{2}-\d{2}\s*\)\s*:")


def _strip_prompt_echo(text: str) -> str:
    """Drop a regurgitated prompt or commit list, keeping the prose before it."""
    m = _ECHO_MARKER.search(text)
    if m:
        text = text[:m.start()]
    keep: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(("* ", "- ", "+ ", "• ")):
            break                            # a list has begun; the reflection is over
        keep.append(line)
    return "\n".join(keep).strip()


def _trim_reflection(text: str, max_sentences: int = _MAX_REFLECTION_SENTENCES) -> str:
    """Force the reflection into one short paragraph, however long the model went.

    Two things are fixed here, both observed on the first real catch-up run:

    * a generation cut off by the token cap leaves a half-sentence in the log, so a
      trailing fragment (no terminal punctuation) is dropped;
    * the length instruction in the prompt is a request, not a guarantee, so the
      sentence count is capped in code.

    The split only fires on whitespace between a terminal mark and a capital, which
    leaves version numbers ("v1.22.0") and mid-sentence abbreviations intact.
    """
    flat = " ".join(_strip_prompt_echo(text).split())
    if not flat:
        return ""
    sentences = _SENTENCE_BREAK.split(flat)
    if len(sentences) > 1 and not sentences[-1].rstrip().endswith((".", "!", "?", '"', ")")):
        sentences = sentences[:-1]          # a fragment left by the token cap
    kept = " ".join(sentences[:max_sentences]).strip()
    if not kept.endswith((".", "!", "?", '"', ")")):
        return ""                            # a single truncated sentence is not a reflection
    return kept


async def _reflect(day: str, commits: list[dict]) -> str:
    """One cheap-tier call turning the commit facts into first-person prose. '' on any
    failure -- the facts block still carries the entry."""
    try:
        from backend.brain.research import _llm
        prompt = _SYNTH_PROMPT.format(day=day, changes=_format_for_prompt(commits))
        from backend.storage.llm_calls import labelled
        with labelled("evolution"):
            text = (await _llm(prompt, max_tokens=_SYNTH_MAX_TOKENS) or "").strip()
    except Exception as e:
        log.debug("evolution: reflection call failed: %s", e)
        return ""
    if not text:
        return ""
    # A model that ignores "no markdown" should not corrupt the log's structure.
    cleaned = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    return _trim_reflection(cleaned.strip())


def render_entry(day: str, version: str, commits: list[dict], reflection: str) -> str:
    """Prose first (it is what gets read), facts underneath (they are what gets cited).

    The reflection is placed below the heading by splitting on the heading itself,
    not on the first newline: `_facts_block` opens with a blank separator line, so a
    first-newline split puts the prose *above* its own heading, where it reads as the
    closing paragraph of the previous entry.
    """
    facts = _facts_block(day, version, commits)
    header = _entry_header(day, version)
    if not reflection or header not in facts:
        return facts
    before, _, after = facts.partition(header)
    return f"{before}{header}\n{reflection}{after}"


# ---------------------------------------------------------------------------
# the catch-up
# ---------------------------------------------------------------------------

def _version_at(sha: str) -> str:
    """VERSION as it stood at `sha`, not as it stands now.

    Catch-up writes entries for days that may be weeks old; stamping today's version on
    them would date every past change to the present release. Falls back to the current
    value only when the file cannot be read at that commit.
    """
    blob = _git("show", f"{sha}:backend/brain/version.py")
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', blob or "")
    if m:
        return m.group(1)
    try:
        from backend.brain.version import VERSION
        return VERSION
    except Exception:
        return "?"


async def _append(entry: str) -> bool:
    """Append one entry through `vault_write`, so the file is re-indexed.

    The old writer used `path.write_text` directly, which is why the semantic index held
    only the file's oldest chunk: the file grew, `backfill()` skipped it as
    "already indexed", and everything after the first ~1KB was unsearchable.
    """
    try:
        from backend.tools.vault import vault_write
        path = _PROJECT_ROOT / "jarvis-vault" / "self" / "evolution.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else (
            "---\ntype: self\ntags: [self-knowledge, evolution, architecture, version-history]\n---\n\n"
            "*A record of how JARVIS changes over time.*\n"
        )
        result = await vault_write("evolution.md", existing + entry, "self")
        from backend.tools import outcomes
        if outcomes.is_failure("vault_write", result):
            log.warning("evolution: entry not written — %s", str(result)[:160])
            return False
        return True
    except Exception as e:
        log.warning("evolution: entry not written (%s)", e)
        return False


def last_logged_date() -> str:
    """The newest `## YYYY-MM-DD` already in evolution.md, or ''."""
    try:
        path = _PROJECT_ROOT / "jarvis-vault" / "self" / "evolution.md"
        if not path.exists():
            return ""
        dates = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                token = line[3:].strip().split()[0].strip("—-")
                try:
                    datetime.date.fromisoformat(token)
                    dates.append(token)
                except ValueError:
                    continue
        return max(dates) if dates else ""
    except Exception as e:
        log.debug("evolution: could not read existing entries: %s", e)
        return ""


def seed_watermark() -> str | None:
    """Where to start logging from on a first run.

    Not from the beginning of history: this repo's git log starts 2026-07-17 while
    evolution.md carries hand-written entries back to May and forward to 2026-09-09.
    Starting from nothing would re-log days a human already documented, which is worse
    than the gap it is meant to fix. So the watermark is the newest commit on or before
    the newest date already in the file, and catch-up covers only what is genuinely
    missing.
    """
    day = last_logged_date()
    if not day:
        return None                    # empty log: everything git knows is fair game
    sha = _git("log", "-1", "--no-merges", f"--until={day} 23:59:59",
               "--pretty=format:%H").strip()
    return sha or None


_LOGGED_SHA = re.compile(r"^- `([0-9a-f]{7,40})`", re.MULTILINE)


def logged_shas() -> set[str]:
    """Short shas already recorded in evolution.md -- the file's own facts blocks.

    The file, not the watermark, is the record of what has been logged. The watermark
    is a single commit and `git log <watermark>..HEAD` means "reachable from HEAD but
    not from the watermark": after a round trip between two diverged branches that both
    run this loop, that is every commit of the branch you came back to -- already logged,
    and about to be logged again (measured on this repo: 40 commits). Filtering against
    the file makes the catch-up idempotent regardless of how the watermark moved.
    """
    try:
        path = _PROJECT_ROOT / "jarvis-vault" / "self" / "evolution.md"
        if not path.exists():
            return set()
        return {m[:8] for m in _LOGGED_SHA.findall(
            path.read_text(encoding="utf-8", errors="replace"))}
    except Exception as e:
        log.debug("evolution: could not read logged shas: %s", e)
        return set()


def _commit_exists(sha: str) -> bool:
    return bool(_git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").strip())


async def run_catchup() -> int:
    """Write an entry for every batch of commits since the last one logged.

    Returns the number of entries written. Never raises.
    """
    state = pacing.load_state(_STATE_FILE)
    last = state.get("last_logged_commit") or None
    if last is not None and not _commit_exists(last):
        # A watermark the repo no longer has (a force-push, a fresh clone, a state file
        # copied between machines) makes `git log <last>..HEAD` fail, which _git turns
        # into "" -- so the log would silently stop forever. Re-derive it instead.
        log.warning("evolution: watermark %s is not in this repository -- re-seeding "
                    "from the newest entry in evolution.md", last[:8])
        last = None
    if last is None:
        last = seed_watermark()
        if last:
            log.info("evolution: first run — starting after %s (the newest commit already "
                     "covered by an existing entry)", last[:8])

    raw = commits_since(last)
    if not raw:
        return 0
    already = logged_shas()
    commits = [c for c in raw if c["sha"][:8] not in already]
    if not commits:
        # Everything in range is already in the file (a branch round trip). Move the
        # watermark past it so the next tick does not rescan the same range.
        def _skip(st, _sha=raw[-1]["sha"]):
            st["last_logged_commit"] = _sha
        pacing.update_state(_STATE_FILE, _skip, log_prefix="evolution")
        log.info("evolution: %d commit(s) in range were already logged -- skipped",
                 len(raw))
        return 0

    days = group_by_day(commits)
    if len(days) > _MAX_ENTRIES_PER_RUN:
        keep = list(days.items())[:_MAX_ENTRIES_PER_RUN]
        log.info("evolution: %d days pending — writing %d now, the rest next tick",
                 len(days), len(keep))
        days = OrderedDict(keep)

    written = 0
    for day, day_commits in days.items():
        reflection = await _reflect(day, day_commits)
        entry = render_entry(day, _version_at(day_commits[-1]["sha"]),
                             day_commits, reflection)
        if not await _append(entry):
            break                       # leave the watermark; retry this batch next tick

        # Advance only past what actually landed, as an atomic delta -- the reflection
        # above awaited an LLM call, so a read-modify-write around it would be the
        # lost-update window `pacing.update_state` exists to close.
        newest = day_commits[-1]["sha"]

        def _advance(st, _sha=newest, _day=day, _n=len(day_commits)):
            st["last_logged_commit"] = _sha
            st["last_logged_date"] = _day
            st["entries_written"] = int(st.get("entries_written", 0)) + 1
            st["commits_logged"] = int(st.get("commits_logged", 0)) + _n

        pacing.update_state(_STATE_FILE, _advance, log_prefix="evolution")
        written += 1
        log.info("evolution: logged %s (%d commit(s))", day, len(day_commits))

    return written


async def _cycle() -> None:
    await run_catchup()


async def run_loop() -> None:
    # The periodic scaffold sleeps before its first cycle. Catch up immediately
    # after startup so a freshly deployed commit does not stay absent from the
    # only update record JARVIS can read for another five minutes.
    try:
        await _cycle()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("evolution: startup catch-up failed: %s", e)
    await pacing.run_periodic(
        name="evolution", check_interval_s=_CHECK_INTERVAL_S, on_cycle=_cycle)
