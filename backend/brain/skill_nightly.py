"""backend/brain/skill_nightly.py — the skills' overnight pass (slice 5, 4 October 2026).

Once a night, between 03:00 and 06:00 local time (after night tasks have started, never while a
turn is running, held by /pause), JARVIS looks after his skills:

  repeats   a request the owner made at least 3 times, on at least 2 different days, with
            the same tools, that no skill covers yet, becomes a skill, built from the
            latest of those runs (skill_learning). If the requests fell on one weekday, or
            only on weekdays, at about the same time, a routine is offered: an approval
            request with Approve / Deny, put to him after the morning brief, never at 3am.
  merges    two skills doing the same job (the same tools, much the same words, and the
            model agreeing) are merged into the more used one; the other is retired.
  folds     a correction the live turn missed: he corrected a reply that followed a skill
            ("next time skip...") and nothing changed; the skill is patched with his words.
  retires   a skill not opened in 90 days leaves the list (the file stays). The clock starts
            when this pass first sees a skill, so usage counted from zero in October does
            not retire everything in January.

Every change keeps the previous version (/skill undo takes back the last one) and is listed
in the next morning brief. At most one new skill, one merge and two folds a night.

Conservative by construction: candidates need code-checked evidence (counts, days, tools,
words) before the one cheap-tier call that drafts or confirms. scripts/measure_skill_repeats.py
prints, read-only, what the repeat detector would pick on the real history.
"""
from __future__ import annotations
from backend import owner as _owner

import datetime
import json
import logging
import re
import statistics
from pathlib import Path
from typing import Callable, Optional

from backend.brain import pacing
from backend.storage import db, lifelog
from backend.storage import skills as store

log = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parents[2] / ".jarvis_skills_nightly.json"
_CHECK_EVERY_S = 30 * 60
_WINDOW = (3, 6)                 # local hours
_OFFER_AFTER_HOUR = 10           # a routine offer still pending (brief off or missed) goes out then
_LOOKBACK_DAYS = 30
_MIN_REPEATS = 3
_MIN_DAYS = 2
_WORDS_SIMILAR = 0.3             # request words (Jaccard) for two requests to be one kind
_TOOLS_SIMILAR = 0.67            # tool sets (Jaccard)
_COVERED = 0.5                   # an active skill holding half a cluster's common words covers it
_MERGE_TOOLS, _MERGE_WORDS = 0.75, 0.45
_FOLD_WINDOW_S = 30 * 60
_RETIRE_AFTER_DAYS = 90
_MAX_NEW, _MAX_MERGES, _MAX_FOLDS = 1, 1, 2

_busy_check: Optional[Callable[[], bool]] = None


def set_busy_check(fn: Optional[Callable[[], bool]]) -> None:
    global _busy_check
    _busy_check = fn


def _load() -> dict:
    return pacing.load_state(_STATE_FILE)


def _update(mutate) -> dict:
    return pacing.update_state(_STATE_FILE, mutate, log_prefix="skill_nightly")


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# --------------------------------------------------------------------------- the record
def _skill_ops(run_id: str) -> list[tuple[str, str, str]]:
    conn = db.connect()
    if conn is None:
        return []
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT outcome, args_preview, result_preview FROM operations WHERE run_id=? AND tool='skill'",
            (run_id,)).fetchall()]
    except Exception:
        return []


def _opened(run_id: str) -> list[str]:
    out = []
    for _, args, _r in _skill_ops(run_id):
        m = re.search(r"'name': '([a-z0-9-]+)'", args)
        if "'view'" in args and m:
            out.append(m.group(1))
    return out


def _runs(since: float, until: float) -> list[dict]:
    """Finished turns that used tools, with what was said and which tools worked."""
    from backend.brain import skill_learning as L
    out = []
    for channel in ("discord", "local"):
        for r in L._runs(channel, since, until):
            if r["state"] != "completed":
                continue
            ops = L._ops(r["run_id"])
            said = L._said(r, channel)
            out.append({**r, "channel": channel, "said": said, "words": L._words(said),
                        "tools": {o["tool"] for o in ops if o["outcome"] == "ok"},
                        "n_ops": len(ops), "opened": _opened(r["run_id"]),
                        "day": lifelog.local_date(r["started"])})
    return out


