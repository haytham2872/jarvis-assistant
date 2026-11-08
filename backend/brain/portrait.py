"""backend/brain/portrait.py -- the owner's portrait, changed by typed dated edits.

jarvis-vault/people/owner.md used to be rewritten in full every session, which let it grow
to ~1,000 words of dated events and system internals, and carried a stale line forward for
as long as nobody rewrote it. Same discipline as positions.py: the model emits small typed
ops, every byte of file surgery happens here.

Format (v2):

    # Portrait
    <!-- portrait v2 -->

    ## Situation
    - what is true now and can change (as of 2026-11-01)

    ## Who they are
    - ...

    ## How to be with them
    - ...

Every entry is "- text (as of YYYY-MM-DD)"; the date is when the line was last confirmed.
A Situation line nobody reconfirms for AGE_OUT_DAYS moves to people/owner-history.md.
Nothing here touches a portrait without the marker (that one gets one full migration
rewrite in consolidation.py). Pure functions: no file I/O.
"""

import datetime
import re

from backend import owner as _owner
from backend.brain.people import _DUPLICATE_OVERLAP, _overlap

MARKER = "<!-- portrait v2 -->"
SECTIONS = ("Situation", "Who they are", "How to be with them")
AGE_OUT_DAYS = 45
HISTORY_TITLE = "# Portrait history"

_ENTRY_RE = re.compile(r"^-\s+(.*?)(?:\s*\(as of (\d{4}-\d{2}-\d{2})\))?\s*$")
# The model often dates its own text and ends with a full stop after it ("... (as of 2026-11-07)."),
# seen live in the replay: strip that too, or the line gets two dates and a match misses.
_ASOF_TAIL_RE = re.compile(r"\s*\(as of \d{4}-\d{2}-\d{2}\)[\s.;,]*$")


def is_v2(md: str) -> bool:
    return MARKER in [l.strip() for l in (md or "").split("\n")[:4]]


# ---------------------------------------------------------------------------
# Parse / render (lines are kept raw, so the round trip is byte-stable)
# ---------------------------------------------------------------------------

def parse(md: str) -> tuple[list[str], list[list]]:
    """(preamble lines, sections) where a section is [heading line, [body lines]]."""
    pre: list[str] = []
    sections: list[list] = []
    for line in (md or "").split("\n"):
        if line.startswith("## "):
            sections.append([line, []])
        elif sections:
            sections[-1][1].append(line)
        else:
            pre.append(line)
    return pre, sections


def render(pre: list[str], sections: list[list]) -> str:
    out = list(pre)
    for heading, body in sections:
        out.append(heading)
        out.extend(body)
    return "\n".join(out)


def _title(heading: str) -> str:
    return heading[3:].strip().lower()


def _entry(line: str):
    """(text, date or None) for a bullet line, else None."""
    if not line.startswith("- "):
        return None
    m = _ENTRY_RE.match(line)
    return (m.group(1).strip(), m.group(2)) if m else None


def _line(text: str, today: str) -> str:
    return f"- {text} (as of {today})"


def _clean(text) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if t.startswith("- "):
        t = t[2:]
    return _ASOF_TAIL_RE.sub("", t).strip()


def _find_section(sections: list[list], name: str):
    want = str(name or "").strip().lower()
    return next((s for s in sections if _title(s[0]) == want), None)


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------

