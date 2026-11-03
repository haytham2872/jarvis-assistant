"""backend/brain/people.py — person profiles: one template, many people.

Every profile in jarvis-vault/people/ follows ONE template (people/_template.md): the
same sections in the same order, with different content per person. The template is a
plain vault note the owner can edit in Obsidian; each `## Section` carries a hidden
comment saying which kinds of person it applies to (known / public / historical), what
belongs in it, the question JARVIS asks when interviewing the owner about someone he
knows, and what to look for when researching someone JARVIS can read about. One file
therefore drives both ways a profile gets filled.

Every section is optional. A profile shows only the sections that have something in
them, in template order; `gaps` reports which applicable ones are still empty or thin.

The profile file is machine-owned the same way positions.md and predictions.md are:
the model never rewrites it. It sends small typed edits (add / revise / remove one
entry in one section) and this module does the file surgery, so a truncated model
response cannot cut a profile in half, and the owner's own Obsidian edits survive
(sections, prose and frontmatter keys the code does not know about are kept).

Every entry carries where it came from, and the code enforces it:
    - Goes quiet rather than argue — the owner, 2026-09-26
    - Wrote the Meditations as a private notebook — [en.wikipedia.org](https://…)
    - Probably dislikes crowds — my inference, 2026-09-26
The A05 lesson: roughly half of stored mem0 facts turned out to be things JARVIS said,
stored as if the owner had. A guess about his friend must never read as his word.

The owner's own portrait, people/owner.md, is NOT a profile: consolidation rewrites it
every session in its own format. A profile of the owner, if asked for, is an ordinary
profile under their name; the portrait is never touched.

Same silent-degradation contract as the rest of the brain: the tool returns an
`Error:` string, never raises.
"""
from __future__ import annotations
from backend import localtime as _localtime
from backend import owner as _owner

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.storage import people as store
from backend.tools import vault

log = logging.getLogger(__name__)

KINDS = ("known", "public", "historical")
PROFILE_TYPE = "person-profile"
TEMPLATE_FILE = "_template.md"
REVISIONS = "Revisions"
_FOLDER = "people"
_TZ = _localtime.ZONE
_READ_CAP = 12_000
_ENTRY_MAX = 600          # one entry is a line, not an essay

DEFAULT_TEMPLATE = _owner.say("""\
---
type: person-profile-template
---
# Person profile template

Every profile in people/ follows this template: the same sections in the same order,
with different content for each person. Every section is optional. A profile shows only
the sections that have something in them, and each one gets filled whenever there is
something worth writing, from conversation or from research.

Edit this file freely. Each section has a hidden comment with:
- applies: who it is for (known, public, historical, or all)
- about: what belongs in it
- ask: the question JARVIS asks when you tell him about someone you know
- research: what he looks for when researching someone he can read about
- enough: how many entries make it "filled" (default 2)

A new section here becomes available to every profile. A section removed or renamed
here stays in profiles that already have it, after the template's own sections.

## Snapshot
<!--
applies: all
about: Who they are in two or three sentences, the version you'd give a stranger.
ask: If you had to introduce them to me in two sentences, what would you say?
research: a short overview of who they are or were, and why they matter
enough: 1
-->

## Current situation
<!--
applies: known, public
about: What their life looks like right now: work, where they live, what they're dealing with.
ask: What's going on in their life at the moment?
research: what they are doing now, their current role and recent news
-->

## Life and background
<!--
applies: all
about: Where they come from and the main stages of their life, roughly in order.
ask: Where do they come from, and what shaped them growing up?
research: origins, upbringing, education and the main stages of their life, with dates
-->

## Era and context
<!--
applies: public, historical
about: The world they lived and worked in, and what it made possible or hard -- not their own life story.
research: the historical, political, economic or industry context of their time and field, not their biography
-->

## Personality and temperament
<!--
applies: all
about: What they are like: energy, humour, moods, how they come across.
ask: How would you describe them to someone who has never met them?
research: temperament and character as described by people who knew them and by biographers
-->

## Values and beliefs
<!--
applies: all
about: What they care about, what they believe, what they won't compromise on.
ask: What do they care about most? What would they never do?
research: their convictions, philosophy, religion or politics, in their own words where possible
-->

## Interests and passions
<!--
applies: all
about: What they love doing or thinking about outside obligations.
ask: What are they passionate about? What could they talk about for hours?
research: interests, hobbies and preoccupations outside their main work
-->

## Work and ideas
<!--
applies: all
about: What they do or did, and the ideas that drive it.
ask: What do they do for a living, and how do they feel about it?
research: their work, field and central ideas, explained plainly
-->

## Major works and achievements
<!--
applies: public, historical
about: The works, decisions or achievements they are known for, with dates.
research: their major works or achievements, with dates and why each mattered
-->

## How they talk and think
<!--
applies: all
about: Their voice: favourite phrases, how they argue, how they reason, real quotes.
ask: How do they talk? Any phrases or habits of speech you'd recognise anywhere?
research: their style of speaking, writing and reasoning, with real attributed quotes
-->

## Strengths and flaws
<!--
applies: all
about: What they are genuinely good at, and where they struggle or go wrong.
ask: What are they best at? And where do they get in their own way?
research: strengths and weaknesses noted by contemporaries, critics and biographers
-->

## Habits and daily life
<!--
applies: all
about: Routines, tastes, how they spend an ordinary day.
ask: What does an ordinary day look like for them? Any habits or rituals?
research: daily routines, working habits and personal tastes
-->

## Relationships
<!--
applies: all
about: The people who matter in their life: family, partners, friends, rivals. Use [[wiki-links]].
ask: Who are the important people in their life?
research: family, partners, friendships, mentors and rivalries
-->

## Influences
<!--
applies: public, historical
about: The people, books and ideas that shaped them.
research: who and what influenced them, and how
-->

## Key moments and stories
<!--
applies: all
about: Turning points, and the stories people tell about them.
ask: Is there a story about them that says a lot about who they are?
research: turning points and well-documented anecdotes that reveal their character
-->

## With «owner»
<!--
applies: known
about: How they and «owner» know each other, and what the relationship is like now.
ask: How did you two meet, and what is the relationship like now?
-->

## Shared history
<!--
applies: known
about: Moments, trips, conversations and inside jokes «owner» and they share.
ask: What's a memory with them you come back to?
-->

## How to be with them
<!--
applies: known
about: What they appreciate, what to avoid, how they like to be supported or argued with.
ask: What do they appreciate from people, and what gets on their nerves?
-->

## Things to remember
<!--
applies: known
about: Practical details: birthday, family names, food, gift ideas, sensitive subjects.
ask: Anything practical I should remember, like their birthday or things to avoid bringing up?
-->

## Plans and aspirations
<!--
applies: known, public
about: What they are working toward, and what they hope for.
ask: What are they working toward right now? What do they hope for?
research: stated goals, upcoming projects and ambitions
-->

## Controversies and criticism
<!--
applies: public, historical
about: What they were criticised for, fairly or not, and the other side of the argument.
research: controversies, criticism and serious disputes about them, with both sides
-->

## Legacy and influence
<!--
applies: public, historical
about: What they left behind and who they shaped.
research: their lasting influence, who they shaped, and how views of them changed over time
-->

## Why they matter to «owner»
<!--
applies: public, historical
about: Why «owner» is interested in them, and what «he» takes from them.
ask: What draws you to them?
-->

## Contradictions and tensions
<!--
applies: all
about: Where they don't add up: tensions between what they say and do, or within themselves.
ask: Is there anything about them that seems contradictory?
research: tensions a source itself points out, between their ideas and their life or within their work; leave empty if none is named
-->

## Open questions
<!--
applies: all
about: What JARVIS doesn't know yet and would like to find out.
ask: What don't I know about them that I should?
research: questions the sources leave open or disagree about
-->
""")


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectionSpec:
    title: str
    applies: frozenset
    about: str = ""
    ask: str = ""
    research: str = ""
    enough: int = 2

    def applies_to(self, kind: str) -> bool:
        return kind in self.applies


