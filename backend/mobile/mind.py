"""Read-only vault graph. Owners parse profiles, skills and positions; no model calls."""
from __future__ import annotations
from backend import localtime as _localtime

from collections import defaultdict
from datetime import datetime
import logging
from pathlib import Path
import re
from threading import RLock
from zoneinfo import ZoneInfo

from backend.brain import people, positions, skills
from backend.brain.semindex import _frontmatter_fields
from backend.mobile import notes
from backend.storage import goals
from backend.tools import vault

log = logging.getLogger(__name__)
LOCAL_TZ = _localtime.ZONE
FOLDERS = ("topics", "people", "skills", "letters", "creative", "conversations/significant")
CAP, RECENT_LETTERS = 400, 60
_lock = RLock()
_files: dict = {}
_graph_key = None
_graph = None


def clear_cache():
    global _graph_key, _graph
    with _lock:
        _files.clear()
        _graph_key, _graph = None, None


def _date(raw) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return (dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)).timestamp()
    except (ValueError, TypeError) as exc:
        log.warning("mobile mind: creation date %r could not be read (%s); using modification date", raw, exc)
        return None


def _title(text: str, name: str, folder: str = "") -> str:
    heading = re.search(r"^#\s+(.+)$", text, re.M)
    if heading:
        return heading[1].strip()
    # Letters and significant conversations are prose named by date and session
    # (2026-10-01_8f3b162a.md): "Letter, 1 Oct 2026" rather than a session id.
    dated = re.match(r"^(\d{4})-(\d{2})-(\d{2})_[a-zA-Z0-9]{8}\.md$", name)
    kind = {"letters": "Letter", "conversations/significant": "Conversation"}.get(folder)
    if dated and kind:
        try:
            day = datetime(int(dated[1]), int(dated[2]), int(dated[3]))
            return f"{kind}, {day.day} {day:%b} {day.year}"
        except ValueError:
            pass
    return Path(name).stem.replace("-", " ").replace("_", " ")


def _parse(folder, name, path, stamp, goal):
    text = path.read_text(encoding="utf-8")
    meta = _frontmatter_fields(text, ("created", "formed"))
    created = _date(meta.get("created") or meta.get("formed"))
    source = "recorded" if created is not None else "modified"
    dated = re.match(r"^(\d{4}-\d{2}-\d{2})_[a-zA-Z0-9]{8}\.md$", name)
    if dated and folder in ("letters", "conversations/significant"):
        created, source = _date(dated[1]), "filename"
    elif created is None:
        # Research and routine notes are named after the day they were written
        # (2026-10-12_spcx-stock-research.md); their mtime moves on every rewrite.
        prefix = re.match(r"^(\d{4}-\d{2}-\d{2})[_-]", name)
        if prefix and _date(prefix[1]) is not None:
            created, source = _date(prefix[1]), "filename"
    profile = None
    if folder == "people" and name != "owner.md" and people.is_profile(text):
        profile = people.parse_profile(text)
        created = _date(profile.created) or created
        if _date(profile.created) is not None:
            source = "recorded"
    if folder == "skills":
        skill = skills.parse(text, Path(name).stem)
        created = _date(skill.created) or created
        if _date(skill.created) is not None:
            source = "recorded"
    updated = stamp[0] / 1_000_000_000
    title = goal["title"] if goal else _title(text, name, folder)
    if goal:
        created, source = goal["created_at"], "recorded"
    common = {"folder": folder, "name": name, "title": title, "created": created if created is not None else updated,
              "created_source": source, "updated": updated, "link": {"folder": folder, "name": name}}
    if goal:
        common["state"] = goal["state"]
    if folder == "self":
        return [(dict(common, id=f"self/positions.md#{p.topic}", title=p.topic,
                      created=_date(p.formed) or updated,
                      created_source="recorded" if _date(p.formed) is not None else "modified"),
                 positions.render_position(p), None) for p in positions.parse_file(text).values()]
    return [(dict(common, id=f"{folder}/{name}"), text, profile)]


def _key(label: str) -> str:
    label = label.strip().replace("\\", "/").casefold()
    base, sep, section = label.partition("#")
    if base.endswith(".md"):
        base = base[:-3]
    return base + (sep + section if sep else "")


