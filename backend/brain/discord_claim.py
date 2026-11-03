"""/claim: the first owner of a new install, without reading ids out of a log.

With DISCORD_ALLOWED_USER_ID unset, JARVIS answers nobody. At startup it logs a one-time
code; the first person to DM the bot "/claim <code>" becomes the allowed user, and their id
is written to .env so it holds after a restart.

The code is only in the server's log, so only someone who can read the log can claim.
It works once per process, is compared in constant time, and five wrong tries switch
claiming off until the next restart. A wrong try gets no reply, as any stranger's message
does: nothing tells them the bot is attended.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# No 0/O or 1/I/L: the code is read off a terminal and typed on a phone.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8                       # 31^8, about 40 bits
MAX_TRIES = 5

_code: str | None = None
_tries = 0
_claimed = False

_CLAIM = re.compile(r"^/claim(?:\s+(\S+))?\s*$", re.IGNORECASE)
_LINE = re.compile(r"^[ \t]*#?[ \t]*DISCORD_ALLOWED_USER_ID[ \t]*=[^\r\n]*", re.MULTILINE)


def is_claim(text: str) -> bool:
    return bool(_CLAIM.match((text or "").strip()))


def open_claim() -> str:
    """Make this process's code (once) and return it, for the startup log."""
    global _code
    if _code is None:
        _code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return _code


def try_claim(author_id: str, text: str, *, in_dm: bool) -> bool:
    """True when this message claims the bot. Synchronous, with no await, so two claims
    arriving together cannot both win."""
    global _tries, _claimed
    if _claimed or _code is None or _tries >= MAX_TRIES or not in_dm:
        return False
    m = _CLAIM.match((text or "").strip())
    if not m:
        return False
    given = (m.group(1) or "").upper()
    if not hmac.compare_digest(given.encode(), _code.encode()):
        _tries += 1
        log.warning("Discord: wrong /claim code from user id=%s (%d of %d tries)",
                    author_id, _tries, MAX_TRIES)
        if _tries >= MAX_TRIES:
            log.warning("Discord: /claim is off until the next restart, which logs a new code.")
        return False
    _claimed = True
    return True


def save_owner(user_id: str, path: Path | None = None) -> bool:
    """Write DISCORD_ALLOWED_USER_ID=<id> into .env: replaces the existing line (empty or
    commented out) or appends one. Atomic, keeps the file's line endings. False when the
    file cannot be written; the id still holds for this run."""
    path = path or ENV_FILE
    if not re.fullmatch(r"\d{5,25}", user_id or ""):
        return False
    line = f"DISCORD_ALLOWED_USER_ID={user_id}"
    try:
        text = path.read_bytes().decode("utf-8") if path.exists() else ""
        nl = "\r\n" if "\r\n" in text else "\n"
        if _LINE.search(text):
            text = _LINE.sub(lambda _m: line, text, count=1)
        else:
            if text and not text.endswith(("\n", "\r\n")):
                text += nl
            text += line + nl
        tmp = path.with_name(path.name + ".claim.tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("Discord: could not save the owner's id to %s: %s", path, exc)
        return False


def _reset_for_tests() -> None:
    global _code, _tries, _claimed
    _code, _tries, _claimed = None, 0, False
