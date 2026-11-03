"""H4: `vault_send` -- deliver a vault note's exact text to the owner on Discord.

Decision 4 of the thinking-headroom roadmap: when the owner asks to see, recite or get a
note's full contents, he gets the file itself, not a retelling. A model retyping a note
can change words; here code reads the bytes and sends them, and the model only confirms.

The file is read as bytes and decoded without newline translation (`Path.read_text`
would turn \\r\\n into \\n), so what is sent is what is on disk. Folders are resolved by
the same helpers `vault_read` uses, including folder "root" for queue.md,
research-queue.md and schedule.md.

Sending is injected (`set_sender`) so this tool never imports the Discord bridge; main.py
wires it to `discord_bridge.send_verbatim`. Unconfigured, the tool says so plainly
rather than pretending a send happened.
"""
from __future__ import annotations
from backend import owner as _owner

import logging
from typing import Awaitable, Callable, Optional

from backend.tools import vault as _vault

log = logging.getLogger(__name__)

# (label, text) -> (sent, how). `how` says where and in what form, for the tool result.
Sender = Callable[[str, str], Awaitable[tuple[bool, str]]]
_sender: Optional[Sender] = None


def set_sender(fn: Optional[Sender]) -> None:
    global _sender
    _sender = fn


def read_exact(filename: str, folder: str) -> tuple[str, str]:
    """(label, exact text) of a vault file. Raises on a bad folder/name or missing file."""
    folder = (folder or "").strip()
    if folder == _vault.ROOT_FOLDER:
        path = _vault._root_file(filename)
        label = path.name
    elif folder == "skills":                  # not a vault_write folder, but his to read
        from backend.brain import skills as _skills
        path = _skills.skills_dir() / _vault._safe_filename(filename)
        label = f"skills/{path.name}"
    else:
        path = _vault._resolve(folder, filename)
        label = f"{folder}/{path.name}"
    if not path.exists():
        raise FileNotFoundError(f"Not found: {label}")
    return label, path.read_bytes().decode("utf-8")


async def vault_send(filename: str, folder: str) -> str:
    try:
        label, text = read_exact(filename, folder)
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        return f"Error: {e}"
    if not text.strip():
        return f"Error: {label} is empty -- nothing to send."
    if _sender is None:
        return "Error: Discord is not connected, so the note could not be sent."
    try:
        sent, how = await _sender(label, text)
    except Exception as e:
        log.warning("vault_send: sending %s failed: %s", label, e)
        return f"Error: sending {label} failed ({e}). Nothing was confirmed sent."
    if not sent:
        return (_owner.say(f"Error: {label} could not be confirmed sent ({how}). Do not say it was "
                "sent; tell «owner» it may not have arrived."))
    return (_owner.say(f"Sent {label} to «owner» exactly as it is on disk ({len(text):,} chars, {how}). "
            "Do not repeat or summarise its contents in your reply unless «he» asks."))
