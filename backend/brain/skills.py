"""backend/brain/skills.py — procedures JARVIS follows for tasks that come back.

A skill is a Markdown file in jarvis-vault/skills/<name>.md: a name, a one-line
description, then When to use / Procedure / Pitfalls / Checks. The owner can read and edit
it in Obsidian.

How a skill reaches a turn changed on 4 October 2026. It used to be picked by cosine
similarity and pasted into the prompt of every complex turn above a 0.68 floor. Measured
with the real embedder, that floor let everything through: 30 of 30 unrelated messages
scored 0.726-0.837 against three sample skills, the relevant ones 0.809-0.911, so no
threshold could separate them. On the server the three skills had 216 recorded "uses";
replayed, "what's on my calendar tomorrow" got the creative-writing skill and "plan my
week" got stock research. Now, as in the open Agent Skills standard:

    catalog_text()        every active skill's name and description, in the cached part
                          of the prompt (it changes only when a skill is saved or retired)
    skill(action="view")  JARVIS opens the full procedure when a request matches one
    forced(user_text)     the owner runs one himself: "/skill stock-research TTWO"

Each opening is recorded (backend/storage/skills.py), which is what /skills reports.
Every change keeps the previous text as a revision, so it can be taken back (undo).

Four ways in (slice 3): the owner describes it (save, source "taught"); "remember how you did
that" (learn from the last task's real record); "do it like last Tuesday" (learn from a past
day's record); a document he sent (learn from an attachments/ note). skill_learning.py builds
the last three from records, never from memory. A skill is used as soon as it is saved;
on Discord the reply then ends with a line naming it and how to take it back.

A skill is only instructions: it unlocks nothing. Emails, trades and calendar changes go
through the same permission checks whatever a skill says, and a skill's text never counts
as the owner's words.
"""
from __future__ import annotations
from backend import owner as _owner

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from contextvars import ContextVar

from backend.storage import lifelog
from backend.storage import skills as store
from backend.tools import vault as _vault
from backend.tasks import spawn

log = logging.getLogger(__name__)

ACTIVE = "active"
RETIRED = "retired"
SOURCES = ("learned", "taught", "document", "migrated")

MAX_ACTIVE = 40          # a focused library: the 2026 survey finds flat lists degrade past ~64
MAX_DESCRIPTION = 300    # Agent Skills allows 1,024; this one rides in every prompt
CATALOG_LINE = MAX_DESCRIPTION   # a hand-edited longer one is cut in the list, not refused
MAX_BODY = 12_000        # ~3k tokens; the standard recommends under 5k
MAX_STEPS = 20
MAX_NAME = 64

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")      # the Agent Skills naming rule
# Frontmatter keys of the June 2026 format. Ignored when read, dropped when rewritten:
# the counters were meaningless (see the module docstring) and now live in SQLite.
_LEGACY_KEYS = frozenset({"category", "tags", "requires_tools", "confidence", "version",
                          "uses", "successes", "success_rate", "last_used"})
_OWN_KEYS = ("name", "description", "status", "source", "tools", "created", "updated")

CATALOG_FRAME = (
    _owner.say("[Your skills -- procedures you keep for tasks that come back. When what «owner» asks "
    "matches one, open it with skill(action=\"view\", name=...) before your first other "
    "tool call, even if you think you know how, and follow it, adapting it to the case: it "
    "holds how «he» wants it done. Don't open one that doesn't fit the request, and don't "
    "mention the file. «He» can run one «himself» with /skill <name>.]")
)


def skills_dir() -> Path:
    """Resolved at call time, so a test that points the vault elsewhere is honoured."""
    return _vault._VAULT / "skills"


# --------------------------------------------------------------------------- the file
@dataclass
class Skill:
    name: str
    description: str
    body: str
    status: str = ACTIVE
    source: str = "learned"
    tools: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    extra: dict[str, str] = field(default_factory=dict)   # keys added by hand, kept as written

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    def steps(self) -> int:
        section = _section(self.body, ("procedure", "steps"))
        return sum(1 for line in section.splitlines() if re.match(r"\s*\d+[.)]\s+\S", line))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
        if value and '\\"' in value:
            value = value.replace('\\"', '"')
    return value


def _as_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [_unquote(v) for v in value.split(",") if _unquote(v)]


def parse(text: str, fallback_name: str = "") -> Skill:
    """A skill file as a Skill. Never raises: a file without frontmatter is a skill whose
    description is its first line of text."""
    lines = (text or "").splitlines()
    meta: dict[str, str] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            key, sep, value = lines[i].partition(":")
            if sep and key.strip() and not key.startswith((" ", "\t")):
                meta[key.strip()] = value.strip()
        else:
            meta, body_start = {}, 0           # an unclosed block is text, not frontmatter
    body = "\n".join(lines[body_start:]).strip()
    description = _unquote(meta.get("description", ""))
    if not description:
        first = next((l.strip() for l in body.splitlines()
                      if l.strip() and not l.startswith("#")), "")
        description = first[:CATALOG_LINE]
    tools = _as_list(meta.get("tools", "") or meta.get("requires_tools", ""))
    extra = {k: v for k, v in meta.items() if k not in _OWN_KEYS and k not in _LEGACY_KEYS}
    return Skill(
        name=_unquote(meta.get("name", "")) or fallback_name,
        description=" ".join(description.split()),
        body=body,
        status=(_unquote(meta.get("status", ACTIVE)) or ACTIVE).lower(),
        source=_unquote(meta.get("source", "learned")) or "learned",
        tools=tools,
        created=_unquote(meta.get("created", "")),
        updated=_unquote(meta.get("updated", "")),
        extra=extra,
    )


