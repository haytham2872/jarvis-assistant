"""Shared read-only note access for the phone. Writers retain their existing folder scope."""
from __future__ import annotations

import logging
from pathlib import PureWindowsPath
from urllib.parse import unquote

from backend.tools import vault

log = logging.getLogger(__name__)
FOLDERS = ("topics", "people", "self", "letters", "skills", "creative", "drafts", "shared", "goals", "attachments", "conversations/significant")
MAX_CHARS = 60_000


class InvalidNote(ValueError):
    """An invalid request, distinct from a note whose UTF-8 contents cannot be read."""


def _flat(name: str) -> bool:
    return not (not name or ".." in name
            or any(c in name for c in '/\\:<>"|?*') or any(ord(c) < 32 for c in name) or unquote(name) != name
            or name != name.strip() or PureWindowsPath(name).is_reserved())


def resolve(folder: str, name: str, *, _bases: dict | None = None):
    """Validate before vault resolution, then check real paths, including symlinks/ADS."""
    if folder == "goals":
        from backend.storage import goals
        parts = name.split("/")
        if (len(parts) != 2 or not _flat(parts[0]) or parts[1] != "result.md"
                or goals.goal_for_folder(parts[0]) is None):
            raise InvalidNote("Choose the result of a known goal.")
        # The allowlist entry is constructed only from an exact, validated store row.
        allowed = f"goals/{parts[0]}"
        base = vault._resolve("goals", "_mobile_anchor.md", folders=FOLDERS).parent.resolve()
        directory = vault._resolve(allowed, "result.md", folders=(allowed,)).parent
        path = vault._resolve(allowed, "result.md", folders=(allowed,)).resolve()
        if (not base.is_relative_to(vault._VAULT.resolve()) or directory.is_symlink()
                or directory.resolve() != base / parts[0] or not path.is_relative_to(directory.resolve())):
            raise InvalidNote("The note is outside its folder.")
        return path
    if folder not in FOLDERS or not _flat(name) or not name.endswith(".md"):
        raise InvalidNote("Choose a Markdown note in an allowed folder.")
    # A graph build can reuse folder roots within that single read. Every file's real
    # path is still resolved afresh, so replacing a file/directory with a symlink fails.
    bases = _bases if _bases is not None else {}
    if "" not in bases:
        bases[""] = vault._VAULT.resolve()
    if folder not in bases:
        bases[folder] = vault._resolve(folder, "_mobile_anchor.md", folders=FOLDERS).parent.resolve()
    base = bases[folder]
    path = vault._resolve(folder, name, folders=FOLDERS).resolve()
    if not base.is_relative_to(bases[""]) or not path.is_relative_to(base):
        raise InvalidNote("The note is outside its folder.")
    return path


def link(folder: str, name: str) -> dict | None:
    """Only advertise a link that the same note reader can open at build time."""
    path = resolve(folder, name)
    return {"folder": folder, "name": name} if path.is_file() else None


def read(folder: str, name: str) -> dict:
    path = resolve(folder, name)
    try:
        with path.open(encoding="utf-8") as stream:
            text = stream.read(MAX_CHARS + 1)
        return {"folder": folder, "name": name, "text": text[:MAX_CHARS],
                "modified": path.stat().st_mtime, "truncated": len(text) > MAX_CHARS}
    except FileNotFoundError:
        raise
    except Exception as exc:
        log.warning("mobile notes: note could not be read (%s/%s): %s", folder, name, exc)
        raise