_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_KEY = re.compile(r"^\s*(applies|about|ask|research|enough)\s*:\s*(.*?)\s*$", re.I)


def parse_template(text: str) -> list[SectionSpec]:
    """Section specs from a template note, in order. Pure; never raises."""
    specs: list[SectionSpec] = []
    body = _split_frontmatter(text or "")[1]
    chunks = re.split(r"(?m)^(?=##\s)", body)
    for chunk in chunks:
        lines = chunk.splitlines()
        if not lines:
            continue
        m = _HEADING.match(lines[0])
        if not m:
            continue
        fields: dict[str, str] = {}
        cm = _COMMENT.search(chunk)
        if cm:
            for line in cm.group(1).splitlines():
                km = _KEY.match(line)
                if km:
                    fields[km.group(1).lower()] = km.group(2)
        applies = {a.strip().lower() for a in fields.get("applies", "all").split(",")}
        kinds = set(KINDS) if (not applies or "all" in applies) else (applies & set(KINDS))
        try:
            enough = max(1, int(fields.get("enough", "2")))
        except ValueError:
            enough = 2
        title = m.group(1).strip()
        if title.lower() == REVISIONS.lower() or not kinds:
            continue
        specs.append(SectionSpec(title=title, applies=frozenset(kinds),
                                 about=fields.get("about", ""), ask=fields.get("ask", ""),
                                 research=fields.get("research", ""), enough=enough))
    return specs


def _people_dir() -> Path:
    return vault._VAULT / _FOLDER


# Earlier DEFAULT_TEMPLATE texts (sha256, "\n" line endings). A template file still
# byte-equal to one of them was never edited, so it is upgraded to the current default;
# an edited one is the owner's and is left alone.
_OLD_DEFAULTS = frozenset({
    "f28d1c744c50ec5153cd21f96390a538d46ca45c39f3fec3ea744a14e74d64f7",   # 2026-09-26
})


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_template() -> list[SectionSpec]:
    """The live template, seeding people/_template.md on first use.

    Written directly rather than through vault_write so it is never semantically
    indexed: its questions would otherwise surface in recall for unrelated queries.
    A template that parses to nothing falls back to the default rather than leaving
    every profile without sections.
    """
    path = _people_dir() / TEMPLATE_FILE
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
            log.info("people: seeded %s", path)
        elif _text_hash(path.read_text(encoding="utf-8")) in _OLD_DEFAULTS:
            path.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
            log.info("people: upgraded the unedited %s to the current default", path)
        specs = parse_template(path.read_text(encoding="utf-8"))
        if specs:
            return specs
        log.warning("people: %s has no usable sections; using the default template", path)
    except Exception as e:
        log.warning("people: template unreadable (%s); using the default", e)
    return parse_template(DEFAULT_TEMPLATE)


# ---------------------------------------------------------------------------
# Profile model
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    name: str
    kind: str
    relation: str = ""
    aliases: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    extra_meta: list[str] = field(default_factory=list)    # frontmatter lines we don't own
    preamble: list[str] = field(default_factory=list)      # text between title and first ##
    sections: dict[str, list[str]] = field(default_factory=dict)   # title -> body lines
    related: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)          # his own, beyond 'person'

    def section(self, title: str) -> list[str] | None:
        for t, lines in self.sections.items():
            if t.lower() == title.lower():
                return lines
        return None


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], "\n".join(lines[i + 1:])
    return [], text


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [v.strip().strip("'\"") for v in value.split(",") if v.strip().strip("'\"")]


_OWNED_META = ("type", "name", "kind", "relation", "aliases", "created", "updated", "tags")
_RELATED = re.compile(r"^\*Related:(.*)\*\s*$")
_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_BLOCK_ITEM = re.compile(r"^\s+-\s*(.*?)\s*$")


def is_profile(text: str) -> bool:
    meta, _ = _split_frontmatter(text or "")
    return any(re.match(rf"^type:\s*{PROFILE_TYPE}\s*$", m.strip()) for m in meta)