def _yaml_value(value: str) -> str:
    """A flat YAML string, quoted when Obsidian would otherwise misread it."""
    if not value or re.search(r":\s|\s#|^[\[{\"'!&*>|%@`-]", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def render(skill: Skill) -> str:
    lines = ["---", f"name: {skill.name}", f"description: {_yaml_value(skill.description)}",
             f"status: {skill.status}", f"source: {skill.source}",
             f"tools: [{', '.join(skill.tools)}]"]
    if skill.created:
        lines.append(f"created: {skill.created}")
    if skill.updated:
        lines.append(f"updated: {skill.updated}")
    lines += [f"{k}: {v}" for k, v in skill.extra.items()]
    lines += ["---", "", skill.body.strip(), ""]
    return "\n".join(lines)


def _section(body: str, titles: tuple[str, ...]) -> str:
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() in titles
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


_NUMBERING = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def build_body(when_to_use: str, steps: list[str], pitfalls: list[str] | None = None,
               checks: list[str] | None = None) -> str:
    """When to use / Procedure / Pitfalls / Checks, from the tool's arguments."""
    def items(values):
        return [_NUMBERING.sub("", " ".join(str(v).split())) for v in (values or [])
                if str(v).strip()]
    parts = []
    if (when_to_use or "").strip():
        parts.append("## When to use\n" + " ".join(when_to_use.split()))
    parts.append("## Procedure\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(items(steps), 1)))
    if items(pitfalls):
        parts.append("## Pitfalls\n" + "\n".join(f"- {p}" for p in items(pitfalls)))
    if items(checks):
        parts.append("## Checks\n" + "\n".join(f"- {c}" for c in items(checks)))
    return "\n\n".join(parts)


def _files() -> list[Path]:
    d = skills_dir()
    try:
        return sorted(p for p in d.iterdir()
                      if p.is_file() and p.suffix == ".md" and not p.name.startswith(("_", ".")))
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("skills: could not list %s (%s)", d, exc)
        return []


def load(name: str) -> Skill | None:
    path = skills_dir() / f"{name}.md"
    try:
        return parse(path.read_text(encoding="utf-8"), path.stem)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("skills: could not read %s (%s)", path, exc)
        return None


def load_all() -> list[Skill]:
    out = []
    for path in _files():
        try:
            out.append(parse(path.read_text(encoding="utf-8"), path.stem))
        except Exception as exc:
            log.warning("skills: could not read %s (%s)", path, exc)
    return out


def active_skills() -> list[Skill]:
    return [s for s in load_all() if s.active and s.description]


# --------------------------------------------------------------------------- the prompt
_catalog_cache: tuple[tuple, str] | None = None


def catalog_text() -> str:
    """The list of skills for the cached part of the prompt, or "" when there are none.

    Name and description only, sorted, no usage figures: it must stay byte-identical from
    turn to turn so the cached prefix holds, changing only when a skill file does."""
    global _catalog_cache
    try:
        key = (str(skills_dir()),
               tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in _files()))
        if _catalog_cache is not None and _catalog_cache[0] == key:
            return _catalog_cache[1]
        lines = []
        for s in sorted(active_skills(), key=lambda s: s.name)[:MAX_ACTIVE]:
            desc = s.description if len(s.description) <= CATALOG_LINE \
                else s.description[:CATALOG_LINE - 1].rstrip() + "…"
            lines.append(f"- {s.name}: {desc}")
        text = f"{CATALOG_FRAME}\n" + "\n".join(lines) if lines else ""
        _catalog_cache = (key, text)
        return text
    except Exception as exc:
        log.warning("skills: catalog unavailable (%s)", exc)
        return ""


def reminder_line() -> str:
    """One line for this turn's context: the names only, next to his message.

    The list in the cached prompt alone was not enough. Measured live on 4 October,
    five research phrasings x 2 trials: with the list only, stock-research was opened
    for 5 of 10 ("should I worry", "dig into" and "is my thesis still good" were missed);
    with this line too, 10 of 10, each as the first tool call, and 0 of 12 unrelated
    messages (prices, calendar, "good night", "plan my week", a LinkedIn post) opened one.
    The same pattern standing rules showed in September: a rule in the system prompt was
    ignored, the same rule next to his message followed."""
    try:
        names = [s.name for s in sorted(active_skills(), key=lambda s: s.name)][:MAX_ACTIVE]
    except Exception:
        return ""
    if not names:
        return ""
    return (f"[Skills you keep: {', '.join(names)}. If this message asks for one of those tasks, "
            "open that skill before anything else.]")


# --------------------------------------------------------------------------- helpers
def _today() -> str:
    return lifelog.local_date()


def _closest(name: str, skills: list[Skill]) -> str:
    names = [s.name for s in skills]
    near = difflib.get_close_matches(name, names, n=3, cutoff=0.4)
    listing = ", ".join(near or names[:10])
    return f" Your skills: {listing}." if listing else " You have no skills yet."


def slug(name: str) -> str:
    """'Meeting Prep' -> 'meeting-prep'. Raises ValueError when nothing usable is left."""
    s = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")[:MAX_NAME].strip("-")
    if not s or not _NAME.match(s):
        raise ValueError("a skill needs a name made of letters, digits and hyphens")
    return s


def _live_caller() -> bool:
    """True in the live turn (or carrying out something he approved). A background job, a
    night task or a routine runs with nobody present: it may open skills, not change them.
    Anything that does not say who it is counts as unattended (policy.current_caller)."""
    from backend.tools import policy
    return policy.current_caller.get() in (policy.LIVE, policy.APPROVED)


def _known_tools() -> set[str]:
    from backend.tools.registry import TOOLS
    return {t["name"] for t in TOOLS}


def _run_and_channel() -> tuple[str | None, str]:
    try:
        from backend.context import origin
        from backend.storage import operations
        o = origin.current_origin.get()
        return operations.current_run_id.get(), (o.channel if o else "")
    except Exception:
        return None, ""


def _index(name: str, text: str) -> None:
    """Keep memory_search's copy of the skill current. Fire-and-forget; silent on failure."""
    try:
        from backend.brain import semindex
        spawn(semindex.index_skill(name, text))
    except Exception as exc:
        log.debug("skills: index dispatch skipped (%s)", exc)


def _write(skill: Skill) -> str:
    """Write the file and read it back. Returns the text written; raises if it did not land."""
    path = skills_dir() / f"{skill.name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(skill)
    path.write_bytes(text.encode("utf-8"))     # LF on every host, so undo is exact
    back = parse(path.read_bytes().decode("utf-8"), skill.name)
    if (back.name, back.description, back.status) != (skill.name, skill.description, skill.status):
        raise OSError(f"{path.name} did not read back as written")
    return text


# What this turn did with skills, for the lines at the end of a Discord reply: which it
# followed, and what changed. One record per turn (start_turn), shared by the tool calls
# that run concurrently inside it.
_turn_events: ContextVar[dict | None] = ContextVar("skill_turn_events", default=None)
UNDO_HINT = "/skill undo takes it back"
_FOLLOWED_FOR_S = 30 * 60      # a correction this soon after is about that skill


def start_turn() -> None:
    _turn_events.set({"changes": [], "followed": [], "worked_on": []})


def _record(kind: str, item: str) -> None:
    events = _turn_events.get()
    if events is not None and item not in events.setdefault(kind, []):
        events[kind].append(item)


def _note(event: str) -> None:
    _record("changes", event)


def followed() -> list[str]:
    """The skills opened (or run with /skill) in this turn to do what he asked, which is what
    their Checks and the "followed" line are for: not the ones the turn worked ON."""
    events = _turn_events.get() or {}
    return [n for n in events.get("followed", []) if n not in events.get("worked_on", [])]


def worked_on(name: str) -> None:
    """This turn did something TO the skill -- scheduled it as a routine, saved, changed,
    retired, undid or exported it -- rather than follow it for a request. Live (5 October):
    "cancel and re-add each one against the skill" opened stock-research first, so the
    scheduling reply counted as following it, ended with "followed: stock-research", and
    its Checks had it rewritten with a bull and a bear case and two closing prices nothing
    had looked up."""
    _record("worked_on", name)


def set_aside(names: list[str]) -> None:
    """The check before sending found the reply does not carry out these skills' task (it
    answered something narrower, or asked him a question): not followed after all."""
    events = _turn_events.get()
    if events is not None:
        events["followed"] = [n for n in events["followed"] if n not in names]


def turn_footer() -> str:
    """The lines for the end of this turn's Discord reply, or "" when no skill was involved:
    "· followed: X" so a one-sentence correction can change X, and what changed, with the undo."""
    events = _turn_events.get() or {}
    lines = []
    if followed():
        lines.append("· followed: " + ", ".join(followed()))
    if events.get("changes"):
        lines.append("· " + " · ".join(events["changes"]) + f" · {UNDO_HINT}")
    return ("\n\n" + "\n".join(lines)) if lines else ""


_footers: dict[str, str] = {}
_last_followed: dict[str, tuple[list[str], float]] = {}


def close_turn(channel: str) -> None:
    """End of chat(): keep this turn's lines for its channel until the reply is delivered,
    and remember which skills it followed, for a correction in the next message."""
    _footers[channel] = turn_footer()
    names = followed()
    if names:
        _last_followed[channel] = (names, lifelog.stamp()[0])
    else:
        _last_followed.pop(channel, None)


def note_followed(channel: str, name: str) -> None:
    """A message that followed a skill outside a turn (a routine's run, DMed to him)."""
    _last_followed[channel] = ([name], lifelog.stamp()[0])


def pop_footer(channel: str) -> str:
    """The line to add to the reply being delivered on `channel`, once."""
    return _footers.pop(channel, "")


def _when(ts: float) -> str:
    """"4 Oct", in local time."""
    dt = datetime.fromtimestamp(ts, tz=lifelog.LOCAL_TZ)
    return f"{dt.day} {dt.strftime('%b')}"


# --------------------------------------------------------------------------- the tool
_FileSender = Callable[[str, str, str], Awaitable[tuple[bool, str]]]
_file_sender: Optional[_FileSender] = None


def set_file_sender(fn: Optional[_FileSender]) -> None:
    """(filename, text, caption) -> (sent, how). main.py wires it to the Discord bridge."""
    global _file_sender
    _file_sender = fn


def _view(name: str) -> str:
    s = load(name) if name else None
    if s is None:
        return f"Error: you have no skill called '{name}'." + _closest(name, active_skills())
    if not s.active:
        return (f"Error: '{name}' is retired. Save it again with skill(action=\"save\") to bring "
                "it back.")
    run_id, channel = _run_and_channel()
    store.record_use(s.name, "model", run_id=run_id, channel=channel)
    _record("followed", s.name)
    return (f"Skill '{s.name}': {s.description}\nFollow it for this request, adapting it to the "
            f"case. Don't mention the file.\n\n{s.body}")


def _save(name: str, description: str, when_to_use: str, steps, pitfalls, checks, tools,
          source: str) -> str:
    try:
        name = slug(name)
    except ValueError as e:
        return f"Error: {e}."
    description = " ".join((description or "").split())
    if not description:
        return "Error: a skill needs a one-line description of what it does and when to use it."
    if len(description) > MAX_DESCRIPTION:
        return f"Error: keep the description under {MAX_DESCRIPTION} characters; it is in every prompt."
    if not [s for s in (steps or []) if str(s).strip()]:
        return "Error: a skill needs at least one step."
    if len(steps) > MAX_STEPS:
        return f"Error: at most {MAX_STEPS} steps; a longer procedure is two skills."
    source = (source or "learned").strip().lower()
    if source not in SOURCES:
        return f"Error: source must be one of {', '.join(SOURCES)}."
    tools = [str(t).strip() for t in (tools or []) if str(t).strip()]
    unknown = sorted(set(tools) - _known_tools())
    if unknown:
        return f"Error: these are not tools you have: {', '.join(unknown)}."
    body = build_body(when_to_use, steps, pitfalls, checks)
    if len(body) > MAX_BODY:
        return f"Error: the skill is {len(body):,} characters; keep it under {MAX_BODY:,}."

    existing = load(name)
    if existing is None and len(active_skills()) >= MAX_ACTIVE:
        return (f"Error: you already keep {MAX_ACTIVE} skills. Retire one that no longer earns "
                "its place first.")
    today = _today()
    skill = Skill(name=name, description=description, body=body, status=ACTIVE, source=source,
                  tools=tools, created=(existing.created if existing and existing.created else today),
                  updated=today, extra=(existing.extra if existing else {}))
    if existing is not None:
        path = skills_dir() / f"{name}.md"
        try:
            old = path.read_bytes().decode("utf-8")
        except Exception:
            old = render(existing)
    else:
        old = ""                         # an empty revision: undo removes the new skill
    if store.add_revision(name, "replace" if existing else "create", old,
                          reason="saved over" if existing else "new", actor="jarvis") is None:
        return ("Error: the earlier version could not be kept, so nothing was changed. "
                "Try again in a moment.")
    try:
        text = _write(skill)
    except Exception as e:
        return f"Error: the skill was NOT saved ({e})."
    _index(name, text)
    worked_on(name)
    what = "replaced the earlier version, which is kept" if existing else "new"
    _note(f"{'Updated' if existing else 'Saved'} skill {name} ({skill.steps()} steps)")
    return (_owner.say(f"Confirmed: skill '{name}' saved to jarvis-vault/skills/{name}.md ({what}, "
            f"{skill.steps()} steps). It is in your skills list from the next message on. "
            "Tell «owner» in one line what you saved. Don't explain how to undo it: a line "
            "saying so is added after your reply."))


def _retire(name: str, reason: str) -> str:
    s = load(name) if name else None
    if s is None:
        return f"Error: you have no skill called '{name}'." + _closest(name, active_skills())
    if not s.active:
        return f"'{name}' is already retired."
    path = skills_dir() / f"{name}.md"
    try:
        old = path.read_bytes().decode("utf-8")
    except Exception:
        old = render(s)
    if store.add_revision(name, "retire", old, reason=reason) is None:
        return "Error: the skill's current version could not be kept, so it was not retired."
    s.status, s.updated = RETIRED, _today()
    try:
        text = _write(s)
    except Exception as e:
        return f"Error: '{name}' was NOT retired ({e})."
    _index(name, text)
    worked_on(name)
    _note(f"Retired skill {name}")
    return (f"Retired skill '{name}': it is off your skills list and stays in "
            "jarvis-vault/skills/; saving it again brings it back.")


def export_file(name: str) -> tuple[str, str, str] | str:
    """(filename, text, caption) of a skill in the open Agent Skills format, or an error.

    Valid under agentskills.io's rules: `name` and `description` at the top, everything
    else under `metadata` as strings. Claude Code, Codex, Cursor and Gemini CLI read it."""
    try:
        key = slug(name)
    except ValueError:
        key = ""
    s = load(key) if key else None
    if s is None:
        return f"Error: you have no skill called '{name}'." + _closest(name or "", active_skills())
    meta = {"author": "JARVIS", "source": s.source}
    if s.created:
        meta["created"] = s.created
    if s.updated:
        meta["updated"] = s.updated
    lines = ["---", f"name: {s.name}", f"description: {_yaml_value(s.description[:1024])}",
             _owner.say("compatibility: Written for JARVIS, «owner's» assistant; tool names in it are "
             "JARVIS's own."), "metadata:"]
    lines += [f"  {k}: \"{v}\"" for k, v in meta.items()]
    lines += ["---", "", s.body.strip(), ""]
    caption = (f"Skill `{s.name}` in the open Agent Skills format. Save it as "
               f"`{s.name}/SKILL.md` in your skills folder (Claude Code: "
               f"`~/.claude/skills/{s.name}/SKILL.md`).")
    return "SKILL.md", "\n".join(lines), caption


async def _export(name: str) -> str:
    out = export_file(name)
    if isinstance(out, str):
        return out
    if _file_sender is None:
        return "Error: Discord is not connected, so the file could not be sent."
    filename, text, caption = out
    try:
        sent, how = await _file_sender(filename, text, caption)
    except Exception as e:
        return f"Error: sending the skill failed ({e}). Nothing was confirmed sent."
    if not sent:
        return f"Error: the skill could not be confirmed sent ({how}). Don't say it was sent."
    worked_on(name)
    return _owner.say(f"Sent '{name}' to «owner» as SKILL.md ({how}). Don't repeat its contents.")


async def _learn(learn_from: str, name: str, day: str, about: str) -> str:
    from backend.brain import skill_learning
    run_id, channel = _run_and_channel()
    fields, where, error = await skill_learning.draft(learn_from, channel or "discord",
                                                      current_run=run_id, day=day, about=about)
    if error:
        return error
    try:
        drafted = slug(fields["name"]) if fields["name"] else ""
    except ValueError:
        drafted = ""
    target = name.strip() or drafted
    if not target:
        return "Error: the draft had no usable name; call learn again with a name."
    try:
        target = slug(target)
    except ValueError as e:
        return f"Error: {e}."
    if not name.strip() and load(target) is not None:
        return (f"Error: you already have a skill called '{target}'. To replace it with this one, "
                f"call learn again with name '{target}'; to keep both, give the new one another name.")
    tools = [t for t in fields["tools"] if t in _known_tools()]
    source = "document" if (learn_from or "").strip().lower() == "document" else "learned"
    out = _save(target, fields["description"], fields["when_to_use"], fields["steps"],
                fields["pitfalls"], fields["checks"], tools, source)
    if not out.startswith("Confirmed"):
        return out
    learned = load(target)
    steps = _section(learned.body, ("procedure",)) if learned else ""
    caution = (_owner.say(" It came from a document someone else wrote: tell «him» to glance over the steps.")
               if source == "document" else "")
    return (_owner.say(f"{out} Learned from {where}.{caution}\n\nThe steps, to tell «him» in brief:\n{steps}"))


def _undo(name: str = "") -> str:
    """Put back the version before the last change: a skill's, or (no name) the most recent
    change in the last day made by the owner or JARVIS -- never quietly a migration's."""
    import re as _re
    name = (name or "").strip().lower()
    if name:
        revs = store.revisions(name, limit=60)
    else:
        revs = [r for r in store.recent_revisions(lifelog.stamp()[0] - 86_400)
                if r["actor"] in ("jarvis", "owner")]
    consumed = {int(n) for r in revs if r["action"] == "undo"
                for n in _re.findall(r"undid #(\d+)", r["reason"])}
    target = next((r for r in revs if r["action"] != "undo" and r["revision_id"] not in consumed), None)
    if target is None:
        return (f"Nothing to undo for '{name}'." if name
                else "No skill was changed in the last day, so there is nothing to undo.")
    skill_name = name or target["skill"]
    worked_on(skill_name)
    path = skills_dir() / f"{skill_name}.md"
    current = path.read_bytes().decode("utf-8") if path.exists() else ""
    if store.add_revision(skill_name, "undo", current, actor="owner",
                          reason=f"undid #{target['revision_id']}") is None:
        return "Error: the current version could not be kept, so nothing was undone."
    if not target["content"]:
        if path.exists():
            path.unlink()
        _index(skill_name, "")
        _note(f"Removed skill {skill_name}")
        return f"Undone: the skill '{skill_name}' was new, so it is gone again (its text is kept)."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(target["content"].encode("utf-8"))
    _index(skill_name, target["content"])
    _note(f"Restored skill {skill_name} to the version before")
    return f"Undone: '{skill_name}' is back to the version before its last change."


_PATCH_SECTIONS = {"procedure": "Procedure", "pitfalls": "Pitfalls", "checks": "Checks",
                   "when_to_use": "When to use"}
_MAX_CHANGES_KEPT = 8
_SECTION_ORDER = ["When to use", "Procedure", "Pitfalls", "Checks", "Changes"]


def _sections(body: str) -> list[list]:
    """[[title, [lines]], ...] in order; text before the first heading has title ""."""
    out: list[list] = [["", []]]
    for line in body.splitlines():
        if line.startswith("## "):
            out.append([line[3:].strip(), []])
        else:
            out[-1][1].append(line)
    return out


def _items(lines: list[str]) -> list[str]:
    return [_NUMBERING.sub("", l).strip() for l in lines if _NUMBERING.match(l)]


def _join_sections(parts: list[list]) -> str:
    chunks = []
    for title, lines in parts:
        text = "\n".join(lines).strip()
        if not title:
            if text:
                chunks.append(text)
            continue
        if text:
            chunks.append(f"## {title}\n{text}")
    return "\n\n".join(chunks)


def _apply_edit(parts: list[list], edit: dict) -> str:
    """Apply one typed edit in place. Returns what it did, in words; raises ValueError."""
    op = str(edit.get("op") or "").strip().lower()
    key = str(edit.get("section") or "procedure").strip().lower().replace(" ", "_")
    if key not in _PATCH_SECTIONS:
        raise ValueError(f"section must be one of {', '.join(_PATCH_SECTIONS)}")
    title = _PATCH_SECTIONS[key]
    text = " ".join(str(edit.get("text") or "").split())
    if _EMAIL_RE.search(text):
        raise ValueError("an edit cannot hold an email address")
    if op not in ("add", "revise", "remove"):
        raise ValueError("op must be add, revise or remove")
    if op in ("add", "revise") and not text:
        raise ValueError(f"{op} needs the text")
    part = next((p for p in parts if p[0].lower() == title.lower()), None)
    if part is None:
        if op != "add":
            raise ValueError(f"the skill has no {title} section")
        part = [title, []]
        order = _SECTION_ORDER
        later = [i for i, p in enumerate(parts) if p[0] in order and order.index(p[0]) > order.index(title)]
        parts.insert(later[0] if later else len(parts), part)
    if key == "when_to_use":
        current = " ".join(" ".join(part[1]).split())
        part[1] = [text if op == "revise" else f"{current} {text}".strip()] if op != "remove" else []
        return f"{'rewrote' if op == 'revise' else 'added to' if op == 'add' else 'removed'} When to use"
    items = _items(part[1])
    noun = {"procedure": "step", "pitfalls": "pitfall", "checks": "check"}[key]
    try:
        index = int(edit.get("index") or 0)
    except (TypeError, ValueError):
        raise ValueError("index must be a number")
    if op == "add":
        where = index if 1 <= index <= len(items) + 1 else len(items) + 1
        items.insert(where - 1, text)
        done = f"added {noun} {where}: {text}"
    else:
        if not 1 <= index <= len(items):
            raise ValueError(f"there is no {noun} {index} (it has {len(items)})")
        old = items[index - 1]
        if op == "revise":
            items[index - 1] = text
            done = f"{noun} {index} now: {text}"
        else:
            items.pop(index - 1)
            done = f"removed {noun} {index} ({old})"
    if key == "procedure":
        if not items:
            raise ValueError("a skill needs at least one step")
        if len(items) > MAX_STEPS:
            raise ValueError(f"at most {MAX_STEPS} steps")
        part[1] = [f"{i}. {t}" for i, t in enumerate(items, 1)]
    else:
        part[1] = [f"- {t}" for t in items]
    return done


def _patch(name: str, edits, reason: str) -> str:
    """Typed edits to one skill: add, revise or remove one step, pitfall or check. Code does
    the surgery (positions.py and people.py work the same way), keeps the old version, and
    dates the change in the skill's Changes section."""
    s = load(name) if name else None
    if s is None:
        return f"Error: you have no skill called '{name}'." + _closest(name, active_skills())
    if isinstance(edits, dict):
        edits = [edits]
    if not edits:
        return "Error: give the edits, e.g. [{\"op\": \"remove\", \"section\": \"procedure\", \"index\": 2}]."
    parts = _sections(s.body)
    done = []
    edits = [e if isinstance(e, dict) else {} for e in edits[:6]]
    # Indexes refer to the skill as it was before this patch. Live, "remove step 2" then
    # "revise step 2" overwrote the old step 3 (the email search). Applying the highest index
    # first keeps every lower one where it was; adds to the end go last; equal indexes keep
    # their order, so "remove 2, add at 2" is a replacement.
    def _at(e):
        try:
            return int(e.get("index") or 0)
        except (TypeError, ValueError):
            return 0
    order = sorted(range(len(edits)), key=lambda i: (-_at(edits[i]) if _at(edits[i]) else 1, i))
    try:
        results = {i: _apply_edit(parts, edits[i]) for i in order}
    except ValueError as e:
        return f"Error: nothing was changed: {e}."
    done = [results[i] for i in range(len(edits))]
    said = " ".join((reason or "").split())[:200]
    changes = next((p for p in parts if p[0] == "Changes"), None)
    if changes is None:
        changes = ["Changes", []]
        parts.append(changes)
    entry = f"- {_today()}: {'; '.join(done)}" + (_owner.say(f' («he» said: "{said}")') if said else "")
    changes[1] = ([l for l in changes[1] if l.strip()] + [entry])[-_MAX_CHANGES_KEPT:]
    path = skills_dir() / f"{s.name}.md"
    old = path.read_bytes().decode("utf-8")
    if store.add_revision(s.name, "patch", old, actor="owner" if said else "jarvis",
                          reason=said or "; ".join(done)) is None:
        return "Error: the current version could not be kept, so nothing was changed."
    s.body, s.updated = _join_sections(parts), _today()
    try:
        text = _write(s)
    except Exception as e:
        return f"Error: the skill was NOT changed ({e})."
    _index(s.name, text)
    worked_on(s.name)
    _note(f"Updated skill {s.name}: {'; '.join(done)[:160]}")
    return (_owner.say(f"Confirmed: skill '{s.name}' changed: {'; '.join(done)}. Tell «him» in a few words "
            "what changed; a line naming it and how to take it back is added after your reply."))


# Wording that reads as a correction of how a task was done. Shared with the overnight pass,
# which folds in corrections the live turn missed.
CORRECTION_WORDS = re.compile(
    r"\b(next time|from now on|instead|don't|do not|skip|stop|always|never|too (long|short|much)|"
    r"less|more|useless|wrong|not what|rather|prefer|leave out|drop|include|forgot|missing)\b", re.I)


def looks_like_correction(user_text: str) -> bool:
    from backend.brain import personality
    return bool(personality.is_correction_request(user_text) or CORRECTION_WORDS.search(user_text or ""))


def correction_context(channel: str, user_text: str = "", correction: bool | None = None) -> str:
    """For a correction right after a reply that followed a skill: which skill, its steps,
    and how to change it. "" otherwise.

    Only when the message reads as a correction (Jev's `correction`, or the wording). Live, with
    the steps given to every next message, back-to-back research requests followed them
    without opening the skill (8 of 10 opened, down from 10 of 10), so no Checks ran."""
    names, at = _last_followed.get(channel, ([], 0.0))
    if not names or lifelog.stamp()[0] - at > _FOLLOWED_FOR_S:
        return ""
    if not (correction or looks_like_correction(user_text)):
        return ""
    blocks = []
    for name in names[:2]:
        s = load(name)
        if s is None or not s.active:
            continue
        steps = _section(s.body, ("procedure", "steps"))
        pitfalls = _section(s.body, ("pitfalls",))
        checks = _section(s.body, ("checks",))
        blocks.append(f"Skill '{s.name}':\nProcedure:\n{steps}"
                      + (f"\nPitfalls:\n{pitfalls}" if pitfalls else "")
                      + (f"\nChecks:\n{checks}" if checks else ""))
    if not blocks:
        return ""
    return (_owner.say("[Your last reply followed the skill below. If «owner's» message corrects how that "
            "task was done (\"skip the profile step next time\", \"always give the P/E\"), change "
            "the skill with skill(action=\"patch\", name=..., edits=[{op, section, index, text}], "
            "reason=<«his» words>) and tell «him» in a few words. Indexes are the numbers shown "
            "below. When «he» says to stop doing something, remove that step AND add a pitfall "
            "saying not to do it and why, so the habit does not come back. If it is about how you behave in "
            "general, that is a standing rule instead. If it corrects nothing about the task, "
            "ignore this.]\n") + "\n\n".join(blocks))


def corrected_counts() -> dict[str, int]:
    """{skill: changes made from his corrections}, for /skills."""
    from collections import Counter
    out: Counter = Counter()
    for s in load_all():
        out[s.name] = sum(1 for r in store.revisions(s.name, limit=200)
                          if r["action"] == "patch" and r["actor"] == "owner")
    return {k: v for k, v in out.items() if v}


def handle_undo_command(arg: str = "") -> str:
    """"/skill undo [name]" on Discord: the owner himself, so no model and no caller check."""
    return _undo(arg)


def _routines() -> dict[str, list[str]]:
    """{skill: ["on weekdays, next Mon 06 Oct 15:30", ...]} from the scheduled tasks."""
    try:
        from backend.brain import chronos
        out: dict[str, list[str]] = {}
        rule = {"once": "once", "daily": "every day", "weekdays": "on weekdays", "weekly": "weekly"}
        for t in chronos.agenda()["tasks"]:
            if t.get("skill"):
                nxt = t["due"].astimezone(lifelog.LOCAL_TZ).strftime("%a %d %b %H:%M")
                out.setdefault(t["skill"], []).append(f"{rule.get(t['repeat'], t['repeat'])}, next {nxt}")
        return out
    except Exception as exc:
        log.debug("skills: routines unavailable (%s)", exc)
        return {}


def list_text() -> str:
    skills = load_all()
    live = sorted((s for s in skills if s.active), key=lambda s: s.name)
    retired = sorted(s.name for s in skills if not s.active)
    if not live and not retired:
        return ("No skills yet. Teach me one in words (\"when you prep me for a meeting, ...\"), "
                "or ask me to remember how I did something.")
    stats = store.use_stats()
    routines = _routines()
    corrected = corrected_counts()
    lines = [f"Skills ({len(live)} in use):"] if live else ["No skills in use."]
    for s in live:
        st = stats.get(s.name)
        used = (f"opened {st['uses']} time{'s' if st['uses'] != 1 else ''}, last {_when(st['last'])}"
                if st else "not opened yet")
        timer = "".join(f" Runs {r}." for r in routines.get(s.name, []))
        fixed = corrected.get(s.name, 0)
        fixes = f" Corrected {fixed} time{'s' if fixed != 1 else ''}." if fixed else ""
        lines.append(f"• {s.name} — {used}.{fixes}{timer} {s.description}")
    if retired:
        lines.append(f"Retired ({len(retired)}): {', '.join(retired)}")
    lines.append("Run one: /skill <name> <details> · Get the file: /skill export <name> · "
                 "Take back the last change: /skill undo")
    return "\n".join(lines)


async def skill(action: str, name: str = "", description: str = "", when_to_use: str = "",
                steps: list[str] | None = None, pitfalls: list[str] | None = None,
                checks: list[str] | None = None, tools: list[str] | None = None,
                source: str = "learned", reason: str = "", learn_from: str = "",
                day: str = "", about: str = "", edits: list | None = None) -> str:
    """The `skill` tool: list, view, save, learn, patch, undo, retire, export."""
    action = (action or "").strip().lower()
    name = (name or "").strip()
    if action in ("save", "learn", "patch", "undo", "retire", "export") and not _live_caller():
        return (_owner.say("Error: skills are changed or sent only in the conversation with «owner». From "
                "here, open one with view and follow it; mention in your answer what you would change."))
    try:
        if action == "list":
            return list_text()
        if action == "view":
            return _view(name.lower())
        if action == "save":
            return _save(name, description, when_to_use, steps or [], pitfalls, checks, tools, source)
        if action == "learn":
            return await _learn(learn_from, name, day, about)
        if action == "patch":
            return _patch(name.lower(), edits or [], reason)
        if action == "undo":
            return _undo(name)
        if action == "retire":
            return _retire(name.lower(), reason)
        if action == "export":
            return await _export(name.lower())
    except Exception as e:
        log.warning("skills: %s failed: %s", action, e)
        return f"Error: skill {action} failed ({e})."
    return "Error: action must be list, view, save, learn, patch, undo, retire or export."


# --------------------------------------------------------------------------- /skill
_FORCED = re.compile(r"^\s*/skill\s+(?!export\b|undo\b)([A-Za-z0-9][\w-]*)\s*(.*)$", re.S | re.I)


def forced(user_text: str) -> tuple[str, str]:
    """"/skill <name> <details>": (name, block for this turn's message), or ("", "").

    A skill that does not exist still gets a block, telling JARVIS to say so, so the
    command never falls silently through to an ordinary turn."""
    m = _FORCED.match(user_text or "")
    if not m:
        return "", ""
    name, details = m.group(1).lower(), " ".join(m.group(2).split())
    s = load(name)
    if s is None or not s.active:
        return "", (_owner.say(f"[«owner» ran /skill {name}, but you have no skill by that name in use. "
                    f"Tell «him» so.{_closest(name, active_skills())}]"))
    run_id, channel = _run_and_channel()
    store.record_use(s.name, "command", run_id=run_id, channel=channel)
    _record("followed", s.name)
    ask = details or _owner.say("no details: ask what «he» wants it for only if the skill can't run without them")
    return s.name, (_owner.say(f"[«owner» ran /skill {s.name} -- follow this skill for «his» request "
                    f"({ask}).]\n\n{s.body}"))


def handle_skills_command(arg: str = "") -> str:
    """"/skills" on Discord."""
    if (arg or "").strip():
        return ("Usage: /skills (the list) · /skill <name> <details> · /skill export <name> · "
                "/skill undo [name]")
    return list_text()