# --------------------------------------------------------------------------- repeats
def _covered(words: set[str], skills_list) -> str:
    from backend.brain import skill_learning as L
    for s in skills_list:
        text = L._words(f"{s.name.replace('-', ' ')} {s.description} {s.body[:600]}")
        if words and len(words & text) / len(words) >= _COVERED:
            return s.name
    return ""


def find_repeats(now_ts: float | None = None) -> list[dict]:
    """Clusters of the same kind of request: [{runs, days, tools, words, covered_by, key}].
    Read-only. Sorted by size, the largest first."""
    from backend.brain import skills
    now_ts = now_ts or lifelog.stamp()[0]
    runs = [r for r in _runs(now_ts - _LOOKBACK_DAYS * 86_400, now_ts)
            if r["n_ops"] >= 2 and r["tools"] and not r["opened"]
            and not r["said"].lstrip().lower().startswith("/skill")]
    parent = list(range(len(runs)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            if (_jaccard(runs[i]["tools"], runs[j]["tools"]) >= _TOOLS_SIMILAR
                    and _jaccard(runs[i]["words"], runs[j]["words"]) >= _WORDS_SIMILAR):
                parent[root(i)] = root(j)
    groups: dict[int, list[dict]] = {}
    for i, r in enumerate(runs):
        groups.setdefault(root(i), []).append(r)
    active = skills.active_skills()
    out = []
    for members in groups.values():
        days = sorted({m["day"] for m in members})
        if len(members) < _MIN_REPEATS or len(days) < _MIN_DAYS:
            continue
        counts: dict[str, int] = {}
        for m in members:
            for w in m["words"]:
                counts[w] = counts.get(w, 0) + 1
        common = {w for w, c in counts.items() if c * 2 >= len(members)}
        tools = set.intersection(*(m["tools"] for m in members)) or members[-1]["tools"]
        members.sort(key=lambda m: m["started"])
        out.append({"runs": members, "days": days, "tools": sorted(tools), "words": sorted(common),
                    "covered_by": _covered(common, active),
                    "key": "|".join(sorted(tools)) + "::" + " ".join(sorted(common))})
    return sorted(out, key=lambda c: -len(c["runs"]))


def _routine_for(runs: list[dict]) -> dict | None:
    """When the requests fell on one weekday, or only on weekdays, at about the same time:
    the routine to offer (local time), else None."""
    moments = [datetime.datetime.fromtimestamp(r["started"], tz=lifelog.LOCAL_TZ) for r in runs]
    minutes = statistics.median(m.hour * 60 + m.minute for m in moments)
    if max(abs((m.hour * 60 + m.minute) - minutes) for m in moments) > 90:
        return None                                  # no regular time of day
    minutes = int(round(minutes / 30) * 30) % (24 * 60)
    weekdays = {m.weekday() for m in moments}
    if len(weekdays) == 1:
        repeat, target = "weekly", next(iter(weekdays))
    elif weekdays <= set(range(5)) and len(weekdays) >= 3:
        repeat, target = "weekdays", None
    else:
        return None
    now = datetime.datetime.fromtimestamp(lifelog.stamp()[0], tz=lifelog.LOCAL_TZ)
    day = now.date() + datetime.timedelta(days=1)
    while (target is not None and day.weekday() != target) or (target is None and day.weekday() >= 5):
        day += datetime.timedelta(days=1)
    when = f"{day.isoformat()} {minutes // 60:02d}:{minutes % 60:02d}"
    words = {"weekly": f"every {day.strftime('%A')}", "weekdays": "on weekdays"}[repeat]
    return {"when": when, "repeat": repeat, "words": f"{words} at {when[-5:]}"}


async def _learn_repeat(cluster: dict) -> str:
    """Draft and save a skill for one cluster; queue the routine offer. Returns the brief line."""
    from backend.brain import skill_learning, skills
    latest = cluster["runs"][-1]
    asks = "\n".join(f"- {datetime.datetime.fromtimestamp(r['started'], tz=lifelog.LOCAL_TZ):%a %d %b %H:%M}: "
                     f"{r['said'][:300]}" for r in cluster["runs"])
    extra = (_owner.say(f"«Owner» has asked for this kind of task {len(cluster['runs'])} times:\n{asks}\n\n"
             "If these are not really the same kind of task, answer {\"skip\": true} instead."))
    fields, error = await skill_learning.draft_from_run(latest["channel"], latest, preface=extra)
    if fields is None:
        log.info("skill_nightly: no skill for a repeat (%s)", error)
        return ""
    try:
        name = skills.slug(fields["name"])
    except ValueError:
        return ""
    if skills.load(name) is not None:
        return ""
    tools = [t for t in fields["tools"] if t in skills._known_tools()]
    out = skills._save(name, fields["description"], fields["when_to_use"], fields["steps"],
                       fields["pitfalls"], fields["checks"], tools, "learned")
    if not out.startswith("Confirmed"):
        log.info("skill_nightly: repeat skill not saved: %s", out[:120])
        return ""
    days = ", ".join(datetime.date.fromisoformat(d).strftime("%a %d") for d in cluster["days"][-4:])
    line = f"learned {name} (you asked for it {len(cluster['runs'])} times: {days})"
    routine = _routine_for(cluster["runs"])
    if routine:
        offer = {"skill": name, "when": routine["when"], "repeat": routine["repeat"],
                 "words": routine["words"], "task": latest["said"][:300], "offered": False}
        _update(lambda st: st.setdefault("offers", []).append(offer))
        line += f"; I'll ask whether to run it {routine['words']}"
    return line


# --------------------------------------------------------------------------- merges
_MERGE_PROMPT = _owner.say("""Two procedures an assistant keeps for «owner» might be the same one.

<first name="{a}">
{a_text}
</first>
<second name="{b}">
{b_text}
</second>

If they are NOT the same kind of task, answer {{"same": false}}.
If they are, write the one merged procedure, keeping every useful step, pitfall and check
of both, as JSON only:
{{"same": true, "description": "...", "when_to_use": "...", "steps": ["..."],
  "pitfalls": ["..."], "checks": ["..."], "tools": ["..."]}}""")


def merge_candidates(skills_list) -> list[tuple]:
    from backend.brain import skill_learning as L
    out = []
    for i, a in enumerate(skills_list):
        for b in skills_list[i + 1:]:
            if not a.tools or not b.tools:
                continue
            wa = L._words(f"{a.description} {a.name.replace('-', ' ')}")
            wb = L._words(f"{b.description} {b.name.replace('-', ' ')}")
            if (_jaccard(set(a.tools), set(b.tools)) >= _MERGE_TOOLS
                    and _jaccard(wa, wb) >= _MERGE_WORDS):
                out.append((a, b))
    return out


async def _merge(a, b) -> str:
    from backend.brain import research, skill_learning, skills
    from backend.storage import llm_calls
    with llm_calls.labelled("skill.nightly"):
        raw = await research._llm(_MERGE_PROMPT.format(a=a.name, a_text=a.body[:5000],
                                                       b=b.name, b_text=b.body[:5000]), max_tokens=1800)
    data = skill_learning._parse(raw)
    if not data or not data.get("same"):
        return ""
    fields = skill_learning._clean({**data, "name": a.name})
    uses = store.use_stats()
    keep, drop = sorted((a, b), key=lambda s: -uses.get(s.name, {}).get("uses", 0))
    tools = [t for t in fields["tools"] if t in skills._known_tools()] or keep.tools
    out = skills._save(keep.name, fields["description"] or keep.description, fields["when_to_use"],
                       fields["steps"], fields["pitfalls"], fields["checks"], tools, keep.source)
    if not out.startswith("Confirmed"):
        return ""
    skills._retire(drop.name, f"merged into {keep.name}")
    return f"merged {drop.name} into {keep.name}"


# --------------------------------------------------------------------------- folds
_FOLD_PROMPT = _owner.say("""An assistant followed this procedure for «owner»:

{skill}

Right after, «owner» said: "{said}"

If «he» is correcting how that task was done, give the edits to the procedure as JSON only:
{{"edits": [{{"op": "add" | "revise" | "remove", "section": "procedure" | "pitfalls" | "checks",
  "index": <1-based, for revise/remove>, "text": "<for add/revise>"}}]}}
If «he» is not correcting the task (thanks, a new question, a general preference about how
the assistant talks), answer {{"edits": []}}.""")


def fold_candidates(now_ts: float) -> list[tuple[str, str]]:
    """[(skill, his words)] for corrections in the last day that changed nothing."""
    from backend.brain import skills
    runs = sorted(_runs(now_ts - 86_400, now_ts), key=lambda r: r["started"])
    out = []
    for i, r in enumerate(runs):
        if not r["opened"]:
            continue
        nxt = next((n for n in runs[i + 1:] if n["channel"] == r["channel"]
                    and n["session_id"] == r["session_id"]), None)
        if nxt is None or nxt["started"] - r["ended"] > _FOLD_WINDOW_S:
            continue
        said = nxt["said"]
        if not skills.looks_like_correction(said):
            continue
        changed = any(o[0] == "ok" and ("'patch'" in o[1] or "'save'" in o[1]) for o in _skill_ops(nxt["run_id"]))
        if changed or _ruled(nxt["run_id"]):
            continue
        out.append((r["opened"][0], said))
    return out


def _ruled(run_id: str) -> bool:
    conn = db.connect()
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM operations WHERE run_id=? AND tool='standing_instruction' "
                            "AND outcome='ok' LIMIT 1", (run_id,)).fetchone() is not None
    except Exception:
        return False


async def _fold(name: str, said: str) -> str:
    from backend.brain import research, skill_learning, skills
    from backend.storage import llm_calls
    s = skills.load(name)
    if s is None or not s.active:
        return ""
    with llm_calls.labelled("skill.nightly"):
        raw = await research._llm(_FOLD_PROMPT.format(skill=s.body[:5000], said=said[:500]), max_tokens=800)
    data = skill_learning._parse(raw) or {}
    edits = [e for e in (data.get("edits") or []) if isinstance(e, dict)]
    if not edits:
        return ""
    out = skills._patch(name, edits, said)
    return f"changed {name} from what you said (\"{said[:80]}\")" if out.startswith("Confirmed") else ""


# --------------------------------------------------------------------------- retires
def _retire_unused(today: datetime.date) -> list[str]:
    from backend.brain import skills
    uses = store.use_stats()
    lines, seen = [], {}

    def note_seen(st):
        first = st.setdefault("first_seen", {})
        for s in skills.active_skills():
            first.setdefault(s.name, today.isoformat())
        seen.update(first)
    _update(note_seen)
    for s in skills.active_skills():
        moments = [datetime.date.fromisoformat(seen.get(s.name, today.isoformat()))]
        if s.created:
            try:
                moments.append(datetime.date.fromisoformat(s.created))
            except ValueError:
                pass
        if s.name in uses:
            moments.append(datetime.datetime.fromtimestamp(uses[s.name]["last"], tz=lifelog.LOCAL_TZ).date())
        if (today - max(moments)).days >= _RETIRE_AFTER_DAYS:
            if skills._retire(s.name, f"not opened in {_RETIRE_AFTER_DAYS} days").startswith("Retired"):
                lines.append(f"retired {s.name} (not opened in {_RETIRE_AFTER_DAYS} days)")
    return lines


# --------------------------------------------------------------------------- the pass
async def run_pass(now: datetime.datetime) -> list[str]:
    """One night's work. Returns the lines for the morning brief."""
    from backend.brain import skills
    lines: list[str] = []
    now_ts = now.timestamp()
    seen = set(_load().get("seen", []))
    try:
        new = 0
        for cluster in find_repeats(now_ts):
            if new >= _MAX_NEW or cluster["covered_by"] or cluster["key"] in seen:
                continue
            _update(lambda st, k=cluster["key"]: st.setdefault("seen", []).append(k))
            line = await _learn_repeat(cluster)
            if line:
                lines.append(line)
                new += 1
    except Exception as e:
        log.warning("skill_nightly: repeats failed: %s", e)
    try:
        for a, b in merge_candidates(skills.active_skills())[:_MAX_MERGES]:
            line = await _merge(a, b)
            if line:
                lines.append(line)
    except Exception as e:
        log.warning("skill_nightly: merges failed: %s", e)
    try:
        for name, said in fold_candidates(now_ts)[:_MAX_FOLDS]:
            line = await _fold(name, said)
            if line:
                lines.append(line)
    except Exception as e:
        log.warning("skill_nightly: folds failed: %s", e)
    try:
        lines += _retire_unused(now.date())
    except Exception as e:
        log.warning("skill_nightly: retires failed: %s", e)
    day = now.date().isoformat()
    _update(lambda st: st.update(report={"date": day, "lines": lines}))
    if lines:
        log.info("skill_nightly: %s", "; ".join(lines))
    return lines


def report(*, strict: bool = False) -> dict:
    """The stored morning-brief report, read without consuming or reconstructing it."""
    state = pacing.load_state(_STATE_FILE, strict=True) if strict else _load()
    return state.get("report") or {}


def brief_line(now: datetime.datetime) -> str:
    """The morning brief's line about last night's skill work, or ""."""
    saved = report()
    if saved.get("date") != now.date().isoformat() or not saved.get("lines"):
        return ""
    return ("Skills overnight: " + "; ".join(saved["lines"])
            + ". /skill undo takes back the last change, /skills lists them.")


async def offer_pending() -> int:
    """Put each routine offer to him as an approval request (Approve runs schedule_task)."""
    from backend.brain import skills
    from backend.tools import approval_flow, policy
    offers = [o for o in _load().get("offers", []) if not o.get("offered")]
    made = 0
    for o in offers:
        s = skills.load(o["skill"])
        if s is not None and s.active:
            args = {"action": "add", "when": o["when"], "task": o["task"], "repeat": o["repeat"],
                    "skill": o["skill"]}
            row = await approval_flow.request(
                context="skills", tool="schedule_task", args=args, risk=policy.OWN,
                why=f"you asked for this {o['words'].split(' at ')[0]}, so it could run by itself",
                label=f"Run skill {o['skill']} {o['words']} (local)")
            if row is None:
                continue
            made += 1

        def mark(st, skill=o["skill"]):
            for item in st.get("offers", []):
                if item.get("skill") == skill:
                    item["offered"] = True
        _update(mark)
    return made


def week_line(monday: datetime.date) -> str:
    """For the Sunday review: what the skills did this week, or ""."""
    start = datetime.datetime.combine(monday, datetime.time(), tzinfo=lifelog.LOCAL_TZ).timestamp()
    end = start + 7 * 86_400
    uses = store.uses_between(start, end)
    changes = store.revisions_between(start, end)
    if not uses and not changes:
        return ""
    used = ", ".join(f"{name} {n}x" for name, n in sorted(uses.items(), key=lambda x: -x[1]))
    corrected = sorted({r["skill"] for r in changes if r["action"] == "patch" and r["actor"] == "owner"})
    new = sorted({r["skill"] for r in changes if r["action"] == "create"})
    parts = [f"used {used}"] if used else []
    if corrected:
        parts.append("corrected " + ", ".join(corrected))
    if new:
        parts.append("new " + ", ".join(new))
    return "Skills this week: " + "; ".join(parts) + "."


async def _cycle() -> None:
    from backend.brain import chronos
    now = chronos.now_local()
    state = _load()
    if now.hour >= _OFFER_AFTER_HOUR and any(not o.get("offered") for o in state.get("offers", [])):
        await offer_pending()
    if not (_WINDOW[0] <= now.hour < _WINDOW[1]) or state.get("last_night") == now.date().isoformat():
        return
    if _busy_check is not None and _busy_check():
        return
    _update(lambda st: st.update(last_night=now.date().isoformat()))   # once a night, even on failure
    await run_pass(now)


async def run_loop() -> None:
    await pacing.run_periodic(name="skill_nightly", check_interval_s=_CHECK_EVERY_S,
                              on_cycle=_cycle, pausable="work")