def parse_profile(text: str) -> Profile:
    """Parse a profile note. Tolerant of hand edits; never raises."""
    meta_lines, body = _split_frontmatter(text or "")
    meta: dict[str, str] = {}
    extra: list[str] = []
    i = 0
    while i < len(meta_lines):
        line = meta_lines[i]
        key, sep, value = line.partition(":")
        k = key.strip().lower()
        if sep and k in _OWNED_META and not line.startswith((" ", "\t")):
            value = value.strip()
            # Obsidian's Properties editor writes lists as YAML blocks ("aliases:" then
            # "  - Kiki"). Read them as the list they are: left as unknown lines, they
            # dropped out of the aliases and came back as broken frontmatter.
            items = []
            while not value and i + 1 < len(meta_lines) and _BLOCK_ITEM.match(meta_lines[i + 1]):
                i += 1
                items.append(_BLOCK_ITEM.match(meta_lines[i]).group(1))
            meta[k] = "[" + ", ".join(items) + "]" if items else value
        else:
            extra.append(line)
        i += 1

    lines = body.splitlines()
    # Strip the "---" + "*Related: …*" footer, keeping its links.
    related: list[str] = []
    tail = [i for i, l in enumerate(lines) if l.strip()]
    if len(tail) >= 2 and _RELATED.match(lines[tail[-1]].strip()) and lines[tail[-2]].strip() == "---":
        related = _LINK.findall(lines[tail[-1]])
        lines = lines[:tail[-2]]

    name = meta.get("name", "")
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        m = _HEADING.match(line)
        if m:
            current = sections.setdefault(m.group(1).strip(), [])
            continue
        if current is None:
            if line.startswith("# ") and not preamble and not name:
                name = line[2:].strip()
            elif not line.startswith("# "):
                preamble.append(line)
            continue
        current.append(line)

    return Profile(
        name=name, kind=_kind(meta.get("kind", "")),
        relation=meta.get("relation", ""), aliases=_parse_list(meta.get("aliases", "")),
        created=meta.get("created", ""), updated=meta.get("updated", ""),
        extra_meta=extra, preamble=_trim(preamble),
        sections={t: _trim(b) for t, b in sections.items()}, related=related,
        tags=[t for t in _parse_list(meta.get("tags", "")) if t.lower() != "person"],
    )


def _kind(value: str) -> str:
    """A hand-edited kind that isn't one we know reads as 'known' rather than hiding
    every section."""
    value = value.strip().lower()
    return value if value in KINDS else "known"


def _trim(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def render_profile(p: Profile, specs: list[SectionSpec]) -> str:
    """Render a profile: template sections in template order (only non-empty ones),
    then any other section already in the file, then Revisions. Pure."""
    out = ["---", f"type: {PROFILE_TYPE}", f"name: {p.name}", f"kind: {p.kind}"]
    if p.relation:
        out.append(f"relation: {p.relation}")
    if p.aliases:
        out.append("aliases: [" + ", ".join(p.aliases) + "]")
    out += [f"created: {p.created}", f"updated: {p.updated}",
            "tags: [" + ", ".join(dict.fromkeys(["person"] + p.tags)) + "]"]
    out += p.extra_meta
    out += ["---", f"# {p.name}", ""]
    if p.preamble:
        out += p.preamble + [""]

    template_titles = [s.title for s in specs]
    ordered = [t for t in template_titles if p.section(t)]
    known = {t.lower() for t in template_titles}
    others = [t for t in p.sections
              if t.lower() not in known and t.lower() != REVISIONS.lower() and p.sections[t]]
    for title in ordered + others:
        # Keep the title as written in the file when it already exists there.
        actual = next(t for t in p.sections if t.lower() == title.lower())
        out += [f"## {actual}"] + p.sections[actual] + [""]
    revisions = p.section(REVISIONS)
    if revisions:
        out += [f"## {REVISIONS}"] + revisions + [""]

    related = list(dict.fromkeys(([_owner.say("«Owner»")] if p.kind == "known" else []) + p.related))
    if related:
        out += ["---", "*Related: " + " · ".join(f"[[{r}]]" for r in related) + "*"]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Entries and edits
# ---------------------------------------------------------------------------

_SEP = " — "
_TYPED_SOURCE = re.compile(
    r"\s+[\u2014-]+\s*(?:" + re.escape(_owner.name()) + r"|my inference|inference)(?:,\s*\d{4}-\d{2}-\d{2})?\s*$", re.I)
_URL = re.compile(r"^https?://", re.I)


def attribution(source: str, today: str) -> str:
    """How an entry records where it came from. '' when the source is unusable."""
    s = " ".join((source or "").split()).replace(_SEP.strip(), "-")
    low = s.lower()
    if low in ("owner", "user", "him", "her", "them", _owner.name().lower()):
        return _owner.say(f"«Owner», {today}")
    if low in ("inference", "my inference", "jarvis", "me"):
        return f"my inference, {today}"
    if _URL.match(s):
        host = re.sub(r"^https?://(www\.)?", "", s, flags=re.I).split("/")[0]
        return f"[{host}]({s})"
    return s


def _entry_text(line: str) -> str:
    """The claim of a bullet line, without its attribution."""
    text = line.strip()
    if text[:2] in ("- ", "* "):
        text = text[2:]
    return text.rsplit(_SEP, 1)[0].strip() if _SEP in text else text


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


# Two wordings of one fact ("Has a girlfriend named Sofia, a pastry chef." / "Girlfriend
# is Sofia, a pastry chef.") were both stored live. Measured on 798 pairs of distinct
# entries in the same section across the real and test profiles: content-word overlap
# never above 0.30; that pair scores 1.0.
_DUPLICATE_OVERLAP = 0.6
_STOPWORDS = frozenset(
    "the a an and of to in his her their he she they was is as at for on with by from that "
    "this its it be had has have who which were are or not but into also named".split())


def _content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", norm_words(s))
            if w not in _STOPWORDS and len(w) > 2}


def _overlap(a: str, b: str) -> float:
    wa, wb = _content_words(a), _content_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def entry_count(lines: list[str] | None) -> int:
    """Non-empty content lines in a section (bullets or prose, not sub-headings)."""
    return sum(1 for l in (lines or []) if l.strip() and not l.lstrip().startswith("#"))