def apply_patch(md: str, ops, today: str) -> tuple[str, list[str]]:
    """Apply typed ops. Returns (new_md, report); the report names every op skipped."""
    if not is_v2(md):
        return md, ["portrait is not v2 -- patch ignored"]
    if not isinstance(ops, list) or not all(isinstance(o, dict) for o in ops):
        return md, ["malformed op list -- portrait unchanged"]

    pre, sections = parse(md)
    report: list[str] = []
    for i, op in enumerate(ops):
        action = str(op.get("action") or "").strip().lower()
        label = f"op {i + 1} ({action} {op.get('section')!r})"
        text = _clean(op.get("text"))
        if action not in ("add", "revise", "remove", "confirm"):
            report.append(f"{label}: unknown action -- skipped")
            continue
        if action in ("add", "revise") and not text:
            report.append(f"{label}: no text -- skipped")
            continue
        sec = _find_section(sections, op.get("section"))
        if sec is None:
            if str(op.get("section") or "").strip().lower() not in {s.lower() for s in SECTIONS}:
                report.append(f"{label}: unknown section -- skipped")
                continue
            title = next(s for s in SECTIONS if s.lower() == str(op["section"]).strip().lower())
            if sections and sections[-1][1] and sections[-1][1][-1] != "":
                sections[-1][1].append("")
            sec = [f"## {title}", [""]]
            sections.append(sec)
        body = sec[1]
        idx = [k for k, l in enumerate(body) if _entry(l)]

        if action == "add":
            dup = next((k for k in idx if _overlap(_entry(body[k])[0], text) >= _DUPLICATE_OVERLAP), None)
            if dup is not None:
                body[dup] = _line(_entry(body[dup])[0], today)
                report.append(f"{label}: near-duplicate of an existing entry -- confirmed it instead")
                continue
            at = (idx[-1] + 1) if idx else 0
            if not idx:                       # keep the blank line under the heading
                at = 1 if body[:1] == [""] else 0
            body.insert(at, _line(text, today))
            continue

        needle = _clean(op.get("match")).lower()
        hits = [k for k in idx if needle and needle in _entry(body[k])[0].lower()]
        if len(hits) != 1:
            report.append(f"{label}: match {op.get('match')!r} found {len(hits)} entries -- skipped")
            continue
        k = hits[0]
        if action == "remove":
            del body[k]
        elif action == "confirm":
            body[k] = _line(_entry(body[k])[0], today)
        else:
            body[k] = _line(text, today)
    return render(pre, sections), report


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------

def age_out(md: str, today: str, days: int = AGE_OUT_DAYS) -> tuple[str, list[str]]:
    """Remove Situation entries last confirmed more than `days` ago. Returns (md, removed lines)."""
    if not is_v2(md):
        return md, []
    try:
        now = datetime.date.fromisoformat(today)
    except ValueError:
        return md, []
    pre, sections = parse(md)
    aged: list[str] = []
    for heading, body in sections:
        if _title(heading) != "situation":
            continue
        keep = []
        for l in body:
            e = _entry(l)
            if e and e[1]:
                try:
                    old = (now - datetime.date.fromisoformat(e[1])).days > days
                except ValueError:
                    old = False
                if old:
                    aged.append(l)
                    continue
            keep.append(l)
        body[:] = keep
    return (render(pre, sections), aged) if aged else (md, [])


def fading(md: str, today: str, window: int = 7) -> list[str]:
    """Texts of Situation entries about to age out: AGE_OUT_DAYS - window to AGE_OUT_DAYS days old."""
    if not is_v2(md):
        return []
    try:
        now = datetime.date.fromisoformat(today)
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    for heading, body in parse(md)[1]:
        if _title(heading) != "situation":
            continue
        for l in body:
            e = _entry(l)
            if not (e and e[1]):
                continue
            try:
                age = (now - datetime.date.fromisoformat(e[1])).days
            except ValueError:
                continue
            if AGE_OUT_DAYS - window <= age <= AGE_OUT_DAYS:
                out.append(e[0])
    return out


def prompt_line() -> str:
    """One line for the per-turn identity block while a recent brief's question is open."""
    try:
        from backend.brain import chronos
        lines = chronos.portrait_asked_recently()
    except Exception:
        return ""
    if not lines:
        return ""
    return _owner.say("This morning's brief asked «owner» whether these lines of «his» portrait still hold: "
                      + "; ".join(lines) + ". If «he» answers, take it as it comes; the session-end portrait "
                      "edit will confirm or correct them.")


def append_history(existing: str, aged_lines: list[str], today: str) -> str:
    """people/owner-history.md with a dated block of aged lines added. Append-only, and
    idempotent: lines already in the file are not added again."""
    existing = existing or ""
    fresh = [l for l in aged_lines if l not in existing.split("\n")]
    if not fresh:
        return existing
    base = existing.rstrip("\n") if existing.strip() else HISTORY_TITLE
    return base + f"\n\n## {today}\n" + "\n".join(fresh) + "\n"