def _connect(parsed):
    paths, filenames, titles = defaultdict(set), defaultdict(set), defaultdict(set)
    profiles = []
    for n, _, profile in parsed:
        paths[_key(n["id"])].add(n["id"])
        filenames[_key(Path(n["name"]).stem)].add(n["id"])
        titles[_key(n["title"])].add(n["id"])
        if n["folder"] == "self":
            paths[_key(f"positions#{n['title']}")].add(n["id"])
        if profile:
            profiles.append((Path(n["id"]), profile))
    edges = {}
    def add(a, b, kind):
        if a == b:
            return
        pair = tuple(sorted((a, b)))
        if pair not in edges or kind == "link":
            edges[pair] = {"a": pair[0], "b": pair[1], "kind": kind}
    for node, text, _ in parsed:
        for target in re.findall(r"\[\[([^\]\n]+)\]\]", text):
            key = _key(target.split("|", 1)[0])
            # A real file stem wins over a title (a topic and a position often share
            # a title). Truly ambiguous file names stay unresolved, never arbitrary.
            matches = paths.get(key) or filenames.get(key) or titles.get(key)
            if not matches:
                base = key.split("#", 1)[0]
                matches = paths.get(base) or filenames.get(base) or titles.get(base, set())
            if len(matches) > 1:
                # One name in two folders: the linking note's own folder wins, as in
                # Obsidian. Still ambiguous after that, it stays unresolved.
                matches = {m for m in matches if m.startswith(node["folder"] + "/")}
            if len(matches) == 1:
                add(node["id"], next(iter(matches)), "link")
        for path, _ in people.match_profiles(text, profiles):
            add(node["id"], path.as_posix(), "mention")
    return sorted(edges.values(), key=lambda e: (e["a"], e["b"]))


def _select(nodes, edges):
    degree = defaultdict(int)
    for e in edges:
        degree[e["a"]] += 1
        degree[e["b"]] += 1
    # Per-folder connection ranking, fair round robin. Non-letters have first claim.
    def rank(n):
        return (-degree[n["id"]], -n["updated"], n["id"])
    groups = defaultdict(list)
    letters = [n for n in nodes if n["folder"] == "letters"]
    recent = {n["id"] for n in sorted(letters, key=lambda n: (-n["created"], n["id"]))[:RECENT_LETTERS]}
    letters = sorted((n for n in letters if n["id"] in recent or degree[n["id"]] > 0), key=rank)
    for n in nodes:
        if n["folder"] != "letters":
            groups[n["folder"]].append(n)
    for group in groups.values():
        group.sort(key=rank)
    chosen = []
    while len(chosen) < CAP and any(groups.values()):
        for folder in sorted(groups):
            if groups[folder] and len(chosen) < CAP:
                chosen.append(groups[folder].pop(0))
    chosen += letters[:max(0, CAP - len(chosen))]
    keep = {n["id"] for n in chosen}
    kept_edges = [e for e in edges if e["a"] in keep and e["b"] in keep]
    kept_degree = defaultdict(int)
    for e in kept_edges:
        kept_degree[e["a"]] += 1
        kept_degree[e["b"]] += 1
    return [dict(n, size=kept_degree[n["id"]]) for n in sorted(chosen, key=lambda n: n["id"])], kept_edges


def view() -> dict:
    global _graph_key, _graph
    with _lock:
        failures, entries, bases = [], [], {}
        def failure(label, exc):
            log.warning("mobile mind: %s could not be read (%s)", label, exc)
            failures.append(f"{label} could not be read just now.")
        def entry(folder, name, goal=None):
            try:
                path = notes.resolve(folder, name, _bases=bases)
                if not path.is_file():
                    return
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                meta = tuple(sorted(goal.items())) if goal else ()
                entries.append((folder, name, path, stamp, meta, goal))
            except Exception as exc:
                failure(f"{folder}/{name}", exc)
        for folder in FOLDERS:
            try:
                directory = vault._resolve(folder, "_mobile_anchor.md", folders=notes.FOLDERS).parent
                for path in sorted(directory.glob("*.md")):
                    if path.name.startswith("_") or (folder == "people" and path.name.casefold() == "predictions.md"):
                        continue
                    entry(folder, path.name)
            except Exception as exc:
                failure(folder, exc)
        entry("self", "positions.md")
        try:
            for g in goals.mind_results():
                entry("goals", f"{g['folder']}/result.md", g)
        except Exception as exc:
            failure("Goal results", exc)
        root = str(vault._VAULT.resolve())
        key = (root, tuple((f, n, stamp, meta) for f, n, _, stamp, meta, _ in entries))
        if key == _graph_key and not failures:
            return _graph
        parsed, current = [], {}
        for folder, name, path, stamp, meta, goal in entries:
            file_key = (root, folder, name, stamp, meta)
            try:
                value = _files.get(file_key)
                if value is None:
                    value = _parse(folder, name, path, stamp, goal)
                current[file_key] = value
                parsed.extend(value)
            except Exception as exc:
                failure(f"{folder}/{name}", exc)
        _files.clear()
        _files.update(current)
        nodes, edges = _select([n for n, _, _ in parsed], _connect(parsed))
        result = {"nodes": nodes, "edges": edges, "total": len(parsed), "truncated": len(nodes) < len(parsed),
                  "notes": failures, "complete": not failures}
        # Retry failures next time. Never turn a failed source into a lasting empty cache.
        _graph_key, _graph = (key if not failures else None), result
        return result


def hub() -> dict:
    graph = view()
    return {"mind": {"count": len(graph["nodes"]), "total": graph["total"], "complete": graph["complete"]},
            "notes": graph["notes"]}