def _resolve_section(title: str, p: Profile, specs: list[SectionSpec]) -> tuple[str | None, str]:
    """(canonical title, error) for an edit target. Only sections that apply to this
    person's kind, or that already exist in the file, can be written."""
    want = _norm(title)
    if not want:
        return None, "no section given"
    applicable = [s.title for s in specs if s.applies_to(p.kind)]
    existing = [t for t in p.sections if t.lower() != REVISIONS.lower()]
    pool = list(dict.fromkeys(applicable + existing))
    exact = [t for t in pool if _norm(t) == want]
    if exact:
        return exact[0], ""
    partial = [t for t in pool if want in _norm(t)]
    if len(partial) == 1:
        return partial[0], ""
    if any(_norm(s.title) == want for s in specs):
        return None, f"'{title}' is not used for a {p.kind} person"
    return None, (f"no section '{title}'" if not partial else f"'{title}' is ambiguous "
                  f"({', '.join(partial)})")


def apply_edits(p: Profile, edits: list, specs: list[SectionSpec],
                today: str) -> tuple[int, list[str]]:
    """Apply typed edits in place. Returns (applied, per-edit report lines). Never raises.

    add:    append an entry; needs text and source. An identical entry is a no-op.
    revise: replace the ONE entry whose text contains `match`; needs text and source.
    remove: delete the ONE entry whose text contains `match`; recorded under Revisions.
    """
    applied = 0
    report: list[str] = []
    for i, e in enumerate(edits or [], start=1):
        if not isinstance(e, dict):
            report.append(f"edit {i}: skipped, not an object")
            continue
        action = str(e.get("action", "add")).strip().lower()
        text = " ".join(str(e.get("text", "")).split())
        if text[:2] in ("- ", "* "):
            text = text[2:]
        # " — " separates an entry from its source; one inside the text (live: "Allergic
        # to hazelnuts — the owner finds this funny") would read as a second separator.
        # Live: the model sometimes types the attribution into the text itself
        # ("First week was good. — the owner, 2026-09-27"); the source is added once, below.
        text = _TYPED_SOURCE.sub("", text)
        text = text.replace(_SEP, " - ")
        match = _norm(str(e.get("match", "")))
        source = str(e.get("source", "")).strip()
        title, err = _resolve_section(str(e.get("section", "")), p, specs)
        if err:
            report.append(f"edit {i}: skipped, {err}")
            continue
        if action not in ("add", "revise", "remove"):
            report.append(f"edit {i}: skipped, unknown action '{action}'")
            continue
        if action in ("add", "revise"):
            attr = attribution(source, today)
            if not text:
                report.append(f"edit {i}: skipped, no text")
                continue
            if not attr:
                report.append(f"edit {i}: skipped, no source (use 'owner', 'inference', "
                              "or a citation)")
                continue
            if len(text) > _ENTRY_MAX:
                report.append(f"edit {i}: skipped, entry over {_ENTRY_MAX} characters -- "
                              "split it into separate entries")
                continue
            line = f"- {text}{_SEP}{attr}"
        lines = p.sections.setdefault(title, [])
        if action == "add":
            same = next((l for l in lines if _norm(_entry_text(l)) == _norm(text)
                         or _overlap(_entry_text(l), text) >= _DUPLICATE_OVERLAP), None)
            if same is not None:
                report.append(f"edit {i}: {title}: already there"
                              + ("" if _norm(_entry_text(same)) == _norm(text)
                                 else f" (as \"{_entry_text(same)[:80]}\")"))
                continue
            lines.append(line)
            applied += 1
            report.append(f"edit {i}: {title}: added")
            continue
        if not match:
            report.append(f"edit {i}: skipped, {action} needs 'match' (part of the entry's text)")
            continue
        hits = [j for j, l in enumerate(lines) if l.strip() and match in _norm(_entry_text(l))]
        if len(hits) != 1:
            report.append(f"edit {i}: skipped, {len(hits)} entries in {title} match "
                          f"'{e.get('match')}' (need exactly one)")
            if not lines:
                p.sections.pop(title, None)
            continue
        old = _entry_text(lines[hits[0]])
        who = f" ({attribution(source, today).split(',')[0]})" if source else ""
        if action == "revise":
            lines[hits[0]] = line
            note = f'- {today}{_SEP}{title}: revised "{old}" to "{text}"{who}'
        else:
            del lines[hits[0]]
            note = f'- {today}{_SEP}{title}: removed "{old}"{who}'
        if not entry_count(lines):
            p.sections.pop(title, None)
        p.sections.setdefault(REVISIONS, []).append(note)
        applied += 1
        report.append(f"edit {i}: {title}: {action}d")
    return applied, report


def gaps(p: Profile, specs: list[SectionSpec]) -> tuple[list, list, list]:
    """(filled, thin, empty) applicable specs, each in template order. Pure."""
    filled, thin, empty = [], [], []
    for s in specs:
        if not s.applies_to(p.kind):
            continue
        n = entry_count(p.section(s.title))
        (filled if n >= s.enough else thin if n else empty).append(s)
    return filled, thin, empty


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(_TZ).date().isoformat()


def file_stem(name: str) -> str:
    """PascalCase file stem: 'Jean-Paul Sartre' -> 'JeanPaulSartre'."""
    words = re.findall(r"\w+", name or "", flags=re.UNICODE)
    return "".join(w[:1].upper() + w[1:] for w in words if w != "_")


def _profile_files() -> list[tuple[Path, str]]:
    d = _people_dir()
    out = []
    try:
        for path in sorted(d.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if is_profile(text):
                out.append((path, text))
    except Exception:
        pass
    return out


def find_profile(name: str) -> tuple[Path, str] | None:
    """The profile for a name, matched on name, alias, or file stem. Case-insensitive."""
    want = _norm(name)
    stem = file_stem(name).lower()
    if not want:
        return None
    for path, text in _profile_files():
        p = parse_profile(text)
        if (want == _norm(p.name) or want in (_norm(a) for a in p.aliases)
                or path.stem.lower() == stem):
            return path, text
    return None


def _new_path(name: str) -> Path | None:
    """Where a new profile goes. A file that exists but isn't a profile -- owner.md,
    the portrait -- is never touched: the profile gets <Stem>Profile.md instead."""
    stem = file_stem(name)
    if not stem:
        return None
    d = _people_dir()
    taken = {p.name.lower() for p in d.glob("*.md")} if d.exists() else set()
    for candidate in (stem, f"{stem}Profile"):
        if f"{candidate}.md".lower() not in taken:
            return d / f"{candidate}.md"
    return None


def _revision(raw: bytes) -> str:
    # Same marker vault.note_revision computes, taken from the bytes we actually parsed.
    return hashlib.sha256(raw).hexdigest()


async def _write(path: Path, content: str, revision: str) -> str:
    return await vault.vault_write(path.name, content, _FOLDER, expected_revision=revision)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

def _section_names(p: Profile, specs: list[SectionSpec]) -> str:
    return (f" Sections for a {p.kind} person: "
            + ", ".join(s.title for s in specs if s.applies_to(p.kind)) + ".")


def _missed_section(report: list[str]) -> bool:
    return any(k in r for r in report for k in ("no section", "is ambiguous", "is not used for"))


def _describe(p: Profile, specs: list[SectionSpec]) -> str:
    filled, thin, empty = gaps(p, specs)
    total = len(filled) + len(thin) + len(empty)
    rel = f", {p.relation}" if p.relation else ""
    return f"{p.name} ({p.kind}{rel}): {len(filled)} of {total} sections filled, {len(thin)} thin"


def _gaps_text(p: Profile, specs: list[SectionSpec]) -> str:
    filled, thin, empty = gaps(p, specs)
    lines = [_describe(p, specs) + "."]
    hint = "ask" if p.kind == "known" else "research"
    for label, group in (("Empty", empty), ("Thin", thin)):
        for s in group:
            prompt = getattr(s, hint) or s.about
            lines.append(f"{label}: {s.title} -- {hint}: {prompt}")
    if filled:
        lines.append("Filled: " + ", ".join(s.title for s in filled))
    if p.kind == "known":
        lines.append(_owner.say("Every section is optional. When «he» is talking about this person, ask at "
                     "most one of these questions per reply, in your own words, and drop it "
                     "if «he» changes the subject."))
    return "\n".join(lines)


async def person_profile(action: str, name: str = "", kind: str = "", relation: str = "",
                         aliases: list | None = None, edits: list | None = None,
                         section: str = "") -> str:
    """Create, read, update, list, decline, fill (research), or find the gaps in profiles."""
    try:
        action = (action or "").strip().lower()
        specs = load_template()
        today = _today()

        if action == "list":
            rows = []
            for path, text in _profile_files():
                p = parse_profile(text)
                rows.append((p.updated, f"- {_describe(p, specs)} -- people/{path.name}"))
            no = store.declined()
            tail = ("\nHe asked you not to profile: " + ", ".join(no) + ".") if no else ""
            if not rows:
                return "No person profiles yet." + tail
            return ("Person profiles:\n" + "\n".join(r for _, r in sorted(rows, reverse=True))
                    + tail)

        if not name.strip():
            return f"Error: person_profile '{action}' needs a name."

        if action == "decline":
            if not store.decline(name):
                return "Error: could not record that -- the store is unavailable."
            return (_owner.say(f"Confirmed: you will not offer a profile for {' '.join(name.split())} "
                    "again. If «he» asks for one later, 'create' still works."))

        found = find_profile(name)

        if action == "fill":
            # Research is for people he can read about, never for someone he knows: that
            # profile is filled from what he says, not from the internet.
            if found:
                if parse_profile(found[1]).kind == "known":
                    return (_owner.say("Error: 'fill' researches public or historical figures only. "
                            f"{name} is someone «owner» knows -- fill it from what «he» tells you."))
            else:
                kind = (kind or "").strip().lower()
                if kind not in ("public", "historical"):
                    return (f"Error: no profile for '{name}' yet. To research one, pass kind "
                            "'public' (a living public figure) or 'historical'.")
                created = await person_profile("create", name=name, kind=kind,
                                               relation=relation, aliases=aliases)
                if not created.startswith("Confirmed"):
                    return created
            from backend.brain import people_research
            return people_research.start(name)

        if action == "create":
            if found:
                return (f"Error: a profile for {name} already exists at "
                        f"people/{found[0].name} -- use 'update'.")
            kind = (kind or "").strip().lower()
            if kind not in KINDS:
                return (_owner.say("Error: 'create' needs kind: 'known' (someone «owner» knows), "
                        "'public' (a living public figure) or 'historical'."))
            path = _new_path(name)
            if path is None:
                return f"Error: cannot make a file name from '{name}'."
            p = Profile(name=" ".join(name.split()), kind=kind,
                        relation=" ".join((relation or "").split()),
                        aliases=[a.strip() for a in (aliases or []) if str(a).strip()],
                        created=today, updated=today)
            applied, report = apply_edits(p, edits or [], specs, today)
            result = await _write(path, render_profile(p, specs), "")
            if not result.startswith("Confirmed"):
                return result
            store.clear_decline(name)          # he asked for it after all
            tail = f" Edits: {'; '.join(report)}." if report else ""
            # The model does not know the template's section names until it is told: live,
            # it guessed "Who they are" and "Profile", and a fact he had stated was lost.
            return f"{result} New profile: {_describe(p, specs)}.{tail}{_section_names(p, specs)}"

        if not found:
            return (f"Error: no profile for '{name}'. Use action='list' to see existing ones, "
                    "or 'create' to start one.")

        if action == "read":
            path, text = found
            p = parse_profile(text)
            if section.strip():
                title, err = _resolve_section(section, p, specs)
                lines = p.section(title) if title else None
                if not lines:
                    present = [t for t in p.sections if p.sections[t]]
                    return (f"{p.name} has nothing under '{section}'"
                            f"{' (' + err + ')' if err else ''}. Sections with content: "
                            f"{', '.join(present) or 'none yet'}.")
                return f"{p.name} -- {title}:\n" + "\n".join(lines)
            return text[:_READ_CAP] + ("\n[...truncated]" if len(text) > _READ_CAP else "")

        if action == "gaps":
            return _gaps_text(parse_profile(found[1]), specs)

        if action == "update":
            if not edits and not relation and aliases is None:
                return "Error: 'update' needs edits (or a new relation/aliases)."
            path = found[0]
            for attempt in range(2):
                raw = path.read_bytes()
                p = parse_profile(raw.decode("utf-8"))
                applied, report = apply_edits(p, edits or [], specs, today)
                meta_changed = False
                if relation.strip() and relation.strip() != p.relation:
                    p.relation, meta_changed = " ".join(relation.split()), True
                if aliases is not None:
                    new = [a.strip() for a in aliases if str(a).strip()]
                    merged = list(dict.fromkeys(p.aliases + new))
                    if merged != p.aliases:
                        p.aliases, meta_changed = merged, True
                names = _section_names(p, specs) if _missed_section(report) else ""
                if not applied and not meta_changed:
                    return ("Error: nothing changed -- " + "; ".join(report or ["no edits"])
                            + "." + names)
                p.updated = today
                result = await _write(path, render_profile(p, specs), _revision(raw))
                if result.startswith("Confirmed"):
                    return (f"{result} {_describe(p, specs)}. Edits: "
                            f"{'; '.join(report) or 'none'}.{names}")
                if "changed since you read it" in result and attempt == 0:
                    continue          # an Obsidian edit landed meanwhile: reapply on fresh text
                return result
        return f"Error: unknown person_profile action '{action}'."
    except Exception as e:
        log.warning("people: %s failed (%s)", action, e)
        return f"Error: person_profile failed: {e}"


# ---------------------------------------------------------------------------
# Conversation: who is being talked about, and what to ask
# ---------------------------------------------------------------------------

# Chars of profile content per person per turn (~800 tokens). Measured on the first real
# profiles: at 1,500, Karim was already cut at 8 entries, and the sections cut first are
# the practical ones at the end of the template (How to be with them, Things to remember).
# With sources shortened to who said it, his is 1,548 and a researched figure 4,800-7,200.
_BLOCK_CAP = 3000
_MAX_PEOPLE_PER_TURN = 2
_QUESTION_GAP_S = 600        # at most one suggested question per person per 10 min
_SECTION_REPEAT_S = 3 * 86400  # the same question is not suggested again for 3 days
# The "offer a profile" nudge. Live, a channel-wide 30-minute limit meant the aunt who
# came up 8 messages into a family conversation could never be offered. Now: a person
# named in the message who was not in an offer in the last 30 minutes can be offered;
# a message naming nobody new waits out the 30 minutes; never more than 2 offers per
# channel in a conversation-length window.
_OFFER_GAP_S = 1800
_OFFER_WINDOW_S = 2 * 3600
_OFFERS_PER_WINDOW = 2
_last_offer: dict[str, list[tuple[float, frozenset]]] = {}     # channel -> (when, names)
def names_in(text: str) -> frozenset:
    """The people a message introduces by relation: "my aunt Nora", "my brother Adam".
    Only these count as someone new for an offer. Measured live: any capitalised word was
    too loose -- "Adam is moving to Berlin" made "Berlin" a new person and spent the
    two-offer cap before the aunt came up."""
    return frozenset(m.group(1)[:-2] if m.group(1).endswith("'s") else m.group(1)
                     for m in _NAMED_RELATION.finditer(text or ""))


def _may_offer(channel: str, names: frozenset, now: float) -> bool:
    history = [(t, n) for t, n in _last_offer.get(channel, []) if now - t < _OFFER_WINDOW_S]
    _last_offer[channel] = history
    if len(history) >= _OFFERS_PER_WINDOW:
        return False
    recent = [(t, n) for t, n in history if now - t < _OFFER_GAP_S]
    if not recent:
        return True
    already = frozenset().union(*(n for _, n in recent))
    return bool(names - already)

# People the owner knows, named by their relation to him. The fallback for noticing a
# new person when Jev is unavailable; Jev's `known_person` answer is the primary signal.
_RELATION_WORDS = (
    "friend|best friend|girlfriend|boyfriend|partner|wife|husband|fiancée?|ex|"
    "brother|sister|mom|mum|mother|dad|father|parents?|son|daughter|cousin|uncle|aunt|"
    "grandma|grandmother|grandpa|grandfather|nephew|niece|family|"
    "colleague|coworker|co-worker|boss|manager|teammate|intern|client|"
    "neighbou?r|roommate|flatmate|housemate|mentor|teacher|professor|tutor|coach|doctor|"
    "therapist|landlord")
_RELATION_RE = re.compile(rf"\bmy (?:(?:old|best|close|new|former) )?(?:{_RELATION_WORDS})s?\b",
                          re.I)


_NAMED_RELATION = re.compile(
    rf"\bmy (?:(?:old|best|close|new|former) )?(?:{_RELATION_WORDS})s?,? (?:named |called )?"
    r"([A-Z][\w'-]+)")


def is_known_person_mention(text: str) -> bool:
    """Keyword fallback: does the message talk about someone by their relation to him?"""
    return bool(_RELATION_RE.search(text or ""))


# A name inside a file name or an address is not a mention. Measured on 336 real
# messages: the only two false loads were the owner's portrait file name and email address.
_BEFORE = r"(?<!\w)"          # "@Karim" is still a mention
_AFTER = r"(?![\w@]|\.\w)"


def _name_patterns(profiles: list[tuple[Path, Profile]]) -> list[tuple[re.Pattern, int]]:
    """(pattern, profile index) pairs. Full names and aliases of 3+ characters match
    case-insensitively; shorter aliases and a bare first name only as written, so
    "will" or "rose" in a sentence is not taken for Will or Rose. A bare first name counts
    only for someone the owner knows -- "Marcus Rashford scored" is not Marcus Aurelius --
    and a first name shared by two of them is ambiguous and left out."""
    firsts: dict[str, list[int]] = {}
    for i, (_, p) in enumerate(profiles):
        parts = p.name.split()
        if p.kind == "known" and len(parts) > 1 and len(parts[0]) >= 3:
            firsts.setdefault(parts[0], []).append(i)
    out = []
    for i, (_, p) in enumerate(profiles):
        for label in [p.name] + p.aliases:
            label = " ".join(label.split())
            if not label:
                continue
            flags = re.I if len(label) >= 3 else 0
            out.append((re.compile(_BEFORE + re.escape(label) + _AFTER, flags), i))
    for first, idx in firsts.items():
        if len(idx) == 1:
            out.append((re.compile(_BEFORE + re.escape(first) + _AFTER), idx[0]))
    return out


def mentioned_profiles(text: str) -> list[tuple[Path, Profile]]:
    """Profiles named in `text`, in order of first mention, at most two."""
    if not (text or "").strip():
        return []
    profiles = [(path, parse_profile(t)) for path, t in _profile_files()]
    return match_profiles(text, profiles, limit=_MAX_PEOPLE_PER_TURN)


def match_profiles(text: str, profiles: list[tuple[Path, Profile]], *,
                   limit: int | None = None) -> list[tuple[Path, Profile]]:
    """The same name rules over already-read profiles, also used by the phone's map.

    Conversation loading keeps its two-person cap; a map can keep every valid mention.
    """
    first_pos: dict[int, int] = {}
    for pattern, i in _name_patterns(profiles):
        m = pattern.search(text)
        if m and (i not in first_pos or m.start() < first_pos[i]):
            first_pos[i] = m.start()
    ordered = sorted(first_pos, key=first_pos.get)
    return [profiles[i] for i in (ordered if limit is None else ordered[:limit])]


_MD_LINK = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")
_DATED = re.compile(r"(— (?:" + re.escape(_owner.name()) + r"|my inference)), \d{4}-\d{2}-\d{2}\s*$")


def _compact(entry: str) -> str:
    """An entry as the model needs it in a turn: who said it, not the full URL or date.
    Measured: in a researched profile the links were about a third of the block."""
    return _DATED.sub(r"\1", _MD_LINK.sub(r"\1", entry))


def _content_digest(p: Profile, specs: list[SectionSpec], cap: int = _BLOCK_CAP) -> str:
    """Filled sections in template order, one line per entry, capped."""
    lines: list[str] = []
    order = [s.title for s in specs] + [t for t in p.sections
                                        if t.lower() not in {s.title.lower() for s in specs}
                                        and t.lower() != REVISIONS.lower()]
    for title in order:
        body = p.section(title)
        if not body:
            continue
        entries = [_compact(l.strip()[2:] if l.strip()[:2] in ("- ", "* ") else l.strip())
                   for l in body if l.strip() and not l.lstrip().startswith("#")]
        lines.append(f"{title}: " + " | ".join(entries))
    text = "\n".join(lines)
    if len(text) > cap:
        text = text[:cap].rsplit("\n", 1)[0] + "\n[...more -- person_profile(action='read')]"
    return text


def next_question(p: Profile, specs: list[SectionSpec], now: float | None = None):
    """The gap question to suggest for this person now, or None. Empty sections before
    thin ones, template order within each; for someone the owner does not know, only
    sections without a research brief; a section suggested in the last three days is
    skipped, and nothing is suggested within ten minutes of the last suggestion."""
    now = now or time.time()
    seen = store.suggestions(p.name)
    if seen and now - max(seen.values()) < _QUESTION_GAP_S:
        return None
    _, thin, empty = gaps(p, specs)
    for s in empty + thin:
        # About a public or historical figure, only ask the owner what only he can answer
        # (why they matter to him); everything with a research brief is found, not asked.
        if p.kind != "known" and s.research:
            continue
        last = seen.get(s.title)
        if s.ask and (last is None or now - last >= _SECTION_REPEAT_S):
            return s
    return None


# Measured live (2026-09-26): 12 messages carrying a lasting fact about a profiled
# friend, 6 carrying passing logistics. The earlier "if he tells you something new,
# record it" recorded 1 of 12; this wording 3 of 12, and 0 of 6 logistics either way.
_RECORD_NUDGE = (_owner.say("If «he» just told you something new about them that will still matter in a "
                 "month, record it in this reply with person_profile(action='update'), one "
                 "entry per fact, source 'owner'; skip passing logistics. Your own "
                 "reading of it is a separate entry, source 'inference'."))


def _person_block(path: Path, p: Profile, specs: list[SectionSpec], now: float) -> str:
    rel = f", {p.relation}" if p.relation else ""
    head = (f"[Person profile: {p.name} ({p.kind}{rel}) -- people/{path.name}. What you know "
            "about them, each entry with its source. Use it naturally; don't recite it.]")
    body = _content_digest(p, specs) or "(Nothing recorded yet.)"
    parts = [head, body]
    q = next_question(p, specs, now)
    if q is not None:
        store.record_suggestion(p.name, q.title, now)
        parts.append(
            _owner.say(f"[A gap worth filling, only if the moment is right: {q.title} -- e.g. \"{q.ask}\" "
            "Ask at most one question about this person in this reply, in your own words, as "
            "part of the conversation, not an interview; skip it if «he» is busy, upset, or in "
            "the middle of something else. ") + _RECORD_NUDGE + "]")
    elif p.kind == "known":
        parts.append("[" + _RECORD_NUDGE + "]")
    return "\n".join(parts)


def _offer_block() -> str:
    no = store.declined()
    avoid = (_owner.say(" «He» has asked you not to profile: ") + ", ".join(no) + ".") if no else ""
    # Live: "honestly I think he'll stay in Canada" -- about Samir, who has a profile, but
    # named only by "he", so no profile loads and the message still reads as someone he
    # knows. Without the list, the offer could be for a person already profiled.
    have = [p.name for _, p in ((path, parse_profile(t)) for path, t in _profile_files())
            if p.kind == "known"]
    has = (" Already profiled (not them): " + ", ".join(have) + ".") if have else ""
    return (
        _owner.say("[«owner» seems to be talking about someone «he» knows who has no profile yet. If this "
        "person clearly matters to «him» -- not a passing mention -- and you have not already "
        "offered for them in this conversation, offer once, briefly, at the end of your "
        "reply, to start a profile on them. Not if «he» has said in this conversation that «he» "
        "doesn't want profiles right now. Write nothing about them anywhere until «he» says "
        "yes.") + avoid + has + _owner.say(" If «he» says yes, call "
        "person_profile(action='create', kind='known') with what «he» has already told you, "
        "source 'owner'. If «he» says no, call person_profile(action='decline') with their "
        "name so you never offer again.]"))


_CARRY_MESSAGES = 2             # his previous messages a "he"/"she" can point back to


def turn_block(text: str, talking_about_someone: bool, channel: str = "local",
               now: float | None = None, recent: list[str] | None = None) -> str:
    """The people context for one turn: profiles of anyone named, else (when the owner is
    talking about someone he knows) the nudge to offer a profile. Never raises.

    `recent` is his previous messages in this conversation, oldest first. Live: "honestly
    I think he'll end up staying in Canada" was about Samir, named one message earlier;
    no profile loaded, and the turn still read as someone he knows, so it could have
    drawn an offer for a person already profiled. When nobody is named but he is talking
    about someone, the people named in his last two messages are carried over."""
    now = now or time.time()
    try:
        found = mentioned_profiles(text)
        if not found and talking_about_someone and recent:
            for earlier in reversed(recent[-_CARRY_MESSAGES:]):
                found = mentioned_profiles(earlier)
                if found:
                    break
        if found:
            specs = load_template()
            return "\n\n".join(_person_block(path, p, specs, now) for path, p in found)
        if talking_about_someone:
            skip = {p.name.split()[0] for _, p in ((pa, parse_profile(t)) for pa, t in _profile_files())}
            skip |= {n.split()[0] for n in store.declined() if n.split()}
            names = frozenset(n for n in names_in(text) if n not in skip)
            if _may_offer(channel, names, now):
                _last_offer.setdefault(channel, []).append((now, names))
                return _offer_block()
    except Exception as e:
        log.warning("people: turn block skipped (%s)", e)
    return ""


# ---------------------------------------------------------------------------
# Session end: what the owner said about people, filed from the record
# ---------------------------------------------------------------------------
# Measured live (2026-09-26): asked to record lasting facts about a profiled friend in
# the moment, the model did so for 3 of 12. Consolidation already reads the whole session
# at its end, so it proposes the edits there, and code keeps an entry only when its quote
# is the owner's own words from that session -- the research quote rule, applied to him.
# That is what makes "source: the owner" provable rather than asserted.

_SESSION_PEOPLE = 4              # profiles offered to consolidation per session
_SESSION_DIGEST_CAP = 1200       # what is already recorded, per person, in that prompt
_PATCH_QUOTE_MIN = 12


def norm_words(s: str) -> str:
    """Words only, in order: case, punctuation and spacing are ignored, so "isn't" matches
    "isnt" and a table row "1999 | GPU" matches "1999 GPU". The words themselves and their
    order still have to match. Shared by research and the session patch."""
    s = (s or "").lower().replace("'", "").replace("‘", "").replace("’", "")
    return re.sub(r"[\W_]+", " ", s).strip()


def session_people(user_texts: list[str]) -> list[tuple[Path, Profile]]:
    """Profiles named anywhere in the session's messages from the owner, first mention first."""
    seen: dict[str, tuple[Path, Profile]] = {}
    for text in user_texts:
        for path, p in mentioned_profiles(text):
            seen.setdefault(str(path), (path, p))
        if len(seen) >= _SESSION_PEOPLE:
            break
    return list(seen.values())[:_SESSION_PEOPLE]


def session_block(people_: list[tuple[Path, Profile]]) -> str:
    """The consolidation prompt's view of who was discussed: name, sections, what's known."""
    if not people_:
        return ""
    specs = load_template()
    out = []
    for _, p in people_:
        rel = f", {p.relation}" if p.relation else ""
        # Titles alone placed one fact (a food allergy) in four different sections over
        # four runs, never "Things to remember"; each title now carries what belongs there.
        sections = "\n".join(f"    {s.title}: {s.about}" for s in specs if s.applies_to(p.kind))
        known = _content_digest(p, specs, _SESSION_DIGEST_CAP) or "(nothing yet)"
        out.append(f"- {p.name} ({p.kind}{rel}). Sections:\n{sections}\n"
                   f"  Already recorded:\n  " + known.replace("\n", "\n  "))
    return "\n".join(out)


def verify_session_patch(entries, allowed: list[str], user_texts: list[str]):
    """{name: [edit]} for entries whose quote is the owner's own words from this session,
    and the number rejected. Pure. The source is always 'owner': anything else he did
    not say is not for this path."""
    names = {n.lower(): n for n in allowed}
    said = [norm_words(t) for t in user_texts if t]
    by_person: dict[str, list[dict]] = {}
    rejected = 0
    for e in entries or []:
        if not isinstance(e, dict):
            rejected += 1
            continue
        name = names.get(str(e.get("name", "")).strip().lower())
        quote = norm_words(str(e.get("quote", "")))
        text = " ".join(str(e.get("text", "")).split())
        if (name is None or not text or len(quote) < _PATCH_QUOTE_MIN
                or not any(quote in s for s in said)):
            rejected += 1
            continue
        by_person.setdefault(name, []).append(
            {"section": str(e.get("section", "")), "action": "add", "text": text,
             "source": "owner"})
    return by_person, rejected


async def apply_session_patch(entries, allowed: list[str], user_texts: list[str]) -> tuple[bool, str]:
    """Apply a consolidation people_patch. (ok, detail). ok is False only when a write
    failed and a retry could land it; a patch with nothing usable is done, not failed."""
    by_person, rejected = verify_session_patch(entries, allowed, user_texts)
    added, notes, failed = 0, [], False
    for name, edits in by_person.items():
        result = await person_profile("update", name=name, edits=edits)
        if result.startswith("Confirmed"):
            n = len(re.findall(r": added\b", result))
            added += n
            notes.append(f"{name}: {n} added")
        elif "nothing changed" in result:
            notes.append(f"{name}: already there")
        else:
            failed = True
            notes.append(f"{name}: {result[:160]}")
    detail = (_owner.say(f"{added} entries added, {rejected} rejected (no quote of «theirs»)")
              + (f"; {'; '.join(notes)}" if notes else ""))
    return not failed, detail
